"""PM 서브에이전트 + 착수 카드 검색 — 한 번의 비동기 호출로 묶는다 (spec 03 §PM, 04 §검색 시점).

- 중·대 작업만. 착수 선언 순간 백그라운드(`vmc-harness task-bg <task_id>`)로 실행, 결과는 로컬 파일 `tasks/<id>.bg.json`
- PM은 새 컨텍스트 `claude -p`(설정 pm.model). 목표·출력 형식만 준다(01 P6 — 절차 금지)
- 작업 에이전트는 기다리지 않는다. **선언 범위 밖 첫 편집 직전에만** handlers가 결과를 기다린다(상한 pm.wait_s)
- 비동기 불가 에이전트(설정 task.pm_sync_agents)·실행 실패면 동기 fallback
- 편의 장치 — 실패하면 조용히 빠진다(01 P8 fail-open)
"""

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from vmc_harness.core import config, events
from vmc_harness.core.clock import now_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.task import state as st

PM_SYSTEM = (
    "너는 이 개발 작업을 맡긴 관리자 입장의 PM이다.\n"
    "목표: 작업의 효과·임팩트를 볼 때 관리자로서 바라는 소요와, 작업 에이전트가 착수 때 낸 예측(에이전트 작업 시간)을 비교해 판정한다.\n"
    "- 바라는 소요와 맞거나 예측이 더 적으면 승인만 한다(의견 없음).\n"
    "- 어느 정도 넘치는 것은 괜찮다 — 승인한다.\n"
    "- 그 이상으로 과하면 어느 부분을 몇 분 안에 끝나길 희망하는지 한 문장으로 적는다.\n"
    "제약: 작업 방법·절차는 지시하지 않는다. 퍼센트·토큰 수는 쓰지 않는다.\n"
    '출력 형식: JSON 한 줄만. {"opinion":"approve"} 또는 {"opinion":"trim","wish":"<부분>은 <N>분 안에 끝나길 희망"}'
)


def bg_path(root, task_id):
    return st.task_path(root, task_id, ".bg")


def read_bg(root, task_id):
    return read_json(bg_path(root, task_id))


def _engine_dir():
    return str(Path(__file__).resolve().parent.parent.parent)


def _env():
    env = dict(os.environ)
    eng = _engine_dir()
    env["PYTHONPATH"] = eng + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def is_sync(root, agent):
    try:
        return agent in (config.get("task.pm_sync_agents", root) or [])
    except KeyError:
        return False


def start(root, task, agent=None):
    """중·대 작업 착수 때. 비동기 실행(실패하면 동기). 돌려주는 값: 'async'|'sync'."""
    task_id = task["task_id"]
    write_json(bg_path(root, task_id), {"task_id": task_id, "status": "running", "started_at": now_iso(),
                                        "declare_rev": task.get("declare_rev", 1)})
    if not is_sync(root, agent or task.get("agent")):
        try:
            from vmc_harness.core.proc import spawn_detached
            spawn_detached([sys.executable, "-m", "vmc_harness", "task-bg", task_id, "--root", str(root)],
                           cwd=str(root), env=_env())
            return "async"
        except Exception:
            pass
    run_bundle(root, task_id)
    return "sync"


