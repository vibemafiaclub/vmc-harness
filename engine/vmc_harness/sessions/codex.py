"""Codex rollout `~/.codex/sessions/YYYY/MM/DD/rollout-<시각>-<id>.jsonl` — {timestamp, type, payload}.

- 사람 메시지: event_msg/user_message. 이것이 없는 버전(Codex Desktop 0.150 alpha 실측)은
  response_item/message role=user 중 주입 컨텍스트(`<environment_context>` 등 '<'로 시작, `# AGENTS.md`)를 뺀 것
- 턴: task_started → task_complete/turn_aborted(turn_id로 짝). task_started가 없는 버전은 사람 메시지 →
  종료 표식(없으면 다음 사람 메시지 전 마지막 기록)
- 토큰: event_msg/token_count의 total_token_usage는 누적 → 마지막 값(total_tokens, 없으면 input+output.
  Codex의 input_tokens는 캐시 읽기(cached_input_tokens)를 포함한다 — Argos transcript-codex.ts §3.3).
  나눈 값: input = input − cached_input, cache_read = cached_input, output, cache_creation = 0(Codex에 없음).
  windows를 주면 구간마다 (구간 안 마지막 누적 − 구간 시작 전 마지막 누적)을 더한다
- 도구: response_item/function_call(JSON arguments)·custom_tool_call(raw input, apply_patch 등)
"""

import json
from pathlib import Path

from vmc_harness.sessions import Session, clip_turns, in_windows
from vmc_harness.sessions.common import iso, paths_from_input, read_lines, span_sec, ts

_TURN_END = ("task_complete", "turn_aborted")
_INJECTED = ("<", "# AGENTS.md")
_INPUT_TRIM = 2000
GUARDIAN_PREFIX = "The following is the Codex agent history"


def is_guardian_request(text):
    """Codex 자동 승인 심사(guardian)가 명령마다 보내는 요청 — 사람 지시가 아니다(세션 읽기 모든 경로가 이것으로 거른다)."""
    return isinstance(text, str) and text.lstrip().startswith(GUARDIAN_PREFIX)


def _tool_input(p):
    a = p.get("arguments")
    if isinstance(a, str):
        try:
            v = json.loads(a)
            if isinstance(v, dict):
                return v
        except ValueError:
            pass
        return {"arguments": a[:_INPUT_TRIM]}
    if isinstance(a, dict):
        return a
    if isinstance(p.get("input"), str):
        return {"input": p["input"]}
    return {}


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text") or "" for b in content if isinstance(b, dict))
    return ""


def _trim(inp):
    return {k: (v[:_INPUT_TRIM] if isinstance(v, str) else v) for k, v in inp.items()}


