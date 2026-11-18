"""진단 신호 공통 (spec 07 §자동 반영 · 04 §판정) — 셸 명령에 정규식을 걸 때 '언급'을 빼고, 문제가 이미 풀렸는지 본다.

언급: 명령 이름이 아니라 글자로만 나온 것 — grep·rg·echo·printf·vmc-harness 인자, git commit 메시지.
      `grep DATABASE_URL .env.local`은 DB에 붙지 않는다. heredoc 본문은 빼지 않는다(인라인 스크립트가 실제로 접속하는 일이 많다).
해결 이벤트: 마지막 신호 뒤에 제안의 고칠 말(`…`로 감싼 명령·경로 중 신호에 안 걸리는 것)이 커밋으로 레포에 들어갔고,
      그 뒤 사람이 연 대화 diagnose.resolved_min_sessions개 이상에서 신호가 0이면 이미 풀린 문제다.
      "최근 N시간 0건"은 쓰지 않는다 — 이틀 조용하다가 다시 나는 문제가 있다(2026-11-18 실측).
"""

import functools
import re

from vmc_harness.core import config, gitutil
from vmc_harness.core.clock import parse_iso

MENTION_CMDS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "echo", "printf", "vmc-harness"}
_TICK = re.compile(r"`([^`\n]{3,200})`")


def _base(tok):
    from vmc_harness.gates import shell
    return shell._base(str(tok))


@functools.lru_cache(maxsize=20000)
def effective_command(cmd):
    """언급 인자를 뺀 명령 글자. 분석기가 못 읽으면 원문 그대로(덜 빼는 쪽)."""
    if not cmd:
        return ""
    try:
        from vmc_harness.gates import shell
        segs = shell._segments_full(cmd)
    except Exception:
        return cmd
    out = []
    for toks, body, _pre, post in segs:
        words = [t for t in toks if not isinstance(t, shell.Op)]
        strs = [str(t) for t in words]
        i = 0
        while i < len(strs) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", strs[i]):  # 앞 변수 대입은 명령 이름이 아님
            i += 1
        name = _base(strs[i]) if i < len(strs) else ""
        if name in MENTION_CMDS or (name == "git" and "commit" in strs[i + 1:i + 3]):
            out.append(strs[i] if i < len(strs) else "")
        else:
            out.append(" ".join(strs))
        if body:
            out.append("\n" + body)
        out.append("\n" if str(post) == "\n" else " %s " % post)  # 원래 구분자 — 한 줄 정규식([^\n]*)이 그대로 맞게
    return "".join(out)


def cmd_search(rx, cmd):
    return bool(rx.search(effective_command(cmd or "")))


def _aware(t):
    t = parse_iso(t) if isinstance(t, str) else t
    if t is not None and t.tzinfo is None:
        t = t.astimezone()
    return t


def hit_times(rx, sessions):
    """신호(명령 정규식)가 난 시각 목록과 사람이 연 대화 [(시작 시각, 세션)]."""
    hits, mains = [], []
    for s in sessions or []:
        st = _aware(s.get("started_at"))
        if s.get("kind", "main") == "main" and st is not None:
            mains.append(st)
        for tc in s.get("tool_calls") or []:
            if any(cmd_search(rx, c) for c in tc.get("commands") or []):
                t = _aware(tc.get("at")) or st
                if t is not None:
                    hits.append(t)
    return sorted(hits), sorted(mains)


def regex_of(p):
    """제안의 명령 신호 정규식(gate command 조건 또는 rule·structure signal). 없으면 None."""
    c = p.get("condition") if isinstance(p.get("condition"), dict) else {}
    if p.get("kind") == "gate" and c.get("type") == "command" and c.get("regex"):
        src = c["regex"]
    else:
        sig = p.get("signal") if isinstance(p.get("signal"), dict) else {}
        src = sig.get("regex") if sig.get("type") == "command" else None
    if not src:
        return None
    try:
        return re.compile(src, re.I | re.M)
    except re.error:
        return None


GUIDE_PATHS = (":(glob)**/AGENTS.md", ":(glob)**/CLAUDE.md", ":(glob)**/SKILL.md", ".claude/", ".codex/", ".cursor/")


