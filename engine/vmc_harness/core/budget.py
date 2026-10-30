"""가벼움 예산 — 넘으면 편의 장치가 비싼 것부터 스스로 빠진다 (spec 01 P8·P5, 03 §작업 크기).

- 실측: 팀 장부 작업 기록(`harness_overhead`)의 최근 `budget.window_tasks`건
  · 소 작업 하네스 오버헤드 중위값 > `budget.small_task_overhead_s`
  · 작업당 게이트 시간(gate_sec) 중위값 > `budget.gate_time_per_task_s`
  둘 중 하나라도 넘으면 아직 켜진 편의 장치 중 가장 비싼 것 하나를 끈다(하루 1회 판정 — 한 번에 하나씩)
- 편의 장치(비싼 순): PM → 과설계 LLM 판정 → 카드 검색(착수 묶음) → 렌더링 재작성. 안전 장치는 여기 없다(줄이지 않는다)
- 사람 승인 없음. 에이전트에게 숫자를 보이지 않는다(P5). 끈 사실은 events(`convenience_disabled`)에만
- 상태는 PC 로컬(`~/.vmc-harness/projects/<id>/budget.json`). 커널 버전이 바뀌면 다시 모두 켠다(새 릴리스가 비용을 고쳤을 수 있다)
- 각 영역은 `enabled(root, 장치)`만 읽는다. 읽기 실패면 켜진 것으로(편의 장치 fail-open)
"""

from vmc_harness import KERNEL_VERSION
from vmc_harness.core import config
from vmc_harness.core.clock import now, now_iso
from vmc_harness.core.jsonio import read_json, write_json

DEVICES = ("pm", "overdesign_llm", "card_search", "render_rewrite")


def _path(root):
    from vmc_harness.core.project import local_dir
    return local_dir(root) / "budget.json"


def state(root):
    try:
        st = read_json(_path(root), {}) or {}
    except Exception:
        return {"disabled": {}}
    if st.get("kernel_version") != KERNEL_VERSION:
        st = {"kernel_version": KERNEL_VERSION, "disabled": {}}
    st.setdefault("disabled", {})
    return st


def enabled(root, device):
    """편의 장치가 켜져 있나. 하네스 미설치·읽기 실패면 True."""
    if not root:
        return True
    try:
        return device not in state(root).get("disabled", {})
    except Exception:
        return True


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def measure(root):
    """{'small_overhead_s','small_n','gate_s','gate_n'} — 최근 작업 기록의 중위값(없으면 None)."""
    from vmc_harness.core import ledger
    win = int(config.get("budget.window_tasks", root))
    tasks = sorted(ledger.read_tasks(root), key=lambda r: r.get("ended_at") or "")
    small = [float((r.get("harness_overhead") or {}).get("sec") or 0) for r in tasks if r.get("size") == "small"][-win:]
    gate = [float((r.get("harness_overhead") or {}).get("gate_sec")) for r in tasks
            if (r.get("harness_overhead") or {}).get("gate_sec") is not None][-win:]
    return {"small_overhead_s": _median(small), "small_n": len(small), "gate_s": _median(gate), "gate_n": len(gate)}


def check(root, force=False):
    """하루 1회. 예산을 넘었으면 가장 비싼 켜진 장치 하나를 끈다. 끈 장치 이름 또는 None."""
    st = state(root)
    today = now().strftime("%Y-%m-%d")
    if not force and st.get("checked_on") == today:
        return None
    st["checked_on"] = today
    m = measure(root)
    need = int(config.get("budget.window_tasks", root))
    over = []
    if m["small_n"] >= need and m["small_overhead_s"] is not None \
            and m["small_overhead_s"] > float(config.get("budget.small_task_overhead_s", root)):
        over.append("small_task_overhead")
    if m["gate_n"] >= need and m["gate_s"] is not None \
            and m["gate_s"] > float(config.get("budget.gate_time_per_task_s", root)):
        over.append("gate_time_per_task")
    off = None
    if over:
        for d in DEVICES:
            if d not in st["disabled"]:
                off = d
                break
    if off:
        st["disabled"][off] = {"at": now_iso(), "over": over, "measured": m}
    write_json(_path(root), st)
    if off:
        try:
            from vmc_harness.core import events
            events.log(root, "convenience_disabled", device=off, over=over, measured=m)
        except Exception:
            pass
    return off
