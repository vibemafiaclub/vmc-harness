"""에이전트 세션 기록 찾기·읽기 (spec 03 §실측, 09 원칙 1).

이식 원본: context-hub `projects/session-export/export_sessions.py`(Claude·Codex·Windows 검증 이력),
Argos `packages/cli/src/lib/transcript.ts`·`transcript-codex.ts`.

- 원본 로그는 복사하지 않고 숫자만 뽑는다.
- 형식이 바뀌어 못 읽으면 `measured=False` — 틀린 숫자로 판정하지 않는다.
- 에이전트 작업 시간 = "사람 메시지 → 에이전트 턴 종료" 구간 합(사람 턴 대기 제외).
- 토큰 = usage 합(Claude: 입력+출력+캐시 생성+캐시 읽기, 서브에이전트 포함 / Codex: 마지막 누적 total).
  token_parts에 input·output·cache_read·cache_creation을 나눠 두고, 작업 실측·예측 비교는 work_tokens()
  (캐시 읽기 제외 — 매 호출 다시 읽는 컨텍스트가 수천만으로 불어나 예측과 단위가 달라진다, 현장 결함 D2)
- windows=[(start, end|None)]를 주면 그 구간만 잰다(작업 구간 — 선언~마감). 턴은 구간과 겹친 부분만

위치 환경변수(테스트·특수 설치): VMC_CLAUDE_ROOT, CLAUDE_CONFIG_DIR, VMC_CODEX_ROOT, CODEX_HOME.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

AGENTS = ("claude", "codex")
HEAD_LINES = 30  # 프로젝트 판별 때 앞부분만 읽는다(session-export와 같은 값)


@dataclass
class Session:
    session_id: Optional[str]
    agent: str
    cwd: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    human_prompts: List[Dict[str, Any]] = field(default_factory=list)   # [{"at","text"}]
    assistant_texts: List[Dict[str, Any]] = field(default_factory=list)   # [{"at","text"}] 어시스턴트 글(도구 호출 제외) — 과설계 판정이 "ㄱㄱ"로 승인된 직전 제안을 읽는다(task/overdesign.py)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)      # [{"at","name","input","paths"}]
    tokens: int = 0
    token_parts: Dict[str, int] = field(default_factory=dict)   # input·output·cache_read·cache_creation
    agent_sec: float = 0.0
    human_turns: int = 0
    measured: bool = False
    turns: List[Any] = field(default_factory=list)   # [(start_dt, end_dt)] 작업 구간과 겹친 턴(단계 나누기 — task/phases.py)

    def work_tokens(self):
        """캐시 읽기를 뺀 토큰(입력 + 캐시 생성 + 출력). 나눈 값이 없으면 tokens 그대로."""
        p = self.token_parts or {}
        if not p:
            return self.tokens
        return int(p.get("input") or 0) + int(p.get("cache_creation") or 0) + int(p.get("output") or 0)


TOKEN_PARTS = ("input", "output", "cache_read", "cache_creation")


def in_windows(d, windows):
    """d(aware datetime)가 구간들 중 하나 안인가. windows가 None이면 항상 True."""
    if windows is None:
        return True
    if d is None:
        return False
    return any(d >= s and (e is None or d <= e) for s, e in windows)


def clip_turns(turns, windows):
    """[(start, end)] 턴을 구간과 겹친 부분만 남긴다."""
    if windows is None:
        return turns
    out = []
    for ts_, te in turns:
        for s, e in windows:
            a = max(ts_, s)
            b = te if e is None else min(te, e)
            if b > a:
                out.append((a, b))
    return out


def first_uuid(path, limit=200):
    """세션 기록의 첫 메시지 uuid. 이어받기(resume)로 세션 id가 바뀌면 새 파일이 앞 기록을 같은 uuid·타임스탬프로
    복사해 담는다(2026-11-05 실측) — 같은 대화인지 이 값으로 가른다. 못 읽으면 None."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i >= limit:
                    break
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("uuid") and r.get("type") in ("user", "assistant"):
                    return r["uuid"]
    except OSError:
        return None
    return None


