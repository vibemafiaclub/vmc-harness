"""Claude Code 세션 `~/.claude/projects/<인코딩된 경로>/<세션id>.jsonl`.

- 사람 메시지: type=user, isMeta 아님, 사이드체인 아님, 도구 결과 아님, 시스템이 넣은 알림(task-notification 등) 아님
- 턴 종료: 다음 사람 메시지 전까지의 마지막 기록 시각(assistant·도구 결과·system)
- 토큰: assistant의 message.usage — 한 응답이 블록마다 여러 줄로 쪼개져 같은 usage가 반복되므로 message.id로 한 번만
- 서브에이전트: `<세션id>/subagents/*.jsonl`의 토큰을 더한다(시간은 부모 턴 안이라 더하지 않는다)
"""

from pathlib import Path

from vmc_harness.sessions import Session, clip_turns, in_windows
from vmc_harness.sessions.common import iso, paths_from_input, read_lines, span_sec, ts

_NOT_HUMAN_PREFIX = ("<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>",
                     "<bash-stdout>", "<bash-stderr>", "<task-notification>", "[Request interrupted")
_INPUT_TRIM = 2000
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _system_trigger(r):
    """사람이 아니라 시스템이 넣어 에이전트 턴을 시작시킨 메시지(백그라운드 작업 알림 등)."""
    if r.get("type") not in ("user", "human") or r.get("isMeta") or r.get("isSidechain"):
        return False
    origin = r.get("origin")
    if isinstance(origin, dict) and origin.get("kind") and origin.get("kind") != "human":
        return True
    if r.get("promptSource") == "system" or (r.get("turnOrigin") or "").startswith("task"):
        return True
    content = (r.get("message") or {}).get("content")
    return isinstance(content, str) and content.lstrip().startswith("<task-notification>")


def _meta(r):
    """턴과 무관한 기록(메타·로컬 명령 출력) — 시간 계산에서 뺀다."""
    if r.get("type") not in ("user", "human"):
        return False
    if r.get("isMeta") or r.get("isCompactSummary") or r.get("isVisibleInTranscriptOnly"):
        return True
    content = (r.get("message") or {}).get("content")
    return isinstance(content, str) and content.lstrip().startswith(_NOT_HUMAN_PREFIX) \
        and not content.lstrip().startswith("<task-notification>")


def _human_text(r):
    """사람이 직접 쓴 메시지면 그 텍스트, 아니면 None."""
    if r.get("type") not in ("user", "human") or r.get("isMeta") or r.get("isSidechain"):
        return None
    if r.get("isCompactSummary") or r.get("isVisibleInTranscriptOnly") or _system_trigger(r):
        return None
    content = (r.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        parts = [b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text"]
        has_image = any(isinstance(b, dict) and b.get("type") == "image" for b in content)
        text = "\n".join(p for p in parts if p)
        if not text and not has_image:
            return None
    else:
        return None
    if text.lstrip().startswith(_NOT_HUMAN_PREFIX):
        return None
    return text


def _trim(v):
    if isinstance(v, str):
        return v if len(v) <= _INPUT_TRIM else v[:_INPUT_TRIM]
    if isinstance(v, dict):
        return {k: _trim(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_trim(x) for x in v]
    return v


_PART_OF = {"input_tokens": "input", "output_tokens": "output", "cache_read_input_tokens": "cache_read",
            "cache_creation_input_tokens": "cache_creation"}


def _usage_tokens(records, windows=None, parts=None):
    """(합계, usage가 있던 응답 수, assistant 응답 수). parts(dict)를 주면 종류별로 더한다. windows 밖 응답은 뺀다."""
    seen, total, with_usage, assistants = set(), 0, 0, 0
    for r in records:
        if r.get("type") != "assistant":
            continue
        if windows is not None and not in_windows(ts(r.get("timestamp")), windows):
            continue
        msg = r.get("message") or {}
        key = msg.get("id") or r.get("requestId") or r.get("uuid")
        if key in seen:
            continue
        seen.add(key)
        assistants += 1
        u = msg.get("usage")
        if isinstance(u, dict):
            with_usage += 1
            total += sum(int(u.get(k) or 0) for k in USAGE_KEYS)
            if parts is not None:
                for k in USAGE_KEYS:
                    parts[_PART_OF[k]] = int(parts.get(_PART_OF[k]) or 0) + int(u.get(k) or 0)
    return total, with_usage, assistants


def _subagent_files(path):
    d = path.parent / path.stem / "subagents"
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


def parse(path, windows=None):
    path = Path(path)
    records = read_lines(path)
    s = Session(session_id=None, agent="claude")
    if not records:
        return s
    prompts, tools, events, triggers = [], [], [], []
    for r in records:
        s.session_id = s.session_id or r.get("sessionId")
        s.cwd = s.cwd or r.get("cwd")
        t = ts(r.get("timestamp"))
        if t is None:
            continue
        rtype = r.get("type")
        text = _human_text(r)
        if text is not None:
            prompts.append((t, text))
            continue
        if r.get("isSidechain") or _meta(r):
            continue
        if _system_trigger(r):
            triggers.append(t)
            continue
        if rtype in ("assistant", "user", "system"):
            events.append(t)
        if rtype == "assistant":
            content = (r.get("message") or {}).get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name"):
                        inp = b.get("input") or {}
                        tools.append({"at": iso(t), "name": b["name"], "input": _trim(inp),
                                      "paths": paths_from_input(inp)})
    s.session_id = s.session_id or path.stem
    all_ts = [p[0] for p in prompts] + events
    if not all_ts:
        return s
    s.started_at, s.ended_at = iso(min(all_ts)), iso(max(all_ts))
    prompts.sort(key=lambda x: x[0])
    events.sort()
    # 턴 시작 = 사람 메시지 또는 시스템 트리거(백그라운드 작업 알림 — 사람을 기다린 게 아니다)
    starts = sorted([t for t, _ in prompts] + triggers)
    turns = []
    for i, start in enumerate(starts):
        nxt = starts[i + 1] if i + 1 < len(starts) else None
        ends = [e for e in events if e >= start and (nxt is None or e < nxt)]
        if ends:
            turns.append((start, max(ends)))
    if windows is not None:
        # 작업 구간만: 구간 안 사람 메시지·도구 호출, 턴은 겹친 부분(선언이 턴 중간이어도 그 뒤는 센다)
        prompts = [p for p in prompts if in_windows(p[0], windows)]
        tools = [x for x in tools if in_windows(ts(x["at"]), windows)]
        turns = clip_turns(turns, windows)
    s.human_prompts = [{"at": iso(t), "text": x} for t, x in prompts]
    s.human_turns = len(prompts)
    s.tool_calls = tools
    s.agent_sec = round(span_sec(turns), 1)
    parts = {}
    tokens, with_usage, assistants = _usage_tokens(records, windows, parts)
    for sub in _subagent_files(path):
        try:
            tokens += _usage_tokens(read_lines(sub), windows, parts)[0]
        except OSError:
            pass
    s.tokens = tokens
    s.token_parts = parts
    # 응답은 있는데 usage를 하나도 못 읽었으면 형식이 바뀐 것 — 미측정
    s.measured = bool(turns) and assistants > 0 and with_usage > 0
    return s
