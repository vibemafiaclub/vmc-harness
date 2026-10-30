"""brain 훅 핸들러. 세션 시작에 원격 브레인 동기화(git pull --ff-only, 상한 brain.pull_timeout_s)를 분리 실행한다.
원격이 없으면 아무것도 안 한다. 실패하면 로컬 사본으로 계속(편의 장치)."""

from vmc_harness.core import llm


def on_session_start(ctx):
    if llm.nested():
        return None
    from vmc_harness.brain import sync
    sync.background()
    return None
