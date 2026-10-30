"""부트스트랩이 읽는 세션 기록을 한 모양으로 모은다.

- 기본: 이 PC 세션 — sessions 영역 `find`·`parse`(없으면 세션 없이 진행)
- `--sessions-dir`: 다른 PC에서 내보낸 세션 묶음(session-export JSON) 또는 원본 jsonl 폴더
- `--analysis-dir`: 사람 프롬프트 정제본(`prompts.jsonl`)·전수 태깅(`tag_*.md`)·`stats.json`이 있는 폴더

돌려주는 세션 dict(09·interfaces의 Session과 같은 필드 + 분류):
 session_id, agent, cwd, started_at, ended_at, kind(main|sub|guardian),
 human_prompts:[{at,text}], tool_calls:[{at,name,write,paths,commands,skill?}], tokens, agent_sec, human_turns, measured,
 hook_runs:[{event,label,key,ms,error,blocked}](Claude만 — 사용자 단위 훅 점검용)
"""

import json
import re
from dataclasses import asdict, is_dataclass
from pathlib import Path, PureWindowsPath, PurePosixPath

from vmc_harness.bootstrap import util
from vmc_harness.core import config
from vmc_harness.core.clock import parse_iso

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "str_replace_based_edit_tool"}
COMMAND_TOOLS = {"Bash", "PowerShell", "run", "shell_command", "exec_command", "shell", "local_shell", "exec"}
ASK_TOOLS = {"AskUserQuestion", "request_user_input", "request_user_input_async"}
_INJECTED = ("# AGENTS.md instructions", "<environment_context", "<user_instructions", "The following is the Codex agent history",
             "<permissions instructions", "<INSTRUCTIONS>", "Caveat:", "<command-name>", "<local-command-stdout>",
             "<system-reminder>", "[Request interrupted", "<task-notification>")
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)
_JSON_CMD = re.compile(r'"(?:command|cmd)"\s*:\s*("(?:[^"\\]|\\.)*"|\[[^\]]*\])')


def _loads(v):
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _to_rel(path, cwd):
    """세션의 절대 경로 → 프로젝트 상대 경로(구분자 /). Windows 경로도 다룬다."""
    if not path:
        return None
    p = str(path).strip().strip('"').strip("'")
    if not p:
        return None
    pw = p.replace("\\", "/")
    if cwd:
        c = str(cwd).replace("\\", "/").rstrip("/")
        if pw.lower().startswith(c.lower() + "/"):
            return pw[len(c) + 1:]
    if re.match(r"^[A-Za-z]:/", pw) or pw.startswith("/"):
        return None  # 프로젝트 밖
    return pw.lstrip("./") if pw.startswith("./") else pw


def _commands_from(name, inp):
    out = []
    if isinstance(inp, dict):
        c = inp.get("command", inp.get("cmd"))
        if isinstance(c, list):
            c = " ".join(str(x) for x in c)
        if isinstance(c, str) and c:
            out.append(c)
        code = inp.get("code") if name in ("exec", "js") else None
        if isinstance(code, str):
            out.extend(_commands_from_text(code))
    elif isinstance(inp, str) and name in ("exec",):
        out.extend(_commands_from_text(inp))
    elif isinstance(inp, str) and name in COMMAND_TOOLS:
        out.append(inp)
    return out


def _commands_from_text(text):
    out = []
    for m in _JSON_CMD.finditer(text):
        raw = m.group(1)
        try:
            v = json.loads(raw)
        except ValueError:
            continue
        if isinstance(v, list):
            v = " ".join(str(x) for x in v)
        if v:
            out.append(v)
    return out


def normalize_tool_call(name, inp, at=None, cwd=None):
    inp = _loads(inp)
    paths, write = [], False
    if isinstance(inp, dict):
        for k in ("file_path", "path", "notebook_path", "filePath"):
            if isinstance(inp.get(k), str):
                paths.append(inp[k])
        for e in inp.get("edits") or [] if isinstance(inp.get("edits"), list) else []:
            if isinstance(e, dict) and isinstance(e.get("file_path"), str):
                paths.append(e["file_path"])
    text = inp if isinstance(inp, str) else (json.dumps(inp, ensure_ascii=False) if name in ("exec", "apply_patch") else "")
    patch_paths = _PATCH_FILE.findall(text or "")
    if name in WRITE_TOOLS or patch_paths:
        write = True
    paths += patch_paths
    if name in ("Read", "Grep", "Glob", "view_image", "codegraph_node", "diagnostics", "symbols", "open_in_codex"):
        write = False
    rel_paths = []
    for p in paths:
        r = _to_rel(p, cwd)
        if r and r not in rel_paths:
            rel_paths.append(r)
    commands = _commands_from(name, inp) if (name in COMMAND_TOOLS or name == "js") else []
    out = {"at": at, "name": name, "write": write, "paths": rel_paths, "commands": commands}
    from vmc_harness.bootstrap.user_setup import skill_of
    sk = skill_of(name, inp)
    if sk:
        out["skill"] = sk  # 사용자 단위 스킬 사용 횟수(user_setup)
    return out