def _parts(tot):
    if not isinstance(tot, dict):
        return {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    inp = int(tot.get("input_tokens") or 0)
    cached = int(tot.get("cached_input_tokens") or 0)
    return {"input": max(inp - cached, 0), "output": int(tot.get("output_tokens") or 0), "cache_read": cached,
            "cache_creation": 0}


def _total(tot):
    t = int(tot.get("total_tokens") or 0)
    return t or int(tot.get("input_tokens") or 0) + int(tot.get("output_tokens") or 0)


def parse(path, windows=None):
    path = Path(path)
    records = read_lines(path)
    s = Session(session_id=None, agent="codex")
    if not records:
        return s
    prompts, item_prompts, ends, events, tools = [], [], [], [], []
    starts, end_by_turn = [], {}
    last_total = None
    totals = []   # [(시각, 누적)] — 구간 자르기용
    guardian = 0
    for r in records:
        rtype = r.get("type")
        p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        ptype = p.get("type")
        t = ts(r.get("timestamp"))
        if rtype == "session_meta":
            s.session_id = s.session_id or p.get("session_id") or p.get("id")
            s.cwd = s.cwd or p.get("cwd")
            continue
        if t is None:
            continue
        if rtype == "event_msg":
            if ptype == "user_message" and isinstance(p.get("message"), str) and p["message"].strip():
                if is_guardian_request(p["message"]):
                    guardian += 1
                    continue
                prompts.append((t, p["message"]))
                continue
            if ptype == "token_count":
                tot = (p.get("info") or {}).get("total_token_usage")
                if isinstance(tot, dict):
                    last_total = tot
                    totals.append((t, tot))
            if ptype == "task_started":
                starts.append((t, p.get("turn_id")))
            if ptype in _TURN_END:
                ends.append(t)
                if p.get("turn_id"):
                    end_by_turn[p["turn_id"]] = t
        if rtype == "response_item" and ptype == "message" and p.get("role") == "user":
            text = _text(p.get("content"))
            if is_guardian_request(text):
                guardian += 1
            elif text.strip() and not text.lstrip().startswith(_INJECTED):
                item_prompts.append((t, text))
        if rtype == "response_item" and ptype in ("function_call", "custom_tool_call") and p.get("name"):
            inp = _tool_input(p)
            tools.append({"at": iso(t), "name": p["name"], "input": _trim(inp), "paths": paths_from_input(inp)})
        events.append(t)
    s.session_id = s.session_id or path.stem
    s.guardian_requests = guardian  # 데이터클래스 칸 밖 표시(asdict에 안 실림) — bootstrap이 세션 종류(guardian)를 가른다
    if not prompts:
        prompts = item_prompts
    all_ts = [x[0] for x in prompts] + events
    if not all_ts:
        return s
    s.started_at, s.ended_at = iso(min(all_ts)), iso(max(all_ts))
    prompts.sort(key=lambda x: x[0])
    events.sort()
    ends.sort()
    turns = []
    starts.sort(key=lambda x: x[0])
    for i, (start, tid) in enumerate(starts):
        nxt = starts[i + 1][0] if i + 1 < len(starts) else None
        end = end_by_turn.get(tid)
        if end is None:
            cand = [e for e in events if e >= start and (nxt is None or e < nxt)]
            end = max(cand) if cand else None
        if end is not None:
            turns.append((start, end))
    for i, (start, _) in enumerate(prompts if not starts else []):
        nxt = prompts[i + 1][0] if i + 1 < len(prompts) else None
        in_turn = [e for e in ends if e >= start and (nxt is None or e < nxt)]
        if not in_turn:
            in_turn = [e for e in events if e >= start and (nxt is None or e < nxt)]
        if in_turn:
            turns.append((start, max(in_turn)))
    if windows is not None:
        prompts = [p for p in prompts if in_windows(p[0], windows)]
        tools = [x for x in tools if in_windows(ts(x["at"]), windows)]
        turns = clip_turns(turns, windows)
    s.human_prompts = [{"at": iso(t), "text": x} for t, x in prompts]
    s.human_turns = len(prompts)
    s.tool_calls = tools
    s.agent_sec = round(span_sec(turns), 1)
    if last_total and windows is None:
        s.tokens = _total(last_total)
        s.token_parts = _parts(last_total)
    elif last_total:
        parts = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        total = 0
        for ws, we in windows:
            before = [x for x in totals if x[0] < ws]
            inside = [x for x in totals if x[0] >= ws and (we is None or x[0] <= we)]
            if not inside:
                continue
            a = before[-1][1] if before else {}
            b = inside[-1][1]
            total += max(_total(b) - (_total(a) if a else 0), 0)
            pa, pb = _parts(a), _parts(b)
            for k in parts:
                parts[k] += max(pb[k] - pa[k], 0)
        s.tokens = total
        s.token_parts = parts
    # 토큰 누적값을 못 읽었으면(형식 변경 또는 토큰 이벤트 없음) 미측정
    # 사람 메시지 없이 목표(goal) 모드로 돈 턴도 에이전트 작업이다(0.133~0.137 실측) — 턴과 토큰이 읽히면 측정
    s.measured = bool(turns) and last_total is not None
    return s
