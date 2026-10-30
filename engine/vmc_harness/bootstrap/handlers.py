"""bootstrap 영역 훅 처리 (hooks/contract.py). 아무것도 주입하지 않고, 아무것도 막지 않는다(편의 장치 — fail-open).

- SessionStart: 그 기간(기본 하루) 재진단이 없으면 백그라운드로 시작(bootstrap/weekly.py)
"""


def on_session_start(ctx):
    if not ctx.root:
        return None
    try:
        from vmc_harness.bootstrap import weekly
        weekly.ensure(ctx.root)
    except Exception:
        pass
    return None
