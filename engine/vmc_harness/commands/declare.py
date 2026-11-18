"""`vmc-harness declare` — 착수 선언(spec 03). 에이전트가 첫 편집 전에 부른다.

입력: 인자 또는 stdin JSON(09 declare 스키마). 같은 세션의 두 번째 선언: 아직 편집 전이거나 범위가 겹치면
같은 작업 갱신(테스트 수정 허용은 누적), 아니면 앞 작업을 마감하고 새 작업. `--same-task`·`--new-task`로 정한다.
`--why-not-smaller "…"`: 과설계 축소 검토에 이유 한 줄만 남긴다.
"""

import argparse
import json
import sys


def register(sub):
    p = sub.add_parser("declare", help="착수 선언(에이전트가 첫 편집 전에) — 인자 또는 stdin JSON")
    p.add_argument("--json", help="09 스키마 declare JSON 문자열(없고 stdin이 파이프면 stdin)")
    p.add_argument("--intent", help="하려는 것 한 줄")
    p.add_argument("--scope", action="append", help="건드릴 범위(파일·디렉터리·모듈·glob). 여러 번 또는 쉼표")
    p.add_argument("--new", action="append", help="새로 만들 것(파일·모듈·의존성)")
    p.add_argument("--protected-touch", action="store_true", help="보호 영역 접촉 예상")
    p.add_argument("--test-change-files", action="append", help="수정할 기존 테스트 파일")
    p.add_argument("--test-change-reason", help="테스트 수정 이유")
    p.add_argument("--tokens", help="토큰 예측 P30,P50,P80")
    p.add_argument("--agent-sec", help="에이전트 작업 시간 예측(초, 사람 턴 대기 제외) P30,P50,P80")
    p.add_argument("--agent-min", help="에이전트 작업 시간 예측(분) P30,P50,P80 — --agent-sec 대신")
    p.add_argument("--approvals", type=int, help="승인 예상 건수")
    p.add_argument("--end", help="어디까지 하나 한마디(예: 문서만, 커밋까지, 배포까지) — 사람에게 보이는 착수 한 줄에 붙는다")
    p.add_argument("--session", help="세션 id(없으면 환경변수 → 가장 최근 훅 세션)")
    p.add_argument("--agent", choices=("claude", "codex"))
    g = p.add_mutually_exclusive_group()
    g.add_argument("--same-task", action="store_true", help="같은 작업을 고쳐 선언(앞 선언에 이어 붙임)")
    g.add_argument("--new-task", action="store_true", help="새 작업(앞 작업 마감)")
    p.add_argument("--why-not-smaller", help="과설계 신호에 대해 줄이지 않는 이유 한 줄")
    p.add_argument("--root", help=argparse.SUPPRESS)
    p.set_defaults(func=_run)


class _ArgError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _ArgError(message)


def parse_argv(argv):
    """`vmc-harness declare` 인자 목록 → (raw dict, mode). 훅이 명령 앞 구간의 declare를 미리 등록할 때 쓴다(task.handlers).
    인자 오류·JSON·stdin 입력·--why-not-smaller 단독이면 ValueError — 훅은 그 명령을 막고 빠진 것을 알린다."""
    top = _Parser(prog="vmc-harness", add_help=False)
    sub = top.add_subparsers(parser_class=_Parser)
    register(sub)
    a = top.parse_args(["declare"] + list(argv))
    if a.json:
        raise ValueError("--json 입력")
    return _from_args(a), ("same" if a.same_task else ("new" if a.new_task else None))


def _flat(values):
    out = []
    for v in values or []:
        out.extend(x.strip() for x in str(v).split(",") if x.strip())
    return out


def _from_args(a):
    raw = {}
    if a.intent:
        raw["intent"] = a.intent
    if a.scope:
        raw["scope"] = _flat(a.scope)
    if a.new:
        raw["new"] = _flat(a.new)
    if a.protected_touch:
        raw["protected_touch"] = True
    if a.test_change_files or a.test_change_reason:
        raw["test_change"] = {"files": _flat(a.test_change_files), "reason": a.test_change_reason or ""}
    pred = {}
    if a.tokens:
        pred["tokens"] = a.tokens
    if a.agent_sec:
        pred["agent_sec"] = a.agent_sec
    elif a.agent_min:
        pred["agent_min"] = a.agent_min
    if pred:
        raw["predict"] = pred
    if a.approvals is not None:
        raw["approvals_expected"] = a.approvals
    if a.end:
        raw["end"] = a.end
    return raw


def _run(a):
    from vmc_harness.core import paths
    from vmc_harness.task import declare as dec
    root = a.root or paths.find_project_root()
    if not root:
        print("하네스가 설치되지 않은 프로젝트", file=sys.stderr)
        return 2
    try:
        if a.why_not_smaller and not (a.json or a.intent):
            t = dec.why_not_smaller(root, a.why_not_smaller, a.session)
            print("기록함: %s" % t["task_id"])
            return 0
        raw = _from_args(a)
        text = a.json
        if not text and not raw and not sys.stdin.isatty():
            text = sys.stdin.read()
        if text and text.strip():
            try:
                obj = json.loads(text)
            except ValueError:
                print("착수 선언 JSON 형식 오류", file=sys.stderr)
                return 2
            obj.update(raw)
            raw = obj
        res = dec.declare(root, raw, session_id=a.session, agent=a.agent,
                          mode="same" if a.same_task else ("new" if a.new_task else None))
        if a.why_not_smaller:
            dec.why_not_smaller(root, a.why_not_smaller, res["task"]["session_id"])
    except dec.DeclareError as e:
        print(str(e), file=sys.stderr)
        return 2
    print("\n".join(res["lines"]))
    return 0
