import datetime as _dt

# 시험용 가짜 시계(시나리오 테스트가 며칠치 흐름을 압축할 때만) — 운영 코드는 건드리지 않는다
_fake = None


def set_fake_now(value):
    """value: aware datetime 또는 None(실제 시계로 복귀)."""
    global _fake
    _fake = value


def now():
    if _fake is not None:
        return _fake
    return _dt.datetime.now().astimezone()


def now_iso():
    return now().isoformat(timespec="seconds")


def parse_iso(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
