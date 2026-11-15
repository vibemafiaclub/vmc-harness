"""규칙 블록 (L1 — spec 02 §규칙 블록).

원본 `.harness/rules.json` → 프로젝트 AGENTS.md의 표시 블록(`<!-- vmc-harness:begin -->` ~ `<!-- vmc-harness:end -->`)만 렌더.
고객의 기존 내용은 한 글자도 바꾸지 않는다. CLAUDE.md가 없으면 `@AGENTS.md` 한 줄로 만든다.

rules.json 형식:
    {"rules": [
       {"id": "r-...", "basis": {"type": "explicit_request", "quote": "<사람 원문 그대로>",
                                 "source": {"session": "...", "date": "2026-10-23", "area": "be/"}}, "action_id": "A-..."},
       {"id": "r-...", "basis": {"type": "machine_fact", "command": ["git", "config", "..."], "pattern": "<정규식, 선택>"},
        "text": "운영 DB 이름: {value}", "value": "...", "checked_at": "..."}],
     "tools": ["...하네스 도구가 있다는 사실 한 줄..."]}
- 사람 명시 요청 규칙은 원문 그대로 + 출처(렌더링·요약 금지, 01 P3)
- 진단 제안 [추가]로 넣은 규칙은 `rule_text`(제안에 함께 보인 규칙 문장 — 사람이 [추가]로 고른 문장)를 싣는다.
  `rule_text`가 없는 옛 진단 규칙(제목 = "~를 규칙으로 올리기" 같은 할 일)은 블록에서 빼고 알림 한 줄로 재확인을 부탁한다
- AGENTS.md·CLAUDE.md는 원래 줄끝(CRLF/LF)·BOM을 바이트 그대로 지킨다(core.textfile) — 바뀌는 것은 표시 블록뿐
- 기계 사실 규칙은 읽어 온 명령과 함께. `refresh_machine_facts`가 세션 시작 때 가끔 다시 실행해 값이 바뀌면 규칙도 바꾼다
- 블록 크기는 budget.rule_block_tokens 안(넘으면 쓰지 않고 RuleBudgetError)
"""

import re
import shlex
from pathlib import Path

from vmc_harness.core import config, paths, textfile
from vmc_harness.core.clock import now, now_iso, parse_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.proc import run
from vmc_harness.gates.common import _write_text, log_event, notify

BEGIN = "<!-- vmc-harness:begin -->"
END = "<!-- vmc-harness:end -->"
_BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"(?:\r?\n)?", re.S)
_CLAUDE_IMPORT = re.compile(r"(?:^|\s)@(?:\./)?AGENTS\.md(?:\s|$)")

DEFAULT_TOOLS = [
    "`vmc-harness declare` — 착수 선언(하려는 것·범위·예측)을 남기는 도구가 있다. 파일 편집은 선언이 있어야 통과한다",
    "`vmc-harness report` — 완료 보고 맨 위 블록(범위 밖 변경·영향·증거·승인)을 출력하는 도구가 있다",
    "`vmc-harness evidence` — 스크린샷·API 호출·테스트 실행을 실제 동작 증거로 남기는 도구가 있다",
    "`vmc-harness impact <파일|파일:심볼>` — 바뀐 코드의 호출처·영향 페이지를 보여 주는 도구가 있다",
]
DB_TOOL = "`vmc-harness db query \"<SELECT>\"` — 개발 DB 읽기 전용 조회 도구가 있다"


class RuleBudgetError(ValueError):
    pass


def rules_path(root):
    return paths.harness_dir(root) / "rules.json"


def load(root):
    data = read_json(rules_path(root), None) or {}
    data.setdefault("rules", [])
    return data


def save(root, data):
    write_json(rules_path(root), data)


def estimate_tokens(text, root=None):
    per = int(config.get("gates.token_ascii_chars", root))
    ascii_n = sum(1 for c in text if ord(c) < 128)
    return (ascii_n + per - 1) // per + (len(text) - ascii_n)


def _cmd_str(cmd):
    if isinstance(cmd, list):
        try:
            return shlex.join([str(c) for c in cmd])
        except Exception:
            return " ".join(str(c) for c in cmd)
    return str(cmd)


def _db_configured(root):
    try:
        from vmc_harness.core.project import local_dir
        if (local_dir(root) / "db.json").is_file():
            return True
    except Exception:
        pass
    try:
        from vmc_harness.gates.common import facts
        return bool((facts(root) or {}).get("db"))
    except Exception:
        return False


