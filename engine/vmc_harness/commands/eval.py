"""`vmc-harness eval` — 회귀 세트 실행기(spec 11). 릴리스 전 VMC PC에서 돈다(고객 PC 명령 아님).

    eval --dataset ~/vmc-client-data/<고객>/eval/                 다섯 세트 전부 → results/<시각>.json·.md, 기준선 비교
    eval --dataset … --sets diagnosis,classify --runs 2 --sample 100
    eval --dataset … --set-baseline                                 이번 결과를 기준선으로
평가 때만 모델 교체: 환경변수 VMC_HARNESS_EVAL_MODEL=<모델>. 종료 코드: 0 통과(또는 기준선 없음) · 1 기준선보다 나빠짐 · 2 입력 오류
"""

import json
import sys


def register(sub):
    p = sub.add_parser("eval", help="회귀 세트(진단 정답지·학습 후보 분류·렌더링·오버헤드·수용률) — 릴리스 전 기준선 비교")
    p.add_argument("--dataset", required=True, help="데이터셋 폴더(레포 밖, 예 ~/vmc-client-data/<고객>/eval/)")
    p.add_argument("--sets", help="쉼표 목록: diagnosis,classify,render,overhead,acceptance,learning (기본 전부)")
    p.add_argument("--runs", type=int, help="① 진단·⑥ 학습 라벨을 돌리는 횟수(기본 eval.diagnose_runs · eval.learning_runs)")
    p.add_argument("--no-existing", action="store_true", help="① 프로젝트에 남은 마지막 진단 결과를 점수에 넣지 않는다")
    p.add_argument("--sample", type=int, help="② 표본 발화 수 상한(기본 eval.classify_sample_prompts)")
    p.add_argument("--rounds", type=int, help="④ 소·중 작업 각각 실측 건수(기본 eval.overhead_rounds)")
    p.add_argument("--no-llm", action="store_true", help="③ 재작성 없이 검사만")
    p.add_argument("--set-baseline", action="store_true", help="이번 결과를 기준선(baseline.json)으로")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)


def run(args):
    from vmc_harness.eval import SETS, runner
    sets = [s.strip() for s in (args.sets or "").split(",") if s.strip()] or list(SETS)
    bad = [s for s in sets if s not in SETS]
    if bad:
        print("없는 세트: %s (가능: %s)" % (", ".join(bad), ", ".join(SETS)), file=sys.stderr)
        return 2
    opts = {"runs": args.runs, "use_existing": not args.no_existing, "sample": args.sample,
            "rounds": args.rounds, "no_llm": args.no_llm}
    say = (lambda m: print(m, file=sys.stderr)) if args.json else print
    try:
        result = runner.run(args.dataset, sets, opts, progress=say)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    paths = runner.save(args.dataset, result, set_baseline=args.set_baseline)
    if args.json:
        print(json.dumps({"passed": result["compare"]["passed"], "files": paths, "compare": result["compare"]},
                         ensure_ascii=False, indent=2))
    else:
        print(runner.summary_md(result).split("\n## ")[0])
        print("결과: %s" % paths["json"])
        if paths.get("baseline"):
            print("기준선으로 저장: %s" % paths["baseline"])
    return 1 if result["compare"]["passed"] is False else 0
