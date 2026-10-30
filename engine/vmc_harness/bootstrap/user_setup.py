"""이 PC의 사용자 단위 에이전트 설정 사실 (spec 07 §시니어식 진단 — 사용자 단위 지침·훅).

에이전트에 지금 걸려 있는 것 중 프로젝트 밖(사용자 단위)에 있는 것을 읽는다:
- Claude Code: `~/.claude/skills/*/SKILL.md`(이름·설명), 플러그인(`plugins/installed_plugins.json` + settings `enabledPlugins` —
  설정 파일로 판단, 플러그인이 싣는 스킬·훅 이름), `~/.claude/CLAUDE.md`, `~/.claude/settings.json` hooks
- Codex: `~/.codex/AGENTS.md`, `config.toml`(키 이름·MCP 서버 이름만), `hooks.json`, `skills/*/SKILL.md`
- 세션 기록에서 그 스킬이 불린 수, 훅이 돈 수·걸린 시간(Claude 기록의 durationMs — 파서가 주는 범위만)

고객 PC의 사적 설정이라 리포트·진단 결과에는 이름·요지·숫자만 싣는다(`summary`). 지침 원문은 진단 모델이 판단에 쓰도록
로컬 참고 자료(`docs_text`)로만 주고, 길이 상한·마스킹을 거친다. 하네스는 사용자 설정을 고치지 않는다 — 제안만.
위치 환경변수: CLAUDE_CONFIG_DIR(Claude Code와 같은 뜻), CODEX_HOME.
"""

import json
import os
import re
import statistics
from pathlib import Path

from vmc_harness.bootstrap import util
from vmc_harness.core import config
from vmc_harness.core.mask import mask

_SKILL_PATH = re.compile(r"skills[/\\]+([\w.:@-]+)[/\\]+SKILL\.md", re.I)
_SCRIPT = re.compile(r"([\w.-]+\.(?:sh|py|js|mjs|cjs|ts|ps1|cmd|bat|exe|rb))\b", re.I)
_WORD = re.compile(r"^[A-Za-z][\w.-]*$")
_SHELL_WORDS = {"if", "then", "fi", "test", "true", "false", "exec", "env", "sh", "bash", "zsh", "cmd", "powershell",
                "pwsh", "node", "python", "python3", "npx", "uvx", "do", "done", "export", "set", "echo", "cd", "command"}
_HOOK_ERROR_TYPES = ("hook_non_blocking_error", "hook_blocking_error", "hook_error", "hook_cancelled")


def claude_home():
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(cfg).expanduser() if cfg else Path.home() / ".claude"


def codex_home():
    h = os.environ.get("CODEX_HOME")
    return Path(h).expanduser() if h else Path.home() / ".codex"


# ---------------------------------------------------------------- 작은 파서

