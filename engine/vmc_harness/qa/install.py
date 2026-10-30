"""QA 서브에이전트 설치 (spec 05 — 첫 설치본에 "쓸 수 있는 도구"로 기본 포함). init이 register, remove가 unregister.

원본은 `qa/subagent/vmc-qa.md` 하나. 여기서 두 형태로 내려놓는다:
- Claude Code: `.claude/agents/vmc-qa.md` (그대로). tools를 적지 않아 세션의 도구(고객이 쓰던 브라우저 MCP 포함)를 그대로 쓴다
- Codex(0.146 multi_agent): 역할 파일 `.codex/agents/vmc-qa.toml`(developer_instructions) +
  `.codex/config.toml`의 `[agents.vmc-qa]`(description, config_file) 표시 블록
고객이 같은 이름으로 직접 만든 파일은 건드리지 않는다(관리 표시가 있는 것만 덮어쓰기·삭제).
"""

from pathlib import Path

from vmc_harness.core import textfile

MARK = "vmc-harness:managed"
SRC = Path(__file__).resolve().parent / "subagent" / "vmc-qa.md"
CLAUDE_REL = ".claude/agents/vmc-qa.md"
CODEX_ROLE_REL = ".codex/agents/vmc-qa.toml"
CODEX_CONFIG_REL = ".codex/config.toml"
BLOCK_BEGIN = "# >>> vmc-harness qa (managed)"
BLOCK_END = "# <<< vmc-harness qa"


def _parse_source():
    text = SRC.read_text(encoding="utf-8")
    meta, body = {}, text
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        for line in fm.strip().splitlines():
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return text, meta, body.strip() + "\n"


def _toml_str(s):
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


def codex_role_toml(meta, body):
    if "'''" in body:
        raise ValueError("서브에이전트 본문에 ''' 사용 불가")
    return ("# %s — 하네스가 설치한 파일\n" % MARK +
            "name = %s\n" % _toml_str(meta.get("name", "vmc-qa")) +
            "description = %s\n" % _toml_str(meta.get("description", "")) +
            "developer_instructions = '''\n%s'''\n" % body)


def codex_config_block(meta):
    return "\n".join([BLOCK_BEGIN,
                      "[agents.vmc-qa]",
                      "description = %s" % _toml_str(meta.get("description", "")),
                      'config_file = "agents/vmc-qa.toml"',
                      BLOCK_END]) + "\n"


def _managed(p):
    return p.is_file() and MARK in p.read_text(encoding="utf-8", errors="replace")


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="\n") as f:  # 3.9: write_text에 newline 인자 없음
        f.write(text)


def _strip_block(text, eol="\n"):
    if BLOCK_BEGIN not in text:
        return text
    head, _, rest = text.partition(BLOCK_BEGIN)
    _, _, tail = rest.partition(BLOCK_END)
    return (head.rstrip("\r\n") + eol + tail.lstrip("\r\n")).strip("\r\n") + (eol if (head.strip() or tail.strip()) else "")


def register(root, agents=("claude", "codex")):
    """{"claude": "installed|skipped(고객 파일)", "codex": ...}"""
    root = Path(root)
    text, meta, body = _parse_source()
    out = {}
    if "claude" in agents:
        p = root / CLAUDE_REL
        if p.exists() and not _managed(p):
            out["claude"] = "skipped"
        else:
            _write(p, text)
            out["claude"] = "installed"
    if "codex" in agents:
        role = root / CODEX_ROLE_REL
        if role.exists() and not _managed(role):
            out["codex"] = "skipped"
        else:
            _write(role, codex_role_toml(meta, body))
            cfg = root / CODEX_CONFIG_REL
            cur, fmt = textfile.read(cfg)  # 고객 파일 — 원래 줄끝·BOM 유지
            eol = fmt["eol"]
            cur = _strip_block(cur or "", eol)
            if "[agents.vmc-qa]" in cur:
                out["codex"] = "skipped"
            else:
                sep = "" if not cur or cur.endswith(eol + eol) else (eol if cur.endswith("\n") else eol + eol)
                cfg.parent.mkdir(parents=True, exist_ok=True)
                textfile.write(cfg, cur + sep + textfile.to_eol(codex_config_block(meta), eol), fmt)
                out["codex"] = "installed"
    return out


def unregister(root):
    root = Path(root)
    removed = []
    for rel in (CLAUDE_REL, CODEX_ROLE_REL):
        p = root / rel
        if _managed(p):
            p.unlink()
            removed.append(rel)
    cfg = root / CODEX_CONFIG_REL
    if cfg.is_file():
        cur, fmt = textfile.read(cfg)
        if BLOCK_BEGIN in cur:
            new = _strip_block(cur, fmt["eol"])
            if new.strip():
                textfile.write(cfg, new, fmt)
            else:
                cfg.unlink()
            removed.append(CODEX_CONFIG_REL)
    return removed
