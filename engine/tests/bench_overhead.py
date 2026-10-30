"""소 작업 오버헤드 실측 (spec 12 미검증 #10, 11 회귀 세트 ④) — `python3 tests/bench_overhead.py [--rounds N]`.

합성 임시 프로젝트에서 소 작업 1건(세션 시작 → 지시 → 착수 선언 → 읽기 3번 → 편집 1번 → 종료 → 세션 끝)을
하네스 훅 체인으로 돌려 하네스가 더한 시간을 잰다. 두 가지를 따로 잰다.
- in-process: 디스패처(있으면 전체 HANDLER_MODULES, 없으면 task 핸들러만) 호출 시간 — 순수 엔진 비용
- subprocess: 실제 에이전트처럼 이벤트마다 파이썬 프로세스를 새로 띄운 시간(인터프리터 시작 포함)
목표: 합계 `budget.small_task_overhead_s` 이하. 고객·실제 세션 데이터는 쓰지 않는다.
"""

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

SID = "bench-session"


def _project(tmp):
    root = tmp / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "list.py").write_text("def sort_items(xs):\n    return sorted(xs)\n", encoding="utf-8")
    (root / ".harness").mkdir()
    (root / ".harness" / "config.json").write_text(json.dumps(
        {"project_id": str(uuid.uuid4()), "profile": "standard", "source_roots": ["."]}), encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "b@b"], ["config", "user.name", "b"],
                 ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git"] + args, cwd=str(root), check=True)
    return root


def _events(root):
    f = root / "src" / "list.py"
    base = {"session_id": SID, "cwd": str(root), "transcript_path": str(root.parent / "t.jsonl")}
    read = dict(base, tool_name="Read", tool_input={"file_path": str(f)})
    edit = dict(base, tool_name="Edit", tool_input={"file_path": str(f), "old_string": "sorted(xs)",
                                                      "new_string": "sorted(xs, reverse=True)"})
    return [("SessionStart", dict(base, source="startup")),
            ("UserPromptSubmit", dict(base, prompt="정렬 반대로 해줘")),
            ("DECLARE", None),
            ("PreToolUse", read), ("PostToolUse", read),
            ("PreToolUse", read), ("PostToolUse", read),
            ("PreToolUse", read), ("PostToolUse", read),
            ("PreToolUse", edit), ("EDIT", None), ("PostToolUse", edit),
            ("Stop", dict(base, stop_hook_active=False)),
            ("SessionEnd", dict(base, reason="exit"))]


DECLARE = {"intent": "목록 정렬을 내림차순으로", "scope": ["src/list.py"],
           "predict": {"tokens": [5000, 10000, 20000], "agent_sec": [30, 60, 120]}, "approvals_expected": 0}


def _transcript(root):
    rows = [{"type": "user", "sessionId": SID, "cwd": str(root), "timestamp": "2026-10-22T10:00:00Z",
             "message": {"content": "정렬 반대로 해줘"}},
            {"type": "assistant", "sessionId": SID, "timestamp": "2026-10-22T10:00:40Z",
             "message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 10}}}]
    (root.parent / "t.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _edit(root):
    f = root / "src" / "list.py"
    f.write_text(f.read_text(encoding="utf-8").replace("sorted(xs)", "sorted(xs, reverse=True)"), encoding="utf-8")


def run_inprocess(root, modules):
    from vmc_harness.hooks import dispatcher
    from vmc_harness.task import declare as dec
    times = []
    for ev, raw in _events(root):
        t0 = time.perf_counter()
        if ev == "DECLARE":
            dec.declare(str(root), dict(DECLARE), session_id=SID, agent="claude")
        elif ev == "EDIT":
            _edit(root)
            continue
        else:
            dispatcher.dispatch(ev, "claude", json.dumps(raw), modules=modules, cwd=str(root))
        times.append((ev, time.perf_counter() - t0))
    return times


def run_subprocess(root, modules_all):
    env = dict(os.environ, PYTHONPATH=str(ENGINE))
    times = []
    code = ("import sys,json;from vmc_harness.hooks import dispatcher;"
            "out=dispatcher.dispatch(sys.argv[1],'claude',sys.stdin.read()%s);"
            "o=out[0] if isinstance(out,tuple) else out;sys.stdout.write(o or '')") % (
        "" if modules_all else ",modules=[('vmc_harness.task.handlers',False)]")
    for ev, raw in _events(root):
        t0 = time.perf_counter()
        if ev == "DECLARE":
            subprocess.run([sys.executable, "-m", "vmc_harness", "declare", "--session", SID],
                           input=json.dumps(DECLARE), cwd=str(root), env=env, capture_output=True, text=True)
        elif ev == "EDIT":
            _edit(root)
            continue
        else:
            subprocess.run([sys.executable, "-c", code, ev], input=json.dumps(raw), cwd=str(root), env=env,
                           capture_output=True, text=True)
        times.append((ev, time.perf_counter() - t0))
    return times


def _one(mode, modules_all):
    tmp = Path(tempfile.mkdtemp(prefix="vmh-bench-"))
    old = dict(os.environ)
    try:
        os.environ["VMC_HARNESS_HOME"] = str(tmp / "home")
        for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID",
                  "VMC_HARNESS_SESSION_ID"):
            os.environ.pop(k, None)
        root = _project(tmp)
        _transcript(root)
        modules = None if modules_all else [("vmc_harness.task.handlers", False)]
        if mode == "inprocess":
            return run_inprocess(root, modules)
        return run_subprocess(root, modules_all)
    finally:
        os.environ.clear()
        os.environ.update(old)
        shutil.rmtree(str(tmp), ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    a = ap.parse_args()
    from vmc_harness.core import config
    budget = config.get("budget.small_task_overhead_s")
    print("python %s · 예산 %ss · %d회 중앙값" % (sys.version.split()[0], budget, a.rounds))
    for modules_all, label in ((False, "task 핸들러만"), (True, "전체 HANDLER_MODULES")):
        for mode in ("inprocess", "subprocess"):
            totals, per = [], {}
            for _ in range(a.rounds):
                rows = _one(mode, modules_all)
                totals.append(sum(t for _, t in rows))
                for ev, t in rows:
                    per.setdefault(ev, []).append(t)
            med = statistics.median(totals)
            detail = ", ".join("%s %.0fms" % (ev, statistics.median(v) * 1000)
                               for ev, v in per.items())
            print("[%s · %s] 합계 %.2fs (%s) — 이벤트 평균: %s" % (
                label, mode, med, "예산 안" if med <= budget else "예산 초과", detail))


if __name__ == "__main__":
    main()
