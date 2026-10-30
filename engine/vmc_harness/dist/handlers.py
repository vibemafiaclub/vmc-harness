"""훅 핸들러(dist) — 세션 시작 때 자동 업데이트 확인을 백그라운드로 띄운다(update.auto가 꺼져 있으면 안 띄운다). 편의 장치라 실패해도 세션을 막지 않는다."""


def on_session_start(ctx):
    from vmc_harness.core.llm import nested
    from vmc_harness.dist import updater
    if nested():
        return None
    try:
        if updater.auto_enabled() and updater.due():
            updater.spawn_background()
    except Exception:
        pass
    return None