def _read_json(p):
    try:
        with open(str(p), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d
    except (OSError, ValueError):
        return None


def skill_meta(p):
    """SKILL.md 앞머리 → (name, description)."""
    text = util.read_text(p, 20000)
    name, desc = None, None
    m = re.match(r"^﻿?---\s*\n(.*?)\n---", text, re.S)
    head = m.group(1) if m else ""
    lines = head.splitlines()
    for i, line in enumerate(lines):
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k == "name" and v:
            name = v.strip("'\"")
        elif k == "description":
            if v in ("|", ">", "|-", ">-", ""):
                more = []
                for nxt in lines[i + 1:]:
                    if nxt.startswith((" ", "\t")):
                        more.append(nxt.strip())
                    else:
                        break
                v = " ".join(more)
            desc = v.strip("'\"")
    return name or Path(p).parent.name, desc or ""


def _skills_in(base, source):
    out = []
    d = Path(base)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*/SKILL.md")):
        name, desc = skill_meta(p)
        out.append({"name": name, "dir": p.parent.name, "description": desc, "source": source})
    return out


def hook_label(cmd):
    """훅 명령 → 사람이 알아볼 짧은 이름(스크립트 파일명 또는 실행 프로그램 이름). 원문 명령은 싣지 않는다."""
    c = str(cmd or "")
    m = _SCRIPT.findall(c)
    if m:
        return m[-1]
    for tok in re.split(r"[\s;&|()]+", c):
        tok = tok.strip("'\"")
        if "/" in tok or "\\" in tok:
            tok = re.split(r"[/\\]", tok)[-1]
        if tok and _WORD.match(tok) and tok.lower() not in _SHELL_WORDS:
            return tok
    return "(명령)"


def _cmd_key(cmd):
    """설정의 훅과 실행 기록을 잇는 열쇠. 세션 색인(로컬)에 남으므로 마스킹·홈 경로 가림을 거친다 — 양쪽이 같은 함수를 지난다."""
    return util.scrub_user(mask(re.sub(r"\s+", " ", str(cmd or "").strip())))


def hooks_from(settings, source):
    """settings 형식 {"hooks": {Event: [{matcher, hooks: [{type, command}]}]}} → [{event, matcher, label, key, source}]"""
    out = []
    hooks = (settings or {}).get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return out
    for event, groups in hooks.items():
        for g in groups if isinstance(groups, list) else []:
            if not isinstance(g, dict):
                continue
            for h in g.get("hooks") or []:
                if isinstance(h, dict) and (h.get("command") or h.get("type")):
                    cmd = h.get("command") or h.get("type")
                    out.append({"event": event, "matcher": g.get("matcher") or "", "label": hook_label(cmd),
                                "key": _cmd_key(cmd), "source": source, "timeout": h.get("timeout")})
    return out


def _doc(p):
    """지침 문서 요지: 줄 수·제목(#)·@가져오기 이름. 원문은 docs_text에만."""
    if not Path(p).is_file():
        return {"exists": False}
    t = util.read_text(p)
    heads = [l.lstrip("#").strip() for l in t.splitlines() if re.match(r"^#{1,3}\s+\S", l)]
    imports = re.findall(r"(?m)(?:^|\s)@([\w./~-]+\.md)\b", t)
    return {"exists": True, "lines": len(t.splitlines()), "chars": len(t), "headings": heads, "imports": imports}


def _toml_names(p):
    """config.toml → 키 이름(값은 안 읽는다 — 비밀값이 있을 수 있다)·[mcp_servers.<이름>]."""
    if not Path(p).is_file():
        return {"exists": False}
    keys, sections, named = [], {}, {"mcp_servers": [], "plugins": []}
    in_section = False
    for line in util.read_text(p).splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^\[+\s*([^\]]+?)\s*\]+$", s)
        if m:
            in_section = True
            # 표 이름의 첫 마디만 센다 — projects."<경로>"·hooks.state."<경로>"는 PC 경로라 싣지 않는다
            mm = re.match(r'^("[^"]+"|[\w-]+)(?:\.("[^"]+"|[\w-]+))?', m.group(1))
            head = mm.group(1).strip('"') if mm else m.group(1)
            sections[head] = sections.get(head, 0) + 1
            sub = (mm.group(2) or "").strip('"') if mm else ""
            if head in named and sub and sub not in named[head]:
                named[head].append(sub)
            continue
        k = s.split("=", 1)[0].strip()
        if "=" in s and k and not in_section:
            keys.append(k)
    return {"exists": True, "top_keys": keys, "sections": sections, "mcp_servers": named["mcp_servers"],
            "plugins": named["plugins"]}


# ---------------------------------------------------------------- 플러그인

def _plugin_installs(home, root):
    """installed_plugins.json(v1·v2) → [{id, scope, version, install_path}] — 사용자 범위 또는 이 프로젝트 범위만."""
    d = _read_json(home / "plugins" / "installed_plugins.json") or {}
    plugins = d.get("plugins") if isinstance(d.get("plugins"), dict) else d
    out = []
    here = str(Path(root).resolve()) if root else None
    for pid, v in (plugins or {}).items():
        if pid == "version":
            continue
        for ins in (v if isinstance(v, list) else [v]):
            if not isinstance(ins, dict):
                continue
            scope = ins.get("scope") or "user"
            if scope != "user":
                pp = ins.get("projectPath")
                if not (here and pp and str(Path(pp).expanduser().resolve()) == here):
                    continue
            out.append({"id": pid, "scope": scope, "version": ins.get("version"), "install_path": ins.get("installPath")})
    return out


def _plugin(pid, scope, version, install_path, enabled):
    rec = {"name": pid, "scope": scope, "version": version, "enabled": enabled, "skills": [], "hooks": []}
    base = Path(install_path).expanduser() if install_path else None
    if base and base.is_dir():
        rec["skills"] = [dict(s, source="plugin:" + pid) for s in _skills_in(base / "skills", "plugin:" + pid)]
        for hp in (base / "hooks" / "hooks.json", base / ".claude-plugin" / "hooks.json"):
            h = _read_json(hp)
            if h:
                rec["hooks"] = hooks_from(h, "plugin:" + pid)
                break
    return rec


# ---------------------------------------------------------------- 사용 기록