def fix_tokens(p, rx=None, hit_cmds=()):
    """제안 문장의 `…` 토큰 중 고칠 말 — 신호에 걸리는 것·문제 명령 안에 그대로 나오는 것(문제 그 자체)·글자가 거의 없는 것은 뺀다."""
    text = " ".join(str(p.get(k) or "") for k in ("title", "rule_text", "scope", "blocks", "evidence"))
    out = []
    for t in _TICK.findall(text):
        t = t.strip()
        if len(t) < 4 or t in out or len(re.findall(r"[0-9A-Za-z가-힣]", t)) < 4:
            continue
        if rx is not None and rx.search(t):
            continue
        if any(t in c for c in hit_cmds):
            continue
        out.append(t)
    return out


def _fix_commit_at(root, tokens, since):
    """since 뒤에 토큰을 지침 문서(AGENTS.md·CLAUDE.md·스킬·에이전트 설정)에 더한 첫 커밋 시각(git log -S). 없으면 None."""
    first = None
    for tok in tokens[:config.get("diagnose.resolved_max_tokens", root)]:
        rc, out, _ = gitutil.git(root, "log", "--since=%s" % since.isoformat(), "-S", tok, "--format=%cI", "--",
                                 *GUIDE_PATHS, timeout=config.get("diagnose.resolved_git_timeout_s", root))
        for line in (out.splitlines() if rc == 0 else []):
            t = _aware(line.strip())
            if t is not None and t > since and (first is None or t < first):
                first = t
    return first


def resolution(root, p, sessions):
    """이미 풀린 문제인가. {"resolved", "last_hit", "fix_at", "sessions_after", "expected_after"} 또는 None(잴 수 없음).
    풀림 = 마지막 신호 뒤 고칠 말이 지침 문서에 커밋으로 들어갔고, 그 뒤 사람 대화에서 신호 0이며
    (대화 diagnose.resolved_min_sessions개 이상 또는 해결 전 빈도로 기대되는 신호 수 diagnose.resolved_expected_hits 이상)."""
    rx = regex_of(p)
    if rx is None:
        return None
    hits, mains = hit_times(rx, sessions)
    if not hits:
        return None
    last = hits[-1]
    hit_cmds = [c for s in sessions or [] for tc in s.get("tool_calls") or [] for c in tc.get("commands") or []
                if cmd_search(rx, c)]
    toks = fix_tokens(p, rx, hit_cmds)
    fix_at = _fix_commit_at(root, toks, last) if toks else None
    out = {"resolved": False, "last_hit": last.isoformat(), "fix_at": fix_at and fix_at.isoformat(),
           "sessions_after": 0, "tokens": toks[:5]}
    if fix_at is None:
        return out
    after = sum(1 for t in mains if t > fix_at)
    import datetime as _dt
    win = config.get("autoapply.secret_days", root)
    pre_s = [t for t in mains if last - _dt.timedelta(days=win) < t <= last]
    pre_h = [t for t in hits if last - _dt.timedelta(days=win) < t <= last]
    expected = (len(pre_h) / float(len(pre_s))) * after if pre_s else 0.0
    out.update(sessions_after=after, expected_after=round(expected, 1),
               resolved=after >= config.get("diagnose.resolved_min_sessions", root) or
               expected >= config.get("diagnose.resolved_expected_hits", root))
    return out


def recent_hits(rx, sessions, days, until=None):
    """until(기본 지금) 전 days일 안에 신호가 난 횟수와 그 신호가 난 대화 수. (hits, sessions)."""
    from vmc_harness.core.clock import now
    import datetime as _dt
    end = _aware(until) or now()
    start = end - _dt.timedelta(days=days)
    hits, sids = 0, set()
    for s in sessions or []:
        st = _aware(s.get("started_at"))
        for tc in s.get("tool_calls") or []:
            t = _aware(tc.get("at")) or st
            if t is None or not (start < t <= end):
                continue
            if any(cmd_search(rx, c) for c in tc.get("commands") or []):
                hits += 1
                sids.add(s.get("session_id"))
    return hits, len(sids)


def last_sessions_window(sessions, until, n):
    """until 전에 시작한 사람 대화 중 마지막 n개의 시작 시각 범위 (since, until) — n개가 안 되면 처음부터."""
    end = _aware(until)
    starts = sorted(t for t in (_aware(s.get("started_at")) for s in sessions or [] if s.get("kind", "main") == "main")
                    if t is not None and end is not None and t <= end)
    if not starts:
        return None, end
    pick = starts[-n:]
    import datetime as _dt
    return pick[0] - _dt.timedelta(seconds=1), end
