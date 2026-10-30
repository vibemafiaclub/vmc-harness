"""자동 업데이트(spec 10). 세션 시작 때 하루 1회 정도 서버 latest를 보고 새 버전이면 받는다.

- 실행 때마다 키를 검사하지 않는다. 서버에 못 닿거나 키가 거부되면(계약 종료) 지금 버전 그대로 조용히 진행
- `update.auto`가 꺼져 있으면(PC 단위 덮어쓰기 `update --auto off`) 세션 시작 확인을 하지 않는다. 수동 `update`는 그대로
- 세션 시작 훅은 기다리지 않는다 — 백그라운드 프로세스로 `update --quiet`를 띄우고, 새 커널은 다음 실행부터 쓰인다
"""

import os
import sys
from pathlib import Path

from vmc_harness.core import paths
from vmc_harness.core.clock import now, now_iso, parse_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.dist import server, settings, shell


def state_file():
    return paths.home() / "update.json"


def auto_enabled():
    """세션 시작 자동 확인을 할지. PC 단위 덮어쓰기(update.json "auto")가 있으면 그것, 없으면 설정 update.auto."""
    v = (read_json(state_file(), {}) or {}).get("auto")
    return v if isinstance(v, bool) else bool(settings.get("update.auto"))


def set_auto(on):
    """`update --auto on|off` — 이 PC의 자동 확인을 켜고 끈다."""
    return mark(auto=bool(on), auto_set_at=now_iso())


def due():
    st = read_json(state_file(), {}) or {}
    last = parse_iso(st.get("checked_at"))
    if not last:
        return True
    return (now() - last).total_seconds() >= settings.get("update.check_interval_h") * 3600


def mark(**fields):
    st = read_json(state_file(), {}) or {}
    st.update(fields)
    write_json(state_file(), st)
    return st


def run():
    """지금 확인·설치. 결과 dict(shell.update_now 형식)."""
    mark(checked_at=now_iso())
    r = shell.update_now(base=server.base_url(),
                         check_timeout=settings.get("update.check_timeout_s"),
                         download_timeout=settings.get("update.download_timeout_s"),
                         keep=settings.get("update.keep_versions"))
    mark(last_result=r.get("status"), version=shell.current_version(), result_at=now_iso())
    return r


def spawn_background():
    """세션 시작 훅용: 기다리지 않고 별도 프로세스로 run(). 띄웠으면 True."""
    from vmc_harness.core.proc import spawn_detached
    import vmc_harness
    if not shell.load_key():
        return False
    mark(checked_at=now_iso())  # 동시에 열린 세션이 여러 번 띄우지 않게 먼저 찍는다
    engine = str(Path(vmc_harness.__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = engine + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["VMC_HARNESS_NESTED"] = "1"  # 자식이 훅을 다시 부르지 않게
    spawn_detached([sys.executable, "-m", "vmc_harness", "update", "--quiet"], env=env)
    return True