def _d(x):
    """dict가 아니면 빈 dict — 손으로 고쳤거나 옛 형식(basis.source가 문자열 등)인 rules.json도 읽는다(현장 결함)."""
    return x if isinstance(x, dict) else {}


def _rules(data):
    """규칙 항목 중 dict만(문자열 등 다른 모양은 건너뛴다 — 파일에는 그대로 둔다)."""
    return [r for r in (data.get("rules") or []) if isinstance(r, dict)]


def _basis(r):
    return _d(r.get("basis"))


def _from_diagnose(r):
    return _d(_basis(r).get("source")).get("via") == "diagnose accept"


def needs_rule_text(r):
    """진단 [추가]로 들어갔는데 규칙 문장이 없는 옛 규칙(제목 = 할 일 문장) — 블록에 싣지 않는다."""
    return _from_diagnose(r) and not (r.get("rule_text") or "").strip()


def render_block(root, data=None):
    data = data or load(root)
    lines = [BEGIN, "## 하네스 규칙 (vmc-harness가 관리 — 이 블록 밖 내용은 그대로)", ""]
    human = [r for r in _rules(data) if _basis(r).get("type") == "explicit_request" and r.get("enabled", True)
             and not needs_rule_text(r)]
    facts_r = [r for r in _rules(data) if _basis(r).get("type") == "machine_fact" and r.get("enabled", True)]
    if facts_r:
        lines.append("### 환경 사실")
        for r in facts_r:
            text = (r.get("text") or "{value}").replace("{value}", str(r.get("value", "")))
            cmd = _cmd_str(_basis(r).get("command") or "")
            lines.append("- %s (읽은 명령: `%s`)" % (text, cmd) if cmd else "- %s" % text)
        lines.append("")
    if human:
        lines.append("### 사람이 정한 규칙 (원문)")
        for r in human:
            b = _basis(r)
            src = _d(b.get("source"))
            where = " · ".join(str(x) for x in (src.get("date"), src.get("area"), ("세션 " + str(src["session"])[:8]) if src.get("session") else None) if x)
            if r.get("rule_text"):
                lines.append("- %s — 출처: %s" % (r["rule_text"].strip(), " · ".join(x for x in (where, "진단 제안") if x)))
            else:
                lines.append("- \"%s\" — 출처: %s" % (b.get("quote", ""), where or "-"))
        lines.append("")
    auto = [r for r in _rules(data) if _basis(r).get("type") == "diagnosis_auto" and r.get("enabled", True)
            and (r.get("rule_text") or "").strip()]
    if auto:
        lines.append("### 진단이 자동으로 넣은 규칙 (시험 중 — 되돌리기 `vmc-harness revert <조치>`)")
        for r in auto:
            lines.append("- %s — 조치 %s" % (r["rule_text"].strip(), r.get("action_id") or "-"))
        lines.append("")
    tools = list(data.get("tools") or DEFAULT_TOOLS)
    if _db_configured(root) and DB_TOOL not in tools:
        tools.append(DB_TOOL)
    # 부트스트랩이 같은 도구 사실을 규칙으로 이미 넣었으면 겹쳐 쓰지 않는다
    rule_text = " ".join((r.get("text") or "") for r in facts_r)
    tools = [t for t in tools if t.split("`")[1].split(" <")[0].split(" \"")[0] not in rule_text]
    lines.append("### 하네스 도구")
    lines.extend("- %s" % t for t in tools)
    lines.append("- 커널 원칙(읽기만): `vmc-harness principles`")
    lines.append(END)
    return "\n".join(lines) + "\n"


def apply_block(existing, block, eol="\n"):
    """기존 AGENTS.md 내용에 블록을 넣거나 바꾼다(블록 밖은 바이트 그대로). 블록은 파일 줄끝(eol)으로 맞춘다."""
    block = textfile.to_eol(block, eol)
    if existing is None:
        return block
    if _BLOCK.search(existing):
        return _BLOCK.sub(lambda m: block, existing, count=1)
    sep = "" if existing.endswith(eol + eol) else (eol if existing.endswith("\n") else eol + eol)
    return existing + sep + block


_BLOCK_WITH_SEP = re.compile(r"(?<=\n)\r?\n" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"(?:\r?\n)?", re.S)


