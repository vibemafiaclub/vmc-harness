"""게이트 공용: 메시지(01 P5·P6), 경로 분류(동작·검증물), 프로젝트 덮어쓰기."""

from vmc_harness.core import config, inject
from vmc_harness.core.project import load_config
from vmc_harness.gates import globs
from vmc_harness.hooks.contract import HookResult


def msg(kind, text, fallback=None):
    """허용 목록 주입 문구. 경로 이름에 'token' 같은 단어가 섞여 압박 필터에 걸리면 경로 없는 문장(fallback)으로."""
    m = inject.message(kind, text)
    if m is None and fallback:
        m = inject.message(kind, fallback)
    return m


def block(text, fallback=None, kind="gate_violation"):
    reason = msg(kind, text, fallback) or inject.message(kind, "하네스 게이트가 막음")
    return HookResult(block=True, reason=reason)


def short_list(items):
    n = int(config.get("gates.reason_max_files"))
    items = list(items)
    s = ", ".join(items[:n])
    if len(items) > n:
        s += " 외 %d개" % (len(items) - n)
    return s


def _facts(root):
    try:
        from vmc_harness.bootstrap import facts
        return facts.load(str(root)) or {}
    except Exception:
        return {}


def behavior_globs(root):
    """{'page': [...], 'api': [...], 'db': [...]} — 프로젝트 config.json > 부트스트랩 facts > 기본값."""
    out = {"page": config.get("gates.page_globs"), "api": config.get("gates.api_globs"), "db": config.get("gates.db_globs")}
    for src in (_facts(root).get("behavior_globs"), load_config(root).get("behavior_globs")):
        if isinstance(src, dict):
            for k in out:
                if isinstance(src.get(k), list):
                    out[k] = src[k]
    return out


def verification_globs(root):
    tg, eg = config.get("gates.test_globs"), config.get("gates.expected_globs")
    for src in (_facts(root).get("verification_globs"), load_config(root).get("verification_globs")):
        if isinstance(src, dict):
            tg = src.get("tests", tg) if isinstance(src.get("tests"), list) else tg
            eg = src.get("expected", eg) if isinstance(src.get("expected"), list) else eg
    return tg, eg


def is_verification(root, rel):
    tg, eg = verification_globs(root)
    return bool(globs.match_any(rel, tg) or globs.match_any(rel, eg))


def behavior_kind(root, rel, bg=None):
    """page|api|db|None. 테스트·하네스 파일은 동작 변경이 아니다."""
    if not rel or rel.startswith(".harness/") or is_verification(root, rel):
        return None
    bg = bg or behavior_globs(root)
    for k in ("db", "api", "page"):
        if globs.match_any(rel, bg[k]):
            return k
    return None


def facts(root):
    return _facts(root)


def log_event(root, kind, **fields):
    """core.events.log — 실패해도 게이트·명령을 멈추지 않는다(Codex 샌드박스 안 명령은 ~/.vmc-harness에 못 쓴다, 실측)."""
    try:
        from vmc_harness.core import events
        return events.log(str(root), kind, **fields)
    except Exception:
        return None


def notify(root, kind, level, text, **kw):
    try:
        from vmc_harness.core import notices
        return notices.add(str(root), kind, level, text, **kw)
    except Exception:
        return None


def _write_text(path, text):
    """하네스가 만드는 파일만 LF로 쓴다(3.9 호환 — Path.write_text(newline=)은 3.10+).
    고객 파일(AGENTS.md·CLAUDE.md·.codex/config.toml 등)은 core.textfile로 — 원래 줄끝·BOM을 바이트 그대로 지킨다."""
    with open(str(path), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