def claude_root():
    p = os.environ.get("VMC_CLAUDE_ROOT")
    if p:
        return Path(p).expanduser()
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        return Path(cfg).expanduser() / "projects"
    return Path.home() / ".claude" / "projects"


def codex_root():
    p = os.environ.get("VMC_CODEX_ROOT")
    if p:
        return Path(p).expanduser()
    home = os.environ.get("CODEX_HOME")
    if home:
        return Path(home).expanduser() / "sessions"
    return Path.home() / ".codex" / "sessions"


def _norm(p):
    try:
        return os.path.normcase(os.path.normpath(str(Path(p).resolve())))
    except (OSError, RuntimeError):
        return os.path.normcase(os.path.normpath(str(p)))


def under(cwd, root):
    """cwd가 root 자신이거나 그 아래인가(Windows 대소문자 무시)."""
    if not cwd:
        return False
    c, r = _norm(cwd), _norm(root)
    return c == r or c.startswith(r.rstrip(os.sep) + os.sep)


def claude_dir_name(path):
    """Claude Code의 프로젝트 폴더 이름 인코딩(영숫자 외 문자를 '-'로)."""
    return "".join(ch if ch.isalnum() else "-" for ch in str(path))


def _head(path, n=HEAD_LINES):
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= n:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def _claude_cwd(path):
    for d in _head(path):
        if isinstance(d, dict) and d.get("cwd"):
            return d["cwd"]
    return None


def _codex_cwd(path):
    for d in _head(path, 5):
        if isinstance(d, dict) and d.get("type") == "session_meta":
            return (d.get("payload") or {}).get("cwd")
    return None


def _mtime(p):
    try:
        return p.stat().st_mtime
    except OSError:
        return 0


def find(project_root, agent=None, limit=None):
    """이 PC의 세션 중 이 프로젝트(하위 폴더 포함) 것. 최근 수정 순 [(agent, path)]."""
    found = []
    if agent in (None, "claude"):
        base = claude_root()
        if base.is_dir():
            enc = claude_dir_name(Path(project_root).resolve())
            for d in base.iterdir():
                if not d.is_dir():
                    continue
                if d.name.lower() != enc.lower() and not d.name.lower().startswith(enc.lower() + "-"):
                    continue
                for f in d.glob("*.jsonl"):
                    try:
                        if f.stat().st_size == 0:
                            continue
                    except OSError:
                        continue
                    if under(_claude_cwd(f), project_root):
                        found.append(("claude", f))
    if agent in (None, "codex"):
        base = codex_root()
        if base.is_dir():
            for f in base.rglob("*.jsonl"):
                if under(_codex_cwd(f), project_root):
                    found.append(("codex", f))
    found.sort(key=lambda x: _mtime(x[1]), reverse=True)
    return found[:limit] if limit else found


def find_by_id(project_root, session_id, agent=None):
    """세션 id로 원본 파일 경로. 없으면 None."""
    if not session_id:
        return None
    for ag, p in find(project_root, agent=agent):
        if session_id in p.name:
            return ag, p
    return None


def parse(path, agent=None, windows=None):
    """세션 파일 → Session. 못 읽으면 measured=False(예외를 던지지 않는다). windows: 작업 구간만 잴 때."""
    p = Path(path)
    if agent is None:
        agent = "codex" if "/.codex/" in str(p).replace("\\", "/") or p.name.startswith("rollout-") else "claude"
    try:
        if agent == "codex":
            from vmc_harness.sessions import codex
            return codex.parse(p, windows=windows)
        from vmc_harness.sessions import claude
        return claude.parse(p, windows=windows)
    except Exception:  # 형식 변경 등 — 틀린 숫자 대신 미측정
        return Session(session_id=None, agent=agent, measured=False)
