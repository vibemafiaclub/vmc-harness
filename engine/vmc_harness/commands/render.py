"""`vmc-harness render` — 렌더링 엔진(spec 08). VMC TS 쪽도 같은 명령을 부른다(stdin/stdout JSON, 코드 두 벌 금지).

입력 JSON(`--input <파일>` 또는 `-`=stdin): {"facts":{…}, "draft":"…", "kind":"notice|approval|rule|…", "reader":…}
  "facts" 키가 없으면 입력 전체를 facts로 본다. `--eval`이면 {"cases":[…]} → 회귀 세트 ③ 결과.
출력 JSON: {"text","checks":{"facts_ok","missing_facts","unknown_words"},"rewritten","llm_calls"}
`--refresh-profile`: 이 PC 세션 프롬프트로 독자 프로필 재계산.
"""

import json
import sys


def register(sub):
    p = sub.add_parser("render", help="렌더링 엔진: 사실 JSON → 독자 말(기계 검사 2개, 실패 때만 1회 재작성)")
    p.add_argument("--reader", default="me", help="프로필: me(이 PC) | 프로필 json 경로 | 이름 | team")
    p.add_argument("--input", default="-", help="입력 JSON 파일(기본 stdin)")
    p.add_argument("--kind", help="notice | approval | report_line | rule(원문 그대로) …")
    p.add_argument("--no-llm", action="store_true", help="검사만(재작성 없음)")
    p.add_argument("--eval", action="store_true", help="회귀 세트 ③: 입력 {cases:[…]}")
    p.add_argument("--refresh-profile", action="store_true", help="독자 프로필 재계산")
    p.set_defaults(func=_run)


def _read(path):
    if path in (None, "-"):
        return json.loads(sys.stdin.read() or "{}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _run(a):
    from vmc_harness.core import paths
    root = paths.find_project_root()
    if a.refresh_profile:
        if not root:
            print(json.dumps({"ok": False, "reason": "하네스가 설치되지 않은 프로젝트"}, ensure_ascii=False))
            return 2
        from vmc_harness.render import profile
        prof = profile.compute(root)
        print(json.dumps({"ok": True, "prompts": prof["source"]["prompts"], "sessions": prof["source"].get("sessions"),
                          "preferred": {k: v["term"] for k, v in prof["preferred"].items()}}, ensure_ascii=False))
        return 0
    from vmc_harness.render import engine
    data = _read(a.input)
    reader = data.get("reader", a.reader) if isinstance(data, dict) and a.reader == "me" else a.reader
    if a.eval:
        out = engine.evaluate(root, reader, data.get("cases") or [], allow_llm=not a.no_llm)
    else:
        facts = data.get("facts") if isinstance(data, dict) and "facts" in data else data
        draft = data.get("draft") if isinstance(data, dict) else None
        kind = a.kind or (data.get("kind") if isinstance(data, dict) else None) or "notice"
        out = engine.render(root, reader, facts, draft=draft, kind=kind, allow_llm=not a.no_llm)
    print(json.dumps(out, ensure_ascii=False))
    return 0
