"""설정 접근 (spec 11). defaults.toml의 작은 부분집합(TOML)만 읽는다 — 표준 라이브러리만(3.9 호환)."""

import re
from pathlib import Path

from vmc_harness.core.jsonio import read_json
from vmc_harness.core import paths

DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "config" / "defaults.toml"
PROFILES = ("standard", "gxp", "prototype", "solo")
_cache = {}


def _parse_value(raw):
    raw = raw.strip()
    if raw in ("true", "false"):
        return raw == "true"
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1].encode("utf-8").decode("unicode_escape") if "\\" in raw else raw[1:-1]
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [_parse_value(x) for x in _split_list(inner)] if inner else []
    if re.fullmatch(r"-?\d+", raw.replace("_", "")):
        return int(raw.replace("_", ""))
    return float(raw.replace("_", ""))


def _split_list(s):
    out, buf, q = [], "", False
    for ch in s:
        if ch == '"':
            q = not q
        if ch == "," and not q:
            out.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf)
    return out


def _strip_comment(line):
    q = False
    for i, ch in enumerate(line):
        if ch == '"':
            q = not q
        elif ch == "#" and not q:
            return line[:i], line[i + 1:]
    return line, ""


def parse_toml(text):
    """{flat_key: value}, {flat_key: meta_comment}"""
    values, meta, section = {}, {}, ""
    for line in text.splitlines():
        body, comment = _strip_comment(line)
        body = body.strip()
        if not body:
            continue
        if body.startswith("[") and body.endswith("]"):
            section = body[1:-1].strip()
            continue
        k, _, v = body.partition("=")
        key = (section + "." if section else "") + k.strip()
        values[key] = _parse_value(v)
        meta[key] = comment.strip()
    return values, meta


def defaults():
    """defaults.toml + 영역별 config/<영역>.toml(병렬 작업 충돌 방지용). 같은 키가 두 파일에 있으면 오류."""
    if "defaults" not in _cache:
        values, meta = parse_toml(DEFAULTS_PATH.read_text(encoding="utf-8"))
        for extra in sorted(DEFAULTS_PATH.parent.glob("*.toml")):
            if extra == DEFAULTS_PATH:
                continue
            v, m = parse_toml(extra.read_text(encoding="utf-8"))
            dup = set(v) & set(values)
            if dup:
                raise ValueError("설정 키 중복: %s (%s)" % (sorted(dup), extra.name))
            values.update(v)
            meta.update(m)
        _cache["defaults"] = (values, meta)
    return _cache["defaults"]


def project_profile(root=None):
    root = root or paths.find_project_root()
    if not root:
        return "standard"
    cfg = read_json(paths.harness_dir(root) / "config.json", {}) or {}
    p = cfg.get("profile", "standard")
    return p if p in PROFILES else "standard"


def get(key, root=None, profile=None):
    """설정값. 프로파일 덮어쓰기 적용. 없는 키는 KeyError(오타를 조용히 넘기지 않는다)."""
    values, _ = defaults()
    profile = profile or project_profile(root)
    over = "profile.%s.%s" % (profile, key)
    if over in values:
        return values[over]
    return values[key]


def meta(key):
    return defaults()[1].get(key, "")


def all_keys():
    return [k for k in defaults()[0] if not k.startswith("profile.")]