def remove_block(existing):
    out = _BLOCK_WITH_SEP.sub("", existing or "", count=1)
    return _BLOCK.sub("", out, count=1)


def render(root):
    """AGENTS.md 블록 갱신 + CLAUDE.md(@AGENTS.md) 보장. 반환 {'tokens','changed','path'}."""
    root = Path(root)
    block = render_block(root)
    tokens = estimate_tokens(block, root)
    limit = int(config.get("budget.rule_block_tokens", root))
    if tokens > limit:
        raise RuleBudgetError("규칙 블록 토큰 예산 초과(%d > %d)" % (tokens, limit))
    agents = root / "AGENTS.md"
    old, fmt = textfile.read(agents)
    new = apply_block(old, block, fmt["eol"])
    changed = new != old
    if changed:
        textfile.write(agents, new, fmt)
    claude = root / "CLAUDE.md"
    if not claude.exists():
        _write_text(claude, "@AGENTS.md\n")
    _notify_hidden(root)
    try:
        import hashlib
        log_event(str(root), "rules_rendered", tokens=tokens, block_hash=hashlib.sha256(block.encode("utf-8")).hexdigest()[:16])
    except Exception:
        pass
    return {"tokens": tokens, "changed": changed, "path": str(agents)}


def unrender(root):
    agents = Path(root) / "AGENTS.md"
    t, fmt = textfile.read(agents)
    if t is not None:
        n = remove_block(t)
        if n != t:
            textfile.write(agents, n, fmt)
            return True
    return False


def ensure_claude_import(root):
    """CLAUDE.md가 있는데 `@AGENTS.md`를 불러오지 않으면 끝에 한 줄 덧붙인다(원래 줄끝·BOM 유지) — Claude Code 세션도
    하네스 규칙 블록을 읽게(B5). 반환: "added" | "present" | "missing"(CLAUDE.md 없음 — render가 만든다)."""
    p = Path(root) / "CLAUDE.md"
    t, fmt = textfile.read(p)
    if t is None:
        return "missing"
    if _CLAUDE_IMPORT.search(t):
        return "present"
    eol = fmt["eol"]
    sep = "" if not t or t.endswith("\n") else eol
    textfile.write(p, t + sep + "@AGENTS.md" + eol, fmt)
    log_event(str(root), "claude_import_added")
    return "added"


def remove_claude_import(text):
    """ensure_claude_import가 덧붙인 마지막 줄만 걷어 낸다(remove). 사람이 옮겼거나 바꿨으면 그대로."""
    for tail in ("@AGENTS.md\r\n", "@AGENTS.md\n"):
        if text.endswith(tail) and (len(text) == len(tail) or text[-len(tail) - 1] == "\n"):
            return text[:-len(tail)]
    return text


def _notify_hidden(root):
    """rule_text 없는 옛 진단 규칙 — 블록에서 뺐다는 알림을 규칙마다 한 번(brief에 한 줄)."""
    data = load(root)
    todo = [r for r in _rules(data) if needs_rule_text(r) and r.get("enabled", True) and not r.get("hidden_notified_at")]
    if not todo:
        return
    titles = ", ".join("'%s'" % (_basis(r).get("quote") or "")[:40] for r in todo[:3])
    notify(str(root), "rule_needs_text", "flow",
           "진단 제안으로 넣은 규칙 %d개(%s)는 규칙 문장이 아니라 할 일 제목이라 AGENTS.md 규칙 블록에서 뺐어요. "
           "규칙으로 둘 문장을 AI 대화에서 그대로 말씀해 주시면 그 원문으로 다시 넣어요" % (len(todo), titles),
           refs={"rule_ids": [r["id"] for r in todo]})
    for r in todo:
        r["hidden_notified_at"] = now_iso()
    save(root, data)


# ---- 규칙 추가 ----

def _rid():
    import os
    return "r-%s" % os.urandom(3).hex()