def run_bundle(root, task_id):
    """PM과 카드 검색을 같은 프로세스에서 병렬로. 결과 파일을 쓴다."""
    task = st.by_id(root, task_id)
    if not task:
        return None
    out = {"pm": None, "cards": []}
    t0 = time.time()

    def _pm():
        out["pm"] = run_pm(root, task)

    def _cards():
        out["cards"] = run_cards(root, task)

    # 가벼움 예산을 넘으면 꺼진 편의 장치는 부르지 않는다(01 P8 — core.budget)
    from vmc_harness.core import budget
    threads = [threading.Thread(target=f) for dev, f in (("pm", _pm), ("card_search", _cards)) if budget.enabled(root, dev)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    pm = out["pm"] or {}
    rec = {"task_id": task_id, "status": "done", "finished_at": now_iso(), "sec": round(time.time() - t0, 2),
           "declare_rev": task.get("declare_rev", 1),
           "pm": {"called": bool(out["pm"]), "opinion": pm.get("opinion"), "wish": pm.get("wish")},
           "cards": out["cards"], "tokens": int(pm.get("tokens") or 0)}
    write_json(bg_path(root, task_id), rec)
    st.add_overhead(root, task_id, tokens=rec["tokens"])
    try:
        events.log(root, "pm", task_id=task_id, opinion=rec["pm"]["opinion"], called=rec["pm"]["called"],
                   sec=rec["sec"], tokens=rec["tokens"])
        if rec["cards"]:
            events.log(root, "card_search", task_id=task_id, cards=[c.get("id") for c in rec["cards"]],
                       phase="declare")
    except Exception:
        pass
    return rec


def _pm_input(task):
    d = task.get("declare") or {}
    pred = d.get("predict") or {}
    sec = pred.get("agent_sec") or {}
    lines = ["하려는 것: %s" % d.get("intent", ""),
             "건드릴 범위: %s" % ", ".join(d.get("scope") or []),
             "새로 만들 것: %s" % (", ".join(d.get("new") or []) or "없음"),
             "보호 영역 접촉 예상: %s" % ("예" if d.get("protected_touch") else "아니오"),
             "테스트 수정: %s" % ((d.get("test_change") or {}).get("reason") or "없음"),
             "에이전트 작업 시간 예측(분) — 공격적 %s / 도전적 %s / 안정적 %s" % tuple(
                 _min(sec.get(k)) for k in ("p30", "p50", "p80"))]
    return "\n".join(lines)


def _min(sec):
    try:
        return "%.0f" % max(1.0, float(sec) / 60.0)
    except (TypeError, ValueError):
        return "?"


def parse_json_obj(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


def run_pm(root, task):
    try:
        from vmc_harness.core.llm import claude_p
        res = claude_p(_pm_input(task), model=config.get("pm.model", root), system=PM_SYSTEM,
                       timeout=config.get("pm.timeout_s", root))
    except Exception:
        return None
    if not res:
        return None
    obj = parse_json_obj(res.get("text")) or {}
    opinion = obj.get("opinion") if obj.get("opinion") in ("approve", "trim") else "approve"
    wish = (obj.get("wish") or "").strip() or None
    if opinion == "approve":
        wish = None
    return {"opinion": opinion, "wish": wish, "tokens": res.get("tokens", 0), "sec": res.get("sec", 0)}


def card_query(task):
    d = task.get("declare") or {}
    return "%s / 범위: %s" % (d.get("intent", ""), ", ".join(d.get("scope") or []))


def run_cards(root, task):
    try:
        from vmc_harness.brain.search import search
    except ImportError:
        return []
    try:
        cards = search(card_query(task), top_k=None, use_llm=True) or []
    except Exception:
        return []
    out = []
    for c in cards:
        if not isinstance(c, dict):
            continue
        out.append({"id": c.get("id") or c.get("card_id") or c.get("path") or c.get("title"),
                    "title": c.get("title"), "judgment": c.get("judgment") or c.get("summary"),
                    "not_when": c.get("not_when")})
    return out


def wait(root, task_id, timeout_s):
    """결과가 나올 때까지 최대 timeout_s 기다린다. 끝난 결과 또는 None."""
    deadline = time.time() + float(timeout_s)
    poll = 0.2
    while True:
        rec = read_bg(root, task_id)
        if rec and rec.get("status") == "done":
            return rec
        if not rec or time.time() >= deadline:
            return None
        time.sleep(poll)


def opinion_text(rec):
    """PM 의견 주입 문구(trim일 때만). 승인이면 None — 조용히 승인."""
    pm = (rec or {}).get("pm") or {}
    if pm.get("opinion") != "trim" or not pm.get("wish"):
        return None
    return "PM 의견: %s" % pm["wish"]


def cards_text(cards):
    if not cards:
        return None
    lines = ["착수 참고 — 브레인 카드(상황이 맞을 때만 따른다):"]
    for c in cards:
        s = "- %s" % (c.get("title") or c.get("id"))
        if c.get("judgment"):
            s += ": %s" % c["judgment"]
        if c.get("not_when"):
            s += " (적용 안 함: %s)" % c["not_when"]
        lines.append(s)
    return "\n".join(lines)
