"""비밀값 마스킹 (spec 01 P9 — 쓰는 시점에 마스킹). 마스킹은 이 모듈 하나로(문자열 `mask`, JSON 값 `mask_obj`)."""

import re

_KEYS = r"password|passwd|pwd|secret|client_secret|token|access_token|refresh_token|id_token|api[_-]?key|authorization"
SECRET_KEY = re.compile(r"(?i)^(?:%s)$" % _KEYS)

# (패턴, 가릴 그룹 번호 — 0이면 전체)
_PATTERNS = [
    # JSON 형태 "password": "x" (빈 값·공백 포함 값도)
    (re.compile(r'(?i)"(?:%s)"\s*:\s*"((?:[^"\\]|\\.)*)"' % _KEYS), 1),
    # Authorization: Bearer xxx / Basic xxx
    (re.compile(r"(?i)\b(?:bearer|basic)\s+([A-Za-z0-9._~+/=\-]{8,})"), 1),
    # 셸·설정 형태 password=x / token: x / api_key='x'
    (re.compile(r"(?i)\b(?:%s)\s*[=:]\s*['\"]?(?!(?:bearer|basic)\b)([^\s;,'\"]+)" % _KEYS), 1),
    # 연결 문자열 PWD=...; / Password=...;
    (re.compile(r"(?i)\b(?:PWD|Password)=([^;]+)"), 1),
    (re.compile(r"vmh_[A-Za-z0-9_\-]{20,}"), 0),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), 0),
]


def _sub(p, g, text):
    def rep(m):
        if g == 0:
            return "****"
        s, e = m.span(g)
        if s == e:
            return m.group(0)
        return text[m.start():s] + "****" + text[e:m.end()]
    return p.sub(rep, text)


def mask(text):
    if not text or not isinstance(text, str):
        return text
    out = text
    for p, g in _PATTERNS:
        out = _sub(p, g, out)
    return out


def mask_obj(v):
    """JSON 값(dict·list·str) 마스킹 — 비밀 키 이름의 값은 통째로, 문자열은 mask()."""
    if isinstance(v, str):
        return mask(v)
    if isinstance(v, dict):
        return {k: ("****" if isinstance(k, str) and SECRET_KEY.match(k) and isinstance(x, (str, int, float)) and x != ""
                    else mask_obj(x)) for k, x in v.items()}
    if isinstance(v, list):
        return [mask_obj(x) for x in v]
    return v