def add_explicit(root, quote, source, action_id=None, rule_text=None, requires=None, basis=None):
    """사람 명시 요청 규칙 — 원문 그대로 + 출처. 진단 [추가]면 rule_text(제안에 보인 규칙 문장)를 함께 —
    requires(기계 확인 전제조건)가 있으면 꺼진 채 두고 조건이 맞으면 bootstrap.requires.enable_ready가 켠다.
    basis(진단 자동 반영 — diagnosis_auto)를 주면 그 근거로 남는다 — 블록에는 '사람이 정한 규칙'이 아니라 따로 싣는다."""
    if not (quote or "").strip():
        raise ValueError("규칙 원문 없음")
    if not (source or {}).get("date"):
        raise ValueError("규칙 출처(날짜) 없음")
    data = load(root)
    b = dict(basis, quote=quote, source=source) if basis else {"type": "explicit_request", "quote": quote, "source": source}
    r = {"id": _rid(), "basis": b,
         "action_id": action_id, "created_at": now_iso(), "enabled": not requires}
    if rule_text:
        r["rule_text"] = rule_text.strip()
    if requires:
        r["requires"] = list(requires)
        r["disabled_reason"] = "requires"
    data["rules"].append(r)
    _check_budget(root, data)
    save(root, data)
    return r


def add_machine_fact(root, command, text, pattern=None, action_id=None):
    """기계 사실 규칙 — 명령을 지금 실행해 값을 읽고 명령과 함께 저장."""
    value = read_fact(root, command, pattern)
    if value is None:
        raise ValueError("기계 사실 명령 실행 실패: %s" % _cmd_str(command))
    data = load(root)
    r = {"id": _rid(), "basis": {"type": "machine_fact", "command": command, "pattern": pattern},
         "text": text, "value": value, "checked_at": now_iso(), "action_id": action_id, "enabled": True}
    data["rules"].append(r)
    _check_budget(root, data)
    save(root, data)
    return r


def _check_budget(root, data):
    t = estimate_tokens(render_block(root, data), root)
    limit = int(config.get("budget.rule_block_tokens", root))
    if t > limit:
        raise RuleBudgetError("규칙 블록 토큰 예산 초과(%d > %d)" % (t, limit))


def read_fact(root, command, pattern=None):
    args = command if isinstance(command, list) else shlex.split(command)
    import shutil
    exe = shutil.which(args[0]) or args[0]
    rc, out, _ = run([exe] + list(args[1:]), cwd=str(root), timeout=config.get("gates.fact_command_timeout_s", root))
    if rc != 0:
        return None
    out = out.strip()
    if pattern:
        m = re.search(pattern, out, re.M)
        if not m:
            return None
        out = m.group(1) if m.groups() else m.group(0)
    return out


def _checked_path(root):
    return paths.cache_dir(root) / "gates" / "rules-checked.json"


def refresh_machine_facts(root, force=False):
    """세션 시작 때 가끔(gates.fact_refresh_hours) 기계 사실 명령을 다시 실행. 값이 바뀌면 규칙·블록 갱신. 바뀐 규칙 id 목록.
    마지막 확인 시각은 캐시에 둔다(값이 안 바뀌면 L1 파일을 건드리지 않는다)."""
    data = load(root)
    hours = float(config.get("gates.fact_refresh_hours", root))
    checked = read_json(_checked_path(root), {}) or {}
    changed = []
    for r in _rules(data):
        b = _basis(r)
        if b.get("type") != "machine_fact" or not b.get("command") or not r.get("enabled", True):
            continue
        if "value" not in r:
            continue   # 값 칸이 없는 옛 형식(문장에 값이 박힘) — 다시 읽어도 바꿀 자리가 없다
        if not r.get("id"):
            continue
        last = parse_iso(checked.get(r["id"]) or r.get("checked_at"))
        if not force and last and (now() - last).total_seconds() < hours * 3600:
            continue
        v = read_fact(root, b["command"], b.get("pattern"))
        checked[r["id"]] = now_iso()
        if v is None:
            log_event(str(root), "rule_fact_failed", rule_id=r["id"])
            continue
        if v != r.get("value"):
            r["value"] = v
            r["checked_at"] = now_iso()
            changed.append(r["id"])
    write_json(_checked_path(root), checked)
    # 업데이트 뒤 첫 세션: 규칙 문장 없는 옛 진단 규칙을 블록에서 빼고 알린다(한 번 — _notify_hidden이 표시를 남긴다)
    migrate = any(needs_rule_text(r) and r.get("enabled", True) and not r.get("hidden_notified_at") for r in _rules(data))
    if changed or migrate:
        if changed:
            save(root, data)
        try:
            render(root)
        except RuleBudgetError as e:
            notify(str(root), "harness_error", "must", "규칙 블록 갱신 실패: %s" % e)
        if changed:
            log_event(str(root), "rule_fact_changed", rule_ids=changed)
    return changed
