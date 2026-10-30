"""에이전트 설정 파일에 훅 등록·해제 (spec 02 §훅 디스패처 — init·remove가 부른다).

- Claude Code: 프로젝트 `.claude/settings.json`의 hooks 6개(SessionStart·UserPromptSubmit·PreToolUse·PostToolUse·Stop·SessionEnd)
  + PostToolUseFailure(실패한 검증 명령 소요 기록만 — CLAUDE_EXTRA)
- Codex: 프로젝트 `.codex/hooks.json` 같은 6개(0.146.0에서 UserPromptSubmit·SessionEnd 실측 확인) + 훅 신뢰 등록
  Codex는 신뢰 등록이 없는 훅을 조용히 건너뛴다(실측). 신뢰는 `codex app-server`의 hooks/list(currentHash) →
  config/value/write(`hooks.state."<key>".trusted_hash`)로 사용자 `~/.codex/config.toml`에 남긴다 — TUI `/hooks` 리뷰와 같은 기록.
  키는 PC·경로마다 달라서 팀원 PC에서는 세션 시작 때 한 번 백그라운드로 다시 등록한다(generated.on_session_start).
- 기존 사용자 훅은 보존하고 중복 등록하지 않는다. 해제는 우리 것(`vmc-harness hook`)만 지운다
"""

import hashlib
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from vmc_harness import __version__
from vmc_harness.core import config
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.hooks.contract import EVENTS

MARK = "vmc-harness hook"
LONG_EVENTS = ("SessionStart", "Stop", "SessionEnd")
# Claude Code는 실패한 도구 호출에 PostToolUse 대신 PostToolUseFailure만 보낸다(2.1.308 실측) — 검증 명령 실패 소요 기록용(기록만)
CLAUDE_EXTRA = ("PostToolUseFailure",)


def command_for(event, agent):
    return "%s %s --agent %s" % (MARK, event, agent)


def _timeout(event, root=None):
    key = "gates.stop_hook_timeout_s" if event in LONG_EVENTS else "budget.hook_timeout_s"
    return int(config.get(key, root))


def _is_ours(hook):
    return isinstance(hook, dict) and MARK in str(hook.get("command") or "")


def _read_settings(path):
    p = Path(path)
    if not p.is_file():
        return {}
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)   # 깨진 JSON이면 예외 — 사용자 파일을 덮어쓰지 않는다
    if not isinstance(data, dict):
        raise ValueError("설정 파일 최상위가 객체가 아님: %s" % path)
    return data


def merge_hooks(data, agent, root=None):
    """data(설정 dict)에 우리 훅을 병합. 반환 'injected'|'updated'|'already_present'."""
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks 항목이 객체가 아님")
    status = "already_present"
    for ev in tuple(EVENTS) + (CLAUDE_EXTRA if agent == "claude" else ()):
        want = {"type": "command", "command": command_for(ev, agent), "timeout": _timeout(ev, root)}
        groups = hooks.setdefault(ev, [])
        found = False
        for g in groups:
            for i, h in enumerate(g.get("hooks") or []):
                if _is_ours(h):
                    if found:
                        continue
                    found = True
                    if h != want:
                        g["hooks"][i] = want
                        status = "updated" if status == "already_present" else status
        if not found:
            groups.append({"matcher": "", "hooks": [want]})
            status = "injected"
    return status


