"""④ 오버헤드 — 데모 작업 소·중을 합성 프로젝트에서 실제 훅 체인으로 돌려 하네스가 더한 시간·토큰을 잰다 (spec 11, 12 #10).

에이전트처럼 이벤트마다 `python -m vmc_harness hook <Event> --agent claude`를 새 프로세스로 부른다(인터프리터 시작 포함).
- 시간(sec): 훅·착수 선언 호출의 벽시계 합 = 에이전트가 기다린 시간
- 토큰: 에이전트 컨텍스트에 들어간 하네스 글(규칙 블록 + 훅 출력 + 착수 선언 출력, 추정) + 백그라운드 LLM 토큰
  (PM·카드 검색·과설계 판정은 작업 기록 overhead.tokens, 자가 점검은 events harness_overhead) — 백그라운드는
  `eval.overhead_bg_wait_s`까지 기다려 센다(claude CLI가 없으면 0)
고객 데이터는 쓰지 않는다. task 영역 `tests/bench_overhead.py`(소 작업·in-process 비교용)와 같은 방식이다.
"""

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

from vmc_harness.core import config
from vmc_harness.eval import common

ENGINE = Path(__file__).resolve().parents[2]
SID = "eval-overhead"

FILES = {
    "src/list.py": "def sort_items(xs):\n    return sorted(xs)\n",
    "src/report.py": "from src.list import sort_items\n\n\ndef top(xs, n):\n    return sort_items(xs)[:n]\n",
    "src/fmt.py": "def fmt(x):\n    return str(x)\n",
}

TASKS = {
    "small": {"prompt": "정렬 반대로 해줘", "reads": ["src/list.py"] * 3,
              "edits": [("src/list.py", "sorted(xs)", "sorted(xs, reverse=True)")], "bash": [],
              "declare": {"intent": "목록 정렬을 내림차순으로", "scope": ["src/list.py"],
                          "predict": {"tokens": [5000, 10000, 20000], "agent_sec": [30, 60, 120]},
                          "approvals_expected": 0}},
    "mid": {"prompt": "상위 n개 보고서를 내림차순·문자열로 바꿔줘",
            "reads": ["src/list.py", "src/report.py", "src/fmt.py", "src/list.py", "src/report.py", "src/fmt.py"],
            "edits": [("src/list.py", "sorted(xs)", "sorted(xs, reverse=True)"),
                      ("src/fmt.py", "str(x)", "'%s' % (x,)"),
                      ("src/report.py", "sort_items(xs)[:n]", "[str(x) for x in sort_items(xs)[:n]]")],
            "bash": ["python -m unittest discover -s src"],
            "declare": {"intent": "상위 n개 보고서 정렬·형식 변경", "scope": ["src/list.py", "src/report.py", "src/fmt.py"],
                        "predict": {"tokens": [20000, 40000, 80000], "agent_sec": [120, 240, 480]},
                        "approvals_expected": 0}},
}


def _events(root, spec):
    base = {"session_id": SID, "cwd": str(root), "transcript_path": str(root.parent / "t.jsonl")}
    evs = [("SessionStart", dict(base, source="startup")), ("UserPromptSubmit", dict(base, prompt=spec["prompt"])),
           ("DECLARE", None)]
    for rel in spec["reads"]:
        r = dict(base, tool_name="Read", tool_input={"file_path": str(root / rel)})
        evs += [("PreToolUse", r), ("PostToolUse", r)]
    for rel, old, new in spec["edits"]:
        e = dict(base, tool_name="Edit", tool_input={"file_path": str(root / rel), "old_string": old, "new_string": new})
        evs += [("PreToolUse", e), ("EDIT", (rel, old, new)), ("PostToolUse", e)]
    for cmd in spec["bash"]:
        b = dict(base, tool_name="Bash", tool_input={"command": cmd})
        evs += [("PreToolUse", b), ("PostToolUse", b)]
    evs += [("Stop", dict(base, stop_hook_active=False)), ("SessionEnd", dict(base, reason="exit"))]
    return evs