# ---------------------------------------------------------------- analysis-dir

def load_analysis_prompts(analysis_dir):
    """prompts.jsonl → {session_id: [{at,text}]} (kind=main만 — 사람이 친 것)"""
    out = {}
    p = Path(analysis_dir) / "prompts.jsonl"
    if not p.is_file():
        return out
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("kind") != "main" or d.get("sub"):
                continue
            out.setdefault(d.get("sid"), []).append({"at": d.get("ts"), "text": d.get("text") or ""})
    return out


_TAG_ROW = re.compile(r"^\|\s*([0-9a-f]{8})[^|]*\|[^|]*\|[^|]*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", re.M)
_TAG_TOTAL = re.compile(r"^\|\s*\*\*합계[^|]*\|[^|]*\|[^|]*\|" + r"\s*\**(\d+)\**\s*\|" * 5, re.M)


def load_analysis_tags(analysis_dir):
    """tag_*.md 세션별 표 → {"sessions": {sid8: {H,A,B,C,D}}, "totals": {...}} (사람이 전수 태깅한 A/B/C/D)"""
    sessions, totals, row_totals = {}, {"H": 0, "A": 0, "B": 0, "C": 0, "D": 0}, {"H": 0, "A": 0, "B": 0, "C": 0, "D": 0}
    has_total = False
    for f in sorted(Path(analysis_dir).glob("tag_*.md")):
        text = util.read_text(f)
        for m in _TAG_ROW.finditer(text):
            vals = dict(zip("HABCD", (int(m.group(i)) for i in range(2, 7))))
            sessions[m.group(1)] = vals
            for k in row_totals:
                row_totals[k] += vals[k]
        for m in _TAG_TOTAL.finditer(text):  # 묶음별 합계 줄이 있으면 그것이 원본
            has_total = True
            for i, k in enumerate("HABCD"):
                totals[k] += int(m.group(i + 1))
    if not sessions and not has_total:
        return None
    return {"sessions": sessions, "totals": totals if has_total else row_totals}


def load_analysis_kinds(analysis_dir):
    """stats.json → {세션 파일 상대 경로: main|sub|guardian} (0주차 분류 원본 — 같은 id가 여러 파일에 있어 파일로 잇는다)"""
    p = Path(analysis_dir) / "stats.json"
    try:
        rows = json.loads(util.read_text(p)) if p.is_file() else []
    except ValueError:
        rows = []
    out = {}
    for r in rows if isinstance(rows, list) else []:
        k = r.get("kind")
        out[r.get("file")] = "sub" if k in ("spawn", "sub") else (k or "main")
    return out


# ---------------------------------------------------------------- 내보내기 JSON

def _is_guardian(text):
    """Codex 자동 승인 심사 요청 — 판정은 sessions.codex 한 곳(모든 세션 읽기 경로가 같은 함수로 거른다)."""
    from vmc_harness.sessions.codex import is_guardian_request
    return is_guardian_request(text)


def _is_injected(text):
    t = (text or "").lstrip()
    return any(t.startswith(x) for x in _INJECTED) or _is_guardian(t)