def strip_hooks(data):
    """우리 훅만 지운다. 비면 그 이벤트·hooks 키도 지운다. 지운 개수."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    n = 0
    for ev in list(hooks):
        groups = hooks.get(ev) or []
        new_groups = []
        for g in groups:
            hs = g.get("hooks") or []
            keep = [h for h in hs if not _is_ours(h)]
            n += len(hs) - len(keep)
            if keep:
                g = dict(g)
                g["hooks"] = keep
                new_groups.append(g)
            elif not hs:
                new_groups.append(g)
        if new_groups:
            hooks[ev] = new_groups
        else:
            del hooks[ev]
    if not hooks:
        del data["hooks"]
    return n


def claude_settings_path(root):
    return Path(root) / ".claude" / "settings.json"


def codex_hooks_path(root):
    return Path(root) / ".codex" / "hooks.json"


def _register_file(path, agent, root):
    try:
        data = _read_settings(path)
    except ValueError as e:
        return "error: %s" % e
    status = merge_hooks(data, agent, root)
    if status != "already_present" or not Path(path).is_file():
        write_json(path, data)
    return status


def register(root, agents=("claude", "codex"), trust=True):
    """훅 등록. {'claude': 상태, 'codex': 상태, 'codex_trust': {...}}"""
    out = {}
    if "claude" in agents:
        out["claude"] = _register_file(claude_settings_path(root), "claude", root)
    if "codex" in agents:
        out["codex"] = _register_file(codex_hooks_path(root), "codex", root)
        if trust and not str(out["codex"]).startswith("error"):
            out["codex_trust"] = trust_codex(root)
    return out


def unregister(root):
    out = {}
    p = claude_settings_path(root)
    if p.is_file():
        try:
            data = _read_settings(p)
            n = strip_hooks(data)
            if n:
                write_json(p, data)
            out["claude"] = n
        except ValueError as e:
            out["claude"] = "error: %s" % e
    p = codex_hooks_path(root)
    if p.is_file():
        try:
            data = _read_settings(p)
            out["codex_untrust"] = untrust_codex(root, data)
            n = strip_hooks(data)
            if n:
                if data:
                    write_json(p, data)
                else:
                    p.unlink()
                    try:
                        p.parent.rmdir()
                    except OSError:
                        pass
            out["codex"] = n
        except ValueError as e:
            out["codex"] = "error: %s" % e
    return out


# ---- Codex 훅 신뢰 (app-server JSON-RPC) ----

SNAKE = {"SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit", "PreToolUse": "pre_tool_use",
         "PostToolUse": "post_tool_use", "Stop": "stop", "SessionEnd": "session_end"}


class AppServer(object):
    """`codex app-server` stdio JSON-RPC 최소 클라이언트(표준 라이브러리, 시간 상한)."""

    def __init__(self, exe, timeout):
        self.timeout = timeout
        self.p = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
        self.lines = []
        self.cv = threading.Condition()
        self.t = threading.Thread(target=self._reader, daemon=True)
        self.t.start()
        self.n = 0
        self.call("initialize", {"clientInfo": {"name": "vmc-harness", "version": __version__}})
        self._send({"jsonrpc": "2.0", "method": "initialized"})

    def _reader(self):
        for line in self.p.stdout:
            with self.cv:
                self.lines.append(line)
                self.cv.notify_all()
        with self.cv:
            self.lines.append(None)
            self.cv.notify_all()

    def _send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def call(self, method, params):
        self.n += 1
        rid = self.n
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        import time
        deadline = time.time() + self.timeout
        with self.cv:
            while True:
                while self.lines:
                    line = self.lines.pop(0)
                    if line is None:
                        raise RuntimeError("codex app-server 종료")
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("id") == rid:
                        if "error" in msg:
                            raise RuntimeError(str(msg["error"])[:200])
                        return msg.get("result")
                left = deadline - time.time()
                if left <= 0:
                    raise RuntimeError("codex app-server 응답 시간 초과(%s)" % method)
                self.cv.wait(left)

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.terminate()
            self.p.wait(timeout=self.timeout)
        except Exception:
            pass
        try:
            self.t.join(self.timeout)
            self.p.stdout.close()
        except Exception:
            pass


def _codex_exe():
    return shutil.which("codex")


def _real(root):
    return str(Path(os.path.realpath(str(root))))


def _record_path(root):
    from vmc_harness.core.project import local_dir
    return local_dir(root) / "codex-trust.json"


def hooks_hash(root):
    p = codex_hooks_path(root)
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.is_file() else None


def trust_needed(root):
    """이 PC에서 지금 hooks.json으로 신뢰 등록한 기록이 없으면 True."""
    if not _codex_exe() or not codex_hooks_path(root).is_file():
        return False
    try:
        rec = read_json(_record_path(root), {}) or {}
    except Exception:
        return True
    return rec.get("hooks_hash") != hooks_hash(root)


def _list_ours(srv, root):
    res = srv.call("hooks/list", {"cwds": [_real(root)]}) or {}
    ours = []
    for e in res.get("data") or []:
        for h in e.get("hooks") or []:
            if MARK in str(h.get("command") or "") and h.get("source") == "project":
                ours.append(h)
    return ours


def trust_codex(root):
    """우리 훅을 Codex 신뢰 목록에 등록. {'status': ok|no_codex|failed, 'trusted': n}"""
    exe = _codex_exe()
    if not exe:
        return {"status": "no_codex", "trusted": 0}
    timeout = config.get("gates.codex_trust_timeout_s", root)
    srv = None
    rec = {"keys": [], "project_trust_set": False}
    try:
        old = read_json(_record_path(root), {}) or {}
        rec["project_trust_set"] = bool(old.get("project_trust_set"))
    except Exception:
        pass
    try:
        srv = AppServer(exe, timeout)
        ours = _list_ours(srv, root)
        if not ours:
            # 프로젝트 자체가 신뢰되지 않으면 프로젝트 훅이 목록에 안 나온다 — 하네스를 설치한 프로젝트만 신뢰 등록
            srv.call("config/value/write", {"keyPath": "projects.%s.trust_level" % json.dumps(_real(root)),
                                            "value": "trusted", "mergeStrategy": "upsert"})
            rec["project_trust_set"] = True
            srv.close()
            srv = AppServer(exe, timeout)
            ours = _list_ours(srv, root)
        n = 0
        for h in ours:
            rec["keys"].append(h["key"])
            if h.get("trustStatus") == "trusted":
                continue
            srv.call("config/value/write", {"keyPath": "hooks.state.%s.trusted_hash" % json.dumps(h["key"]),
                                            "value": h["currentHash"], "mergeStrategy": "upsert"})
            n += 1
        rec["hooks_hash"] = hooks_hash(root)
        try:
            write_json(_record_path(root), rec)
        except Exception:
            pass
        return {"status": "ok" if ours else "failed", "trusted": n, "hooks": len(ours)}
    except Exception as e:
        return {"status": "failed", "trusted": 0, "error": str(e)[:200]}
    finally:
        if srv:
            srv.close()


def untrust_codex(root, data=None):
    """우리 훅의 신뢰 기록만 지운다(우리가 프로젝트 신뢰를 넣었으면 그것도)."""
    exe = _codex_exe()
    if not exe:
        return {"status": "no_codex"}
    keys = []
    try:
        rec = read_json(_record_path(root), None)
    except Exception:
        rec = None
    if not rec:
        # 우리가 신뢰를 등록한 기록이 없으면 사용자 Codex 설정을 건드리지 않는다
        return {"status": "nothing"}
    keys.extend(rec.get("keys") or [])
    data = data if data is not None else (_read_settings(codex_hooks_path(root)) if codex_hooks_path(root).is_file() else {})
    hp = str(Path(_real(root)) / ".codex" / "hooks.json")
    for ev, groups in (data.get("hooks") or {}).items():
        for gi, g in enumerate(groups or []):
            for hi, h in enumerate(g.get("hooks") or []):
                if _is_ours(h) and ev in SNAKE:
                    keys.append("%s:%s:%d:%d" % (hp, SNAKE[ev], gi, hi))
    keys = sorted(set(keys))
    srv = None
    try:
        srv = AppServer(exe, config.get("gates.codex_trust_timeout_s", root))
        for k in keys:
            srv.call("config/value/write", {"keyPath": "hooks.state.%s" % json.dumps(k), "value": None,
                                            "mergeStrategy": "replace"})
        if rec.get("project_trust_set"):
            srv.call("config/value/write", {"keyPath": "projects.%s" % json.dumps(_real(root)), "value": None,
                                            "mergeStrategy": "replace"})
        return {"status": "ok", "removed": len(keys)}
    except Exception as e:
        return {"status": "failed", "error": str(e)[:200]}
    finally:
        if srv:
            srv.close()