def _transcript(root, prompt):
    rows = [{"type": "user", "sessionId": SID, "cwd": str(root), "timestamp": "2026-10-22T10:00:00Z",
             "message": {"content": prompt}},
            {"type": "assistant", "sessionId": SID, "timestamp": "2026-10-22T10:00:40Z",
             "message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 10}}}]
    (root.parent / "t.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _tok(text, root):
    from vmc_harness.gates.rules import estimate_tokens
    return estimate_tokens(text or "", root) if text else 0


def _bg_tokens(root, task_id, wait_s, expect_bg):
    """백그라운드 LLM 토큰(작업 overhead + 자가 점검). wait_s까지 기다린다. → ({"task","selfcheck"}, waited_s, complete)"""
    from vmc_harness.core import events, llm
    from vmc_harness.core.project import local_dir
    from vmc_harness.task import state as tstate
    t0 = time.time()
    complete = not llm.available()
    while llm.available() and time.time() - t0 < wait_s:
        bg_ok = (not expect_bg) or any((local_dir(root) / "tasks").glob("*.bg.json"))
        sc_ok = any(True for _ in events.read(root, "session_selfcheck"))
        if bg_ok and sc_ok:
            complete = True
            break
        time.sleep(1)
    task = tstate.by_id(root, task_id) if task_id else None
    task_tokens = int(((task or {}).get("overhead") or {}).get("tokens") or 0)
    selfcheck_tokens = sum(int(e.get("tokens") or 0) for e in events.read(root, "harness_overhead"))
    return {"task": task_tokens, "selfcheck": selfcheck_tokens}, round(time.time() - t0, 1), complete


def run_task(kind, wait_s=None):
    """소·중 작업 1건 → {"sec","context_tokens","bg_tokens","tokens","per_event_ms","size", ...}"""
    spec = TASKS[kind]
    wait_s = config.get("eval.overhead_bg_wait_s") if wait_s is None else wait_s
    clear = {k: None for k in common.SESSION_VARS}
    with common.temp_project(FILES, git=True) as (root, home), common.env(VMC_HARNESS_HOME=str(home), **clear):
        from vmc_harness.gates import rules
        rule_tokens = rules.render(str(root))["tokens"]
        _transcript(root, spec["prompt"])
        env = dict(os.environ, PYTHONPATH=str(ENGINE) + os.pathsep + os.environ.get("PYTHONPATH", ""))
        total, ctx_tokens, per = 0.0, rule_tokens, {}
        for ev, raw in _events(root, spec):
            if ev == "EDIT":
                rel, old, new = raw
                f = root / rel
                f.write_text(f.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
                continue
            if ev == "DECLARE":
                args = [sys.executable, "-m", "vmc_harness", "declare", "--session", SID, "--agent", "claude"]
                payload = json.dumps(spec["declare"])
            else:
                args = [sys.executable, "-m", "vmc_harness", "hook", ev, "--agent", "claude"]
                payload = json.dumps(raw)
            t0 = time.perf_counter()
            p = subprocess.run(args, input=payload, cwd=str(root), env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            dt = time.perf_counter() - t0
            total += dt
            per.setdefault(ev, []).append(dt)
            ctx_tokens += _tok(p.stdout, root)
        from vmc_harness.task import state as tstate
        tasks = tstate.all_tasks(str(root))   # 세션이 끝나 current()는 비어 있을 수 있다 — 이 프로젝트의 작업은 1건
        task_id = tasks[-1].get("task_id") if tasks else None
        task = tstate.by_id(str(root), task_id) if task_id else None
        bg, waited, complete = _bg_tokens(str(root), task_id, wait_s, expect_bg=(task or {}).get("size") != "small")
        return {"kind": kind, "sec": round(total, 3), "context_tokens": ctx_tokens, "rule_block_tokens": rule_tokens,
                "bg_tokens": sum(bg.values()), "bg_tokens_by": bg, "tokens": ctx_tokens + sum(bg.values()), "bg_complete": complete, "bg_waited_s": waited,
                "size": (task or {}).get("size"),
                "per_event_ms": {k: round(statistics.median(v) * 1000) for k, v in per.items()}}


def run(rounds=None, wait_s=None, progress=None):
    rounds = rounds or config.get("eval.overhead_rounds")
    say = progress or (lambda *_: None)
    out = {"status": "ok", "rounds": rounds, "python": sys.version.split()[0], "tasks": {}}
    metrics = {}
    for kind in ("small", "mid"):
        rows = []
        for i in range(rounds):
            say("④ %s 작업 %d/%d" % (kind, i + 1, rounds))
            rows.append(run_task(kind, wait_s))
        out["tasks"][kind] = rows
        metrics["%s_sec" % kind] = round(statistics.median(r["sec"] for r in rows), 3)
        metrics["%s_tokens" % kind] = int(statistics.median(r["tokens"] for r in rows))
        metrics["%s_context_tokens" % kind] = int(statistics.median(r["context_tokens"] for r in rows))
    out["metrics"] = metrics
    out["budget_small_s"] = config.get("budget.small_task_overhead_s")
    return out