def hook_runs_from_records(records):
    """Claude 세션 기록 줄 → 훅 실행 [{event, label, key, ms, error, blocked}]. 원문 명령은 key(공백 정리)로만 — 색인에는 label만 남긴다."""
    out = []
    for r in records:
        a = r.get("attachment") if isinstance(r.get("attachment"), dict) else None
        if a and str(a.get("type") or "").startswith("hook_") and a.get("command") is not None:
            out.append({"event": a.get("hookEvent") or (a.get("hookName") or "").split(":")[0], "label": hook_label(a["command"]),
                        "key": _cmd_key(a["command"]), "ms": a.get("durationMs"),
                        "error": a.get("type") in _HOOK_ERROR_TYPES, "blocked": a.get("type") == "hook_blocking_error"})
        elif r.get("type") == "system" and r.get("subtype") in ("stop_hook_summary", "subagent_stop_hook_summary"):
            ev = "SubagentStop" if r.get("subtype").startswith("subagent") else "Stop"
            for h in r.get("hookInfos") or []:
                if isinstance(h, dict) and h.get("command") is not None:
                    out.append({"event": ev, "label": hook_label(h["command"]), "key": _cmd_key(h["command"]),
                                "ms": h.get("durationMs"), "error": False, "blocked": bool(r.get("preventedContinuation"))})
    return out