def from_export(d, prompts_by_sid=None, kind_override=None):
    agent = (d.get("agent") or "").lower()
    src = d.get("_source") or {}
    cwd = src.get("cwd")
    title = d.get("title") or ""
    kind = "sub" if d.get("isSubagent") else "main"
    if _is_guardian(title):
        kind = "guardian"
    if kind_override:
        kind = kind_override
    human, tools, asks = [], [], 0
    msgs = d.get("messages") or []
    for m in msgs:
        if m.get("toolName"):
            tc = normalize_tool_call(m["toolName"], m.get("toolInput"), m.get("timestamp"), cwd)
            tools.append(tc)
            if m["toolName"] in ASK_TOOLS:
                inp = _loads(m.get("toolInput"))
                qs = inp.get("questions") if isinstance(inp, dict) else None
                asks += len(qs) if isinstance(qs, list) and qs else 1
        elif m.get("role") == "user" and kind == "main" and prompts_by_sid is None:
            text = m.get("content") or ""
            if text and not _is_injected(text):
                human.append({"at": m.get("timestamp"), "text": text})
    if prompts_by_sid is not None:
        human = list(prompts_by_sid.get(d.get("id"), [])) if kind == "main" else []
    tokens = sum(int(d.get(k) or 0) for k in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens"))
    s, e = parse_iso(d.get("startedAt")), parse_iso(d.get("endedAt"))
    return {"session_id": d.get("id"), "agent": agent, "cwd": cwd, "started_at": d.get("startedAt"),
            "ended_at": d.get("endedAt"), "kind": kind, "human_prompts": human, "tool_calls": tools,
            "tokens": tokens, "agent_sec": None, "human_turns": len(human) + asks, "asks": asks,
            "wall_sec": (e - s).total_seconds() if s and e else None, "measured": True}


def _from_parsed(sess):
    d = asdict(sess) if is_dataclass(sess) else dict(sess)
    guardian = int(getattr(sess, "guardian_requests", 0) or d.pop("guardian_requests", 0) or 0)  # 파서가 이미 거른 수
    human = [x for x in d.get("human_prompts") or [] if not _is_guardian(x.get("text"))]
    extra = len(d.get("human_prompts") or []) - len(human)  # 파서가 못 거른 것(옛 파서·다른 형식)
    if guardian or extra:  # 자동 승인 심사 요청은 사람 개입이 아니다(D4) — 사람 지시가 하나도 없으면 심사 세션
        d["human_prompts"] = human
        d["human_turns"] = max(0, int(d.get("human_turns") or 0) - extra) if human else 0
        d["guardian_requests"] = guardian + extra
        if not human:
            d["kind"] = "guardian"
    tools = []
    for t in d.get("tool_calls") or []:
        tc = normalize_tool_call(t.get("name"), t.get("input"), t.get("at"), d.get("cwd"))
        if not tc["paths"] and t.get("paths"):
            tc["paths"] = [p for p in (_to_rel(x, d.get("cwd")) or x for x in t["paths"]) if p]
        tools.append(tc)
    d["tool_calls"] = tools
    d.setdefault("kind", "main")
    d.setdefault("asks", 0)
    return d


def _with_hooks(d, agent, path):
    """Claude 세션이면 훅 실행 기록(명령 이름·걸린 시간)을 붙인다 — sessions 파서가 다루지 않는 줄이라 따로 읽는다."""
    if agent == "claude":
        from vmc_harness.bootstrap.user_setup import hook_runs_file
        d["hook_runs"] = hook_runs_file(path)
    return d


def load(root, sessions_dir=None, analysis_dir=None, limit=None):
    """세션 목록과 출처 설명. (sessions, info)"""
    limit = limit or config.get("bootstrap.max_sessions", root)
    prompts = load_analysis_prompts(analysis_dir) if analysis_dir else None
    kinds = load_analysis_kinds(analysis_dir) if analysis_dir else None
    info = {"source": None, "read": 0, "skipped": 0, "limit": limit, "analysis_dir": bool(analysis_dir)}
    out = []
    if sessions_dir:
        info["source"] = "sessions_dir"
        files = sorted(Path(sessions_dir).rglob("*.json")) + sorted(Path(sessions_dir).rglob("*.jsonl"))
        for f in files:
            if f.name in ("manifest.json", "stats.json"):
                continue
            if f.suffix == ".json":
                try:
                    with f.open("r", encoding="utf-8") as fh:
                        d = json.load(fh)
                except (ValueError, OSError):
                    info["skipped"] += 1
                    continue
                if not isinstance(d, dict) or "messages" not in d:
                    info["skipped"] += 1
                    continue
                out.append(from_export(d, prompts, (kinds or {}).get(f.relative_to(sessions_dir).as_posix())))
            else:
                agent = "codex" if "codex" in f.as_posix().lower() or f.name.startswith("rollout-") else "claude"
                st, sess = util.optional_call([("vmc_harness.sessions", "parse")], str(f), agent)
                if st == "ok" and sess is not None:
                    out.append(_with_hooks(_from_parsed(sess), agent, f))
                else:
                    info["skipped"] += 1
    else:
        st, found = util.optional_call([("vmc_harness.sessions", "find")], str(root), None, None)
        if st != "ok":
            info["source"] = "none (sessions 영역 없음 — 건너뜀)"
            return [], info
        info["source"] = "this_pc"
        for agent, path in found or []:
            st, sess = util.optional_call([("vmc_harness.sessions", "parse")], str(path), agent)
            if st == "ok" and sess is not None:
                out.append(_with_hooks(_from_parsed(sess), agent, path))
            else:
                info["skipped"] += 1
    out.sort(key=lambda s: s.get("started_at") or "", reverse=True)
    main = [s for s in out if s.get("kind") == "main"]
    others = [s for s in out if s.get("kind") != "main"]
    # 상한은 사람이 연 세션 기준(최근 것부터). 서브·심사 세션은 도구 호출 백테스트용으로 같은 기간 것만 남긴다
    main = main[:limit]
    if main:
        oldest = min((s.get("started_at") or "") for s in main)
        others = [s for s in others if (s.get("started_at") or "") >= oldest]
    info["read"] = len(main) + len(others)
    info["main"] = len(main)
    info["truncated"] = len([s for s in out if s.get("kind") == "main"]) > limit
    return main + others, info
