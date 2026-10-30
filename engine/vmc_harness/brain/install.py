"""브레인 MCP 서버 등록·해제 (spec 04·07 init 2단계). Claude Code·Codex 양쪽, 프로젝트 단위.

- Claude Code: 프로젝트 `.mcp.json`의 mcpServers + `.claude/settings.json`의 enabledMcpjsonServers·permissions.allow
  (`claude -p`는 enabledMcpjsonServers 없이도 붙었다 — 2.1.301 실측. 대화형은 프로젝트 .mcp.json 서버 승인 창이 뜨므로
  이름을 넣어 둔다(대화형 미검증). 도구 두 개는 읽기 전용이라 allow에 넣어 호출마다 묻지 않게)
- Codex: 프로젝트 `.codex/config.toml`의 [mcp_servers.vmc-brain]. Codex 0.146 실측: 프로젝트 설정은 그 프로젝트가
  사용자 설정(~/.codex/config.toml)에서 trusted일 때만 읽힌다 — 결과에 codex_trusted로 알린다.
  도구 승인 기본값이 "묻기"라 `codex exec`에선 호출이 취소된다 → default_tools_approval_mode = "approve"(실측으로 확인)
- 명령 문자열은 PC마다 다른 절대 경로를 쓰지 않는다(팀 커밋에 실림) — 기본 `vmc-harness mcp`
"""

import json
import os
import re
from pathlib import Path

from vmc_harness.brain.mcp_server import SERVER_NAME
from vmc_harness.brain.store import write_lf
from vmc_harness.core.jsonio import read_json, write_json

DEFAULT_COMMAND = ("vmc-harness", "mcp")
TOOL_PERMS = ("mcp__%s__search_cards" % SERVER_NAME, "mcp__%s__get_card" % SERVER_NAME)  # 읽기 전용 — 호출마다 묻지 않게
_HEADER = "[mcp_servers.%s]" % SERVER_NAME


def _toml_str(s):
    return json.dumps(str(s), ensure_ascii=False)


def _codex_block(cmd):
    # 읽기 전용 도구라 호출마다 승인을 묻지 않게(codex exec는 묻는 순간 취소된다 — 0.146 실측)
    return "%s\ncommand = %s\nargs = [%s]\ndefault_tools_approval_mode = \"approve\"\n" % (
        _HEADER, _toml_str(cmd[0]), ", ".join(_toml_str(a) for a in cmd[1:]))


def _strip_codex_block(text):
    out, skip = [], False
    for line in text.splitlines(True):
        s = line.strip()
        if s.startswith("["):
            skip = s == _HEADER or s.startswith("[mcp_servers.%s." % SERVER_NAME)
        if not skip:
            out.append(line)
    return "".join(out).rstrip("\n") + ("\n" if "".join(out).strip() else "")


def codex_home():
    h = os.environ.get("CODEX_HOME")
    return Path(h).expanduser() if h else Path.home() / ".codex"


_PROJECT_HEADER = re.compile(r"""^\s*\[\s*projects\s*\.\s*("(?:[^"\\]|\\.)*"|'[^']*')\s*\]\s*$""")
_TRUSTED = re.compile(r'^\s*trust_level\s*=\s*["\']trusted["\']')


def _toml_key(raw):
    """[projects."…"] 키 문자열을 푼다. 리터럴('…')은 그대로, 기본("…")은 이스케이프를 푼다(틀린 이스케이프는 경로로 둔다)."""
    if raw.startswith("'"):
        return raw[1:-1]
    try:
        return json.loads(raw)
    except ValueError:
        pass
    return re.sub(r'\\([\\"])', r"\1", raw[1:-1])


def _path_key(p):
    """경로 비교용 정규화 — Windows는 긴 경로 접두(//?/)·구분자·대소문자 차이를 없앤다."""
    s = str(p)
    if os.name == "nt":
        if s.startswith("\\\\?\\UNC\\"):
            s = "\\\\" + s[8:]
        elif s.startswith("\\\\?\\"):
            s = s[4:]
        s = os.path.normcase(os.path.normpath(s))
    else:
        s = os.path.normpath(s)
    return s.rstrip("\\/") or s


def codex_trusted(root):
    """사용자 Codex 설정에서 이 프로젝트가 trusted인지. 키 표기(기본·리터럴 문자열)·Windows 경로 표기 차이를 흡수한다."""
    p = codex_home() / "config.toml"
    if not p.is_file():
        return False
    text = p.read_text(encoding="utf-8", errors="replace")
    want = {_path_key(Path(root)), _path_key(Path(root).resolve())}
    section = None
    for line in text.splitlines():
        if line.lstrip().startswith("["):
            m = _PROJECT_HEADER.match(line)
            section = _path_key(_toml_key(m.group(1))) if m else None
        elif section in want and _TRUSTED.match(line):
            return True
    return False


def register_mcp(root, command=None):
    root = Path(root)
    cmd = list(command or DEFAULT_COMMAND)
    # Claude Code
    mp = root / ".mcp.json"
    data = read_json(mp, None) or {}
    data.setdefault("mcpServers", {})[SERVER_NAME] = {"type": "stdio", "command": cmd[0], "args": cmd[1:]}
    write_json(mp, data)
    sp = root / ".claude" / "settings.json"
    st = read_json(sp, None) or {}
    enabled = list(st.get("enabledMcpjsonServers") or [])
    if SERVER_NAME not in enabled:
        enabled.append(SERVER_NAME)
    st["enabledMcpjsonServers"] = enabled
    perms = st.setdefault("permissions", {})
    allow = list(perms.get("allow") or [])
    for t in TOOL_PERMS:
        if t not in allow:
            allow.append(t)
    perms["allow"] = allow
    write_json(sp, st)
    # Codex
    cp = root / ".codex" / "config.toml"
    cp.parent.mkdir(parents=True, exist_ok=True)
    old = cp.read_text(encoding="utf-8") if cp.is_file() else ""
    base = _strip_codex_block(old)
    write_lf(cp, (base + "\n" if base.strip() else "") + _codex_block(cmd))
    return {"claude": [str(mp), str(sp)], "codex": str(cp), "codex_trusted": codex_trusted(root)}


def unregister_mcp(root):
    root = Path(root)
    mp = root / ".mcp.json"
    data = read_json(mp, None)
    if isinstance(data, dict) and SERVER_NAME in (data.get("mcpServers") or {}):
        data["mcpServers"].pop(SERVER_NAME)
        if not data["mcpServers"]:
            data.pop("mcpServers")
        if data:
            write_json(mp, data)
        else:
            mp.unlink()
    sp = root / ".claude" / "settings.json"
    st = read_json(sp, None)
    if isinstance(st, dict) and SERVER_NAME in (st.get("enabledMcpjsonServers") or []):
        st["enabledMcpjsonServers"] = [x for x in st["enabledMcpjsonServers"] if x != SERVER_NAME]
        if not st["enabledMcpjsonServers"]:
            st.pop("enabledMcpjsonServers")
        write_json(sp, st)
    st = read_json(sp, None)
    if isinstance(st, dict) and isinstance(st.get("permissions"), dict):
        allow = [x for x in (st["permissions"].get("allow") or []) if x not in TOOL_PERMS]
        if allow:
            st["permissions"]["allow"] = allow
        else:
            st["permissions"].pop("allow", None)
            if not st["permissions"]:
                st.pop("permissions")
        write_json(sp, st)
    cp = root / ".codex" / "config.toml"
    if cp.is_file():
        rest = _strip_codex_block(cp.read_text(encoding="utf-8"))
        if rest.strip():
            write_lf(cp, rest)
        else:
            cp.unlink()