def hook_runs_file(path):
    """Claude 세션 파일에서 훅 실행만 — durationMs가 든 줄만 해석한다(전체 파싱은 sessions 영역이 이미 했다)."""
    recs = []
    try:
        with open(str(path), "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"durationMs"' not in line or "hook" not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    recs.append(d)
    except OSError:
        return []
    return hook_runs_from_records(recs)


def skill_of(name, inp):
    """도구 호출 하나가 스킬을 부른 것이면 그 이름. Claude `Skill` 도구, 또는 SKILL.md를 직접 읽은 호출(Codex)."""
    if name == "Skill" and isinstance(inp, dict):
        v = inp.get("skill") or inp.get("name") or inp.get("command")
        return str(v) if v else None
    text = inp if isinstance(inp, str) else (json.dumps(inp, ensure_ascii=False) if isinstance(inp, (dict, list)) else "")
    m = _SKILL_PATH.search(text or "")
    return m.group(1) if m else None


def run_key(event, key):
    """같은 명령이 여러 이벤트에 걸리면 이벤트마다 따로 센다."""
    return "%s\x1f%s" % (event or "", key or "")


def usage(sessions):
    """세션 색인 → {"skills": {이름: {calls, sessions}}, "hooks": {run_key(event, key): {...}}, "sessions": n, "hook_sessions": n}"""
    skills, hooks = {}, {}
    hook_sessions = 0
    for s in sessions or []:
        sid = s.get("session_id")
        for t in s.get("tool_calls") or []:
            sk = t.get("skill")
            if sk:
                x = skills.setdefault(sk, {"calls": 0, "sessions": set()})
                x["calls"] += 1
                x["sessions"].add(sid)
        runs = s.get("hook_runs") or []
        if runs:
            hook_sessions += 1
        for h in runs:
            k = run_key(h.get("event"), h.get("key") or h.get("label"))
            x = hooks.setdefault(k, {"label": h.get("label"), "event": h.get("event") or "", "runs": 0, "ms": [], "errors": 0,
                                     "blocked": 0, "sessions": set()})
            x["runs"] += 1
            if isinstance(h.get("ms"), (int, float)):
                x["ms"].append(float(h["ms"]))
            x["errors"] += 1 if h.get("error") else 0
            x["blocked"] += 1 if h.get("blocked") else 0
            x["sessions"].add(sid)
    out_s = {k: {"calls": v["calls"], "sessions": len(v["sessions"])} for k, v in skills.items()}
    out_h = {}
    for k, v in hooks.items():
        ms = v["ms"]
        out_h[k] = {"label": v["label"], "event": v["event"], "runs": v["runs"],
                    "sessions": len(v["sessions"]), "errors": v["errors"], "blocked": v["blocked"],
                    "total_sec": round(sum(ms) / 1000.0, 1) if ms else None,
                    "median_ms": round(statistics.median(ms)) if ms else None,
                    "max_ms": round(max(ms)) if ms else None}
    return {"skills": out_s, "hooks": out_h, "sessions": len(sessions or []), "hook_sessions": hook_sessions}


# ---------------------------------------------------------------- 모으기

def collect(root=None, sessions=None):
    """사용자 단위 설정 사실 + 사용 기록. {"summary": 리포트·진단 결과용(이름·요지·숫자), "docs_text": 진단 모델 참고용 원문(로컬)}"""
    ch, xh = claude_home(), codex_home()
    st = _read_json(ch / "settings.json") or {}
    enabled = st.get("enabledPlugins") if isinstance(st.get("enabledPlugins"), dict) else {}
    plugins = []
    seen = set()
    for ins in _plugin_installs(ch, root):
        if ins["id"] in seen:
            continue
        seen.add(ins["id"])
        plugins.append(_plugin(ins["id"], ins["scope"], ins["version"], ins["install_path"], bool(enabled.get(ins["id"]))))
    for pid, on in enabled.items():  # 켜져 있는데 설치 기록이 없는 것도 이름은 남긴다
        if pid not in seen:
            plugins.append({"name": pid, "scope": "user", "version": None, "enabled": bool(on), "skills": [], "hooks": []})
    claude = {"home": "~/.claude", "claude_md": _doc(ch / "CLAUDE.md"),
              "skills": _skills_in(ch / "skills", "user"),
              "hooks": hooks_from(st, "user settings"),
              "plugins": plugins}
    codex_hooks = _read_json(xh / "hooks.json") or {}
    codex = {"home": "~/.codex", "agents_md": _doc(xh / "AGENTS.md"),
             "agents_override_md": _doc(xh / "AGENTS.override.md"),
             "config": _toml_names(xh / "config.toml"),
             "hooks": hooks_from(codex_hooks if "hooks" in codex_hooks else {"hooks": codex_hooks}, "codex hooks.json"),
             "skills": _skills_in(xh / "skills", "codex")}
    u = usage(sessions)
    # 설정에 걸린 훅과 실행 기록을 (이벤트, 명령 문자열)로 잇는다. 플러그인 훅처럼 실행 때 변수가 펼쳐져 글자가 다르면
    # (이벤트, 이름)으로 잇는다. 원문 명령은 결과에 싣지 않는다
    all_hooks = claude["hooks"] + codex["hooks"] + [h for p in plugins if p.get("enabled") for h in p["hooks"]]
    used_keys = set()
    by_label = {}
    for k, v in u["hooks"].items():
        by_label.setdefault((v["event"], v["label"]), []).append(k)
    for h in all_hooks:
        k = run_key(h["event"], h["key"])
        if k not in u["hooks"]:
            k = next((x for x in by_label.get((h["event"], h["label"]), []) if x not in used_keys), None)
        run = u["hooks"].get(k) if k else None
        if k:
            used_keys.add(k)
        h["used"] = {x: run[x] for x in ("runs", "sessions", "total_sec", "median_ms", "max_ms", "errors", "blocked")} if run else None
    all_skills = claude["skills"] + codex["skills"] + [s for p in plugins if p.get("enabled") for s in p["skills"]]
    for s in all_skills:
        names = {s["name"], s["dir"]}
        if s["source"].startswith("plugin:"):
            plug = s["source"].split(":", 1)[1].split("@")[0]
            names |= {"%s:%s" % (plug, s["name"]), "%s:%s" % (plug, s["dir"])}
        hit = [u["skills"][n] for n in names if n in u["skills"]]
        s["used"] = {"calls": sum(x["calls"] for x in hit), "sessions": sum(x["sessions"] for x in hit)}
    other = [dict(v, source="프로젝트 설정·기타") for k, v in u["hooks"].items() if k not in used_keys]
    summary = {"claude": _strip_keys(claude), "codex": _strip_keys(codex),
               "other_hook_runs": sorted(other, key=lambda x: -(x.get("total_sec") or 0)),
               "skill_calls": u["skills"], "sessions_read": u["sessions"], "sessions_with_hook_records": u["hook_sessions"],
               "time_source": "훅 시간은 Claude Code 기록의 durationMs. 스킬 시간과 Codex 훅 시간은 기록에 없어 세지 않음"}
    lim_doc = config.get("diagnose.user_doc_chars", root)
    docs = []
    for label, p in (("~/.claude/CLAUDE.md", ch / "CLAUDE.md"), ("~/.codex/AGENTS.md", xh / "AGENTS.md"),
                     ("~/.codex/AGENTS.override.md", xh / "AGENTS.override.md")):
        if p.is_file():
            t = util.read_text(p)
            cut = len(t) > lim_doc
            docs.append("## %s%s\n\n%s" % (label, " (앞 %d자만)" % lim_doc if cut else "", util.scrub_user(mask(t[:lim_doc]))))
    for s in all_skills:
        if s.get("description"):
            docs.append("## 스킬 %s (%s)\n\n%s" % (s["name"], s["source"], util.scrub_user(mask(s["description"][:lim_doc]))))
    return {"summary": summary, "docs_text": "\n\n".join(docs)}


def _strip_keys(d):
    """결과·리포트용 — 원문 명령(key)은 빼고 설명은 길이 상한."""
    lim = config.get("diagnose.user_desc_chars")

    def walk(x):
        if isinstance(x, dict):
            out = {}
            for k, v in x.items():
                if k == "key":
                    continue
                if k == "description" and isinstance(v, str):
                    v = util.scrub_user(mask(v))
                    v = v if len(v) <= lim else v[:lim].rstrip() + "…"
                out[k] = walk(v)
            return out
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x
    return walk(d)
