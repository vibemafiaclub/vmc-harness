"""에이전트별 훅 입력 정규화·출력 형식."""

from vmc_harness.hooks.adapters import claude, codex
from vmc_harness.hooks.adapters.common import detect_agent

ADAPTERS = {"claude": claude, "codex": codex}


def get(agent):
    return ADAPTERS.get(agent, claude)


__all__ = ["get", "detect_agent", "ADAPTERS"]
