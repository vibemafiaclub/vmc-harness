"""원격 서버(apps/api) 호출. 서버로 가는 건 라이선스·버전·잠금뿐 — 고객 작업 데이터 0바이트(01 P9)."""

from vmc_harness.dist import settings
from vmc_harness.dist import shell


def base_url():
    """환경변수 VMC_HARNESS_SERVER(로컬 E2E·사내 프록시) > config server.base_url."""
    return shell.base_url(settings.get("server.base_url"))


def call(method, path, body=None, auth=True, timeout=None):
    """(status, json). 서버에 못 닿으면 (0, {"error": "unreachable"}), 키가 없으면 (0, {"error": "no_license"})."""
    key = None
    if auth:
        key = shell.load_key()
        if not key:
            return 0, {"error": "no_license"}
    if timeout is None:
        timeout = settings.get("server.call_timeout_s")
    try:
        return shell.api(method, path, body=body, key=key, timeout=timeout, base=base_url())
    except shell.Unreachable as e:
        return 0, {"error": "unreachable", "detail": str(e)}
