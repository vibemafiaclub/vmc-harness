"""진단 제안 자동 반영 (spec 07 §자동 반영 · 04 §판정 — 2026-11-16 최수민 승인 방향 A·B).

모델(진단)이 낸 것은 '발견'이고, 반영 여부는 코드가 필드·숫자로 정한다(01 P7). 사람 [추가]를 기다리지 않는다 —
검사를 전부 통과한 제안은 시험 중(trial) 조치로 반영하고, 알림 한 줄 + 원클릭 되돌리기(`vmc-harness revert A-…`).
잘못된 반영은 판정(ledger/judge.py)이 기계 지표로 원복한다(side_effects).

검사(하나라도 아니면 반영하지 않는다 — 결과는 진단 결과 `auto_checks`에 이유와 함께):
  enabled   autoapply.enabled(프로파일 gxp 끔)
  kind      autoapply.kinds 안의 종류
  conflict  human_rule_conflict.conflicts가 false — 칸이 없거나 형식이 틀리면 반영 안 함(모델 누락을 '충돌 없음'으로 읽지 않는다)
  tighten   gate·protected는 만드는 일 자체가 조이기(코드 사실). rule·structure는 direction == "tighten"이고
            제목·규칙 문장에 '안전 장치 + 푸는 말'(LOOSEN)이 없을 때
  domain    커널(.harness/ 상태·레포 밖 경로·하네스 엔진)·외부 발송·결제·삭제에 닿지 않는다(제목·규칙 문장·대상·조건 글자)
  backtest  gate: 하네스가 다시 돌린 백테스트(없으면 모델 값)가 있고 problem_changes ≥ autoapply.gate_min_problem_changes(실제 문제를 잡은 증거).
            조건이 db_direct·test_modify면 커널 안전 게이트가 이미 막는다 — 반영할 것 없음
  signal    rule·structure: 효과를 잴 신호(signal)가 형식에 맞고 지난 대화 기록에서 실제로 났다(대화당 횟수 > 0)
            — 판정이 반영 전후로 셀 수 없는 것은 자동으로 넣지 않는다
같은 대상(dedupe — 대상 파일·경로 glob·명령·게이트 조건·백테스트 장면)으로 이미 낸·반영한·제외한 제안은 diagnose.run이
제안 목록에서 먼저 빼고 앞 것에 묶는다(repeats).

반영 경로는 기존 것을 그대로 쓴다: gate·rule·protected는 diagnose.apply_proposal(사람 [추가]와 같은 함수, 근거만 diagnosis_auto),
structure는 루프 티켓(loop.tickets — worktree·검사) → loop.merge(자동 머지)를 진단 직후 유휴 조건 없이 돌린다(밤당 토큰 상한 그대로).
"""

import json
import os
import re
import sys
from pathlib import Path

from vmc_harness.core import config, events, ledger, notices, paths
from vmc_harness.core.clock import now_iso, parse_iso
from vmc_harness.core.jsonio import read_json, write_json

AUTO_KINDS = ("gate", "rule", "protected", "structure")
SIGNAL_TYPES = ("command", "path_write", "hook_error")

_SAFETY = r"(게이트|훅|hook|보호\s*영역|승인|허락|확인|검사|검증|선언|차단|가드|guard|안전|권한|permission|deny)"
_RELAX = (r"(풀|해제|제거|삭제|끄|꺼|없애|줄이|줄여|낮추|완화|생략|건너뛰|묻지\s*않|하지\s*않|빼|우회|"
          r"\bskip|\bdisable|\bremove|\bbypass|\bloosen)")
LOOSEN = re.compile(_SAFETY + r".{0,40}?" + _RELAX + "|" + _RELAX + r".{0,40}?" + _SAFETY, re.I | re.S)
DOMAIN = {
    "외부 발송": re.compile(r"(발송|메일|e-?mail|gmail|문자(?!열)|\bsms\b|알림톡|카카오|\bsend|\bpush\b|배포|deploy|publish|"
                        r"업로드|upload|webhook|슬랙|slack|디스코드|discord)", re.I),
    "결제": re.compile(r"(결제|환불|청구|이체|송금|payment|billing|invoice|세금계산서|stripe)", re.I),
    "삭제": re.compile(r"(삭제|지우|지워|\brm\s|\bdelete\b|drop\s+table|truncate|purge)", re.I),
    "커널": re.compile(r"(하네스\s*(엔진|코드|커널|레포)|커널|[/~]\S*vmc-harness)", re.I),
}
# 명령 차단 제안의 분류(evaluate) — 비밀값 반출은 사람 눈에 안 보여 정정·문제 커밋이 생기지 않는다(2026-11-18 실측: 정정 연결 0건)
SECRET_EXFIL = re.compile(r"(env\s+pull|\.env\b|비밀\s*값|시크릿|secret|token|토큰|api[_ -]?key|키체인|keychain|"
                          r"find-generic-password|credential|자격\s*증명|DATABASE_URL|접속\s*(문자열|정보)|password|비밀번호)", re.I)
HARNESS_CONFIG = ".harness/config.json"
HOOK_SETTINGS = (".claude/settings.json", ".claude/settings.local.json", ".codex/hooks.json")
HOOK_DIRS = (".claude/hooks/", ".codex/hooks/", ".githooks/", ".husky/")
HUMAN_DOCS = ("AGENTS.md", "CLAUDE.md")
_BLOCK_MARK = re.compile(r"(exit\s+2\b|\"decision\"\s*:\s*\"block\"|permissionDecision|\bdeny\b)", re.I)
_TICK = re.compile(r"`([^`\n]{2,200})`")


def _hist_path(root):
    from vmc_harness.bootstrap import diagnose
    return diagnose.history_path(root)


# ---------------------------------------------------------------- 사람 규칙 묶음(진단 참고 자료 human_rules.md)

def _strip_block(text):
    from vmc_harness.gates import rules as grules
    s, e = text.find(grules.BEGIN), text.find(grules.END)
    if s >= 0 and e > s:
        return text[:s] + text[e + len(grules.END):]
    return text


def human_rules_text(root):
    """레포 AGENTS.md·CLAUDE.md의 사람 영역(하네스 블록 밖)과 그 문서가 `@경로`로 불러오는 레포 안 문서(한 단계), 브레인 카드(켜진 것)의
    제목·판단. 진단 모델이 human_rule_conflict 칸을 채울 참고 자료 — 마스킹·길이 상한(autoapply.human_rules_chars)."""
    from vmc_harness.core.mask import mask
    root = Path(root)
    parts, seen = [], set()

    def add(rel):
        p = (root / rel).resolve()
        if rel in seen or not p.is_file() or root.resolve() not in p.parents:
            return None
        seen.add(rel)
        try:
            text = _strip_block(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return None
        parts.append("## %s\n%s" % (rel, text.strip()))
        return text

    for name in HUMAN_DOCS:
        text = add(name)
        for m in re.finditer(r"^@(\S+\.md)\s*$", text or "", re.M):
            add(m.group(1).lstrip("/"))
    try:
        from vmc_harness.brain import store
        cards = [c for c in store.list_cards() if c.get("status", "active") == "active"]
    except Exception:
        cards = []
    if cards:
        parts.append("## 브레인 카드\n" + "\n".join("- [%s] %s — %s" % (c.get("id"), c.get("title") or "",
                                                                      re.sub(r"\s+", " ", str(c.get("judgment") or ""))[:300])
                                                  for c in cards))
    out = mask("\n\n".join(parts) or "(사람 규칙 문서 없음)")
    lim = config.get("autoapply.human_rules_chars", root)
    return out if len(out) <= lim else out[:lim] + "\n…(길이 상한에서 자름)"


# ---------------------------------------------------------------- 검사

def _text(p):
    return " ".join(str(p.get(k) or "") for k in ("title", "rule_text"))


def _paths_of(p):
    out = [t for t in (p.get("targets") or []) if isinstance(t, str)]
    out += [g for g in (p.get("protected_globs") or []) if isinstance(g, str)]
    c = p.get("condition") if isinstance(p.get("condition"), dict) else {}
    for k in ("globs", "allowed"):
        out += [g for g in (c.get(k) or []) if isinstance(g, str)]
    return [_norm(x) for x in out if x.strip()]


def _norm(s):
    s = str(s).strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.rstrip("/")


def _kernel_path(rel):
    r = _norm(rel)
    if r.startswith(("/", "~", "../")) or re.match(r"^[A-Za-z]:/", r) or "vmc-harness" in r:
        return True
    return r.startswith(".harness/") and r != HARNESS_CONFIG


def domain_hits(p):
    """커널·외부 발송·결제·삭제 — 닿는 분야 이름 목록."""
    text = " ".join([_text(p), " ".join(_paths_of(p)),
                     json.dumps(p.get("condition") or {}, ensure_ascii=False)])
    hits = [name for name, rx in DOMAIN.items() if rx.search(text)]
    if "커널" not in hits and any(_kernel_path(x) for x in _paths_of(p)):
        hits.append("커널")
    return hits


def _backtest_of(p):
    bt = p.get("backtest")
    if isinstance(bt, dict) and not bt.get("error") and isinstance(bt.get("problem_changes"), int):
        return bt, "하네스 백테스트"
    mb = p.get("model_backtest")
    if isinstance(mb, dict) and isinstance(mb.get("problem_changes"), int):
        return mb, "진단 모델 값"
    return None, None


def signal_error(sig):
    if not isinstance(sig, dict) or sig.get("type") not in SIGNAL_TYPES:
        return "신호(signal) 없음 또는 type이 %s 중 하나가 아님" % "|".join(SIGNAL_TYPES)
    t = sig["type"]
    if t == "command":
        try:
            re.compile(str(sig.get("regex") or ""))
        except re.error:
            return "신호 regex 오류"
        if not str(sig.get("regex") or "").strip():
            return "신호 regex 없음"
    if t == "path_write" and not (isinstance(sig.get("globs"), list) and any(str(g).strip() for g in sig["globs"])):
        return "신호 globs 없음"
    if t == "hook_error" and not str(sig.get("label") or "").strip():
        return "신호 label 없음"
    return None


def _in_window(at, since, until):
    t = parse_iso(at) if isinstance(at, str) else None
    if t is None:
        return since is None and until is None
    t = t if t.tzinfo else t.astimezone()
    if since is not None and t <= since:
        return False
    if until is not None and t > until:
        return False
    return True


def signal_count(sig, sessions, since=None, until=None):
    """신호가 난 횟수 / 사람이 연 대화 수(since < 시작 ≤ until). {"hits", "sessions", "rate"} — rate는 대화가 없으면 None."""
    from vmc_harness.bootstrap import signals, util
    since = parse_iso(since) if isinstance(since, str) else since
    until = parse_iso(until) if isinstance(until, str) else until
    since = since.astimezone() if since is not None and since.tzinfo is None else since
    until = until.astimezone() if until is not None and until.tzinfo is None else until
    t = sig["type"]
    rx = re.compile(sig["regex"], re.I | re.M) if t == "command" else None
    label = str(sig.get("label") or "").lower()
    hits, mains = 0, 0
    for s in sessions or []:
        if not _in_window(s.get("started_at"), since, until):
            continue
        if s.get("kind", "main") == "main":
            mains += 1
        if t == "hook_error":
            hits += sum(1 for h in s.get("hook_runs") or [] if isinstance(h, dict) and h.get("error")
                        and label in ("%s %s" % (h.get("label") or "", h.get("key") or "")).lower())
            continue
        for tc in s.get("tool_calls") or []:
            if t == "command" and any(signals.cmd_search(rx, c) for c in tc.get("commands") or []):
                hits += 1
            elif t == "path_write" and tc.get("write") and any(util.any_glob(_norm(x), sig["globs"]) for x in tc.get("paths") or []):
                hits += 1
    return {"hits": hits, "sessions": mains, "rate": round(hits / float(mains), 4) if mains else None}


def evaluate(root, p, sessions=None):
    """제안 하나의 자동 반영 검사. {"eligible", "checks": {이름: bool}, "reasons": [빠진 이유], "signal_before"}."""
    from vmc_harness.bootstrap import diagnose
    kind = p.get("kind")
    checks, reasons, sig_before = {}, [], None

    def put(name, ok, why=None):
        checks[name] = bool(ok)
        if not ok and why:
            reasons.append(why)

    put("enabled", config.get("autoapply.enabled", root), "자동 반영 꺼짐(autoapply.enabled·프로파일)")
    put("kind", kind in config.get("autoapply.kinds", root) and kind in AUTO_KINDS, "자동 반영 종류 아님(%s)" % kind)
    c = p.get("human_rule_conflict")
    if not isinstance(c, dict) or not isinstance(c.get("conflicts"), bool):
        put("conflict", False, "사람 규칙 충돌 칸 없음 — 충돌 여부를 모름")
    else:
        put("conflict", c["conflicts"] is False, "사람 규칙과 충돌(%s)" % (c.get("with") or "위치 미상"))
    if kind in ("gate", "protected"):
        put("tighten", True)
    else:
        m = LOOSEN.search(_text(p))
        if p.get("direction") != "tighten":
            put("tighten", False, "조이는 변경인지 모름(direction=%s)" % (p.get("direction") or "없음"))
        else:
            put("tighten", not m, "안전 장치를 푸는 말: '%s'" % (m.group(0)[:40] if m else ""))
    dh = domain_hits(p)
    cls = gate_class(p)
    # 외부 발송·결제·삭제 분야는 푸는 변경에만 막는다 — 막기만 하는 게이트·보호 영역·조이는 규칙(외부 행동을 더 막는 것)은
    # 아래 분류 기준으로 본다. structure(그 분야 코드를 바꾸는 것)와 커널(하네스 상태·엔진)은 방향과 무관하게 막는다
    tightening = checks.get("tighten") and (kind in ("gate", "protected") or
                                            (kind == "rule" and p.get("direction") == "tighten"))
    blocked = [h for h in dh if h == "커널" or not tightening]
    put("domain", not blocked, "자동 반영 금지 분야: %s" % ", ".join(blocked))
    if kind == "gate":
        cond = p.get("condition") or {}
        bt, src = _backtest_of(p)
        if cond.get("type") in diagnose.KERNEL_GATE_TYPES:
            put("backtest", False, "커널 안전 게이트가 이미 막는 조건(%s)" % cond.get("type"))
        elif cls == "secret_exfil":
            ok, why, occ = secret_occurrence(root, p, sessions)
            put("backtest", ok, why)
            sig_before = occ
        elif cls == "external_action":
            # 외부 행동 자체를 막으면 정상 발송·발행이 다 막힌다(실측 send --confirm 20건 거의 전부 정상).
            # 형태 차단만 — 안전 스크립트가 생긴 뒤 그 우회를 막는 것(requires)
            put("backtest", bool(p.get("requires")),
                "외부 행동 자체를 막는 제안 — 형태 차단(requires로 켜는 우회 차단)만 자동 반영")
        elif bt is None:
            put("backtest", False, "백테스트 결과 없음")
        else:
            # problem_changes = 그 게이트가 막았을 변경 중 뒤에 고쳐지거나 되돌려진 것(실제 문제를 잡은 증거).
            # 실제 문제를 잡은 적 없는 게이트는 막는 비용만 남으니 자동 반영하지 않는다. 헛막기는 판정의 반박 비율로 원복한다
            lim = config.get("autoapply.gate_min_problem_changes", root)
            put("backtest", bt["problem_changes"] >= lim,
                "백테스트에서 실제 문제를 잡은 변경 %d건(%s) < %d" % (bt["problem_changes"], src, lim))
    else:
        put("backtest", True)
    if kind in ("rule", "structure"):
        err = signal_error(p.get("signal"))
        if err:
            put("signal", False, err)
        else:
            sig_before = signal_count(p["signal"], sessions if sessions is not None else diagnose.read_sessions_index(root),
                                      until=now_iso())
            put("signal", bool(sig_before["rate"]), "신호가 지난 대화 기록에 없음(%d/%d)" % (sig_before["hits"], sig_before["sessions"]))
    else:
        put("signal", True)
    return {"eligible": all(checks.values()), "checks": checks, "reasons": reasons, "signal_before": sig_before,
            "class": cls}


def gate_class(p):
    """명령 차단 제안 분류: secret_exfil | external_action | other (제목·막는 것·범위·조건 글자)."""
    if p.get("kind") != "gate":
        return "other"
    cond = p.get("condition") if isinstance(p.get("condition"), dict) else {}
    text = " ".join([str(p.get(k) or "") for k in ("title", "blocks", "scope")] + [str(cond.get("regex") or "")])
    if SECRET_EXFIL.search(text):
        return "secret_exfil"
    if DOMAIN["외부 발송"].search(text) or DOMAIN["결제"].search(text):
        return "external_action"
    return "other"


def secret_occurrence(root, p, sessions=None, until=None):
    """비밀값 반출 게이트 자동 반영 조건: 최근 autoapply.secret_days일 안 autoapply.secret_min_hits회 이상,
    autoapply.secret_min_sessions개 대화 이상, 이미 풀리지 않음(해결 이벤트). (ok, 이유, {"hits","sessions"})."""
    from vmc_harness.bootstrap import diagnose, signals
    rx = signals.regex_of(p)
    if rx is None:
        return False, "명령 조건이 아님 — 발생 횟수로 잴 수 없음", None
    sess = sessions if sessions is not None else diagnose.read_sessions_index(root)
    hits, nsess = signals.recent_hits(rx, sess, config.get("autoapply.secret_days", root), until)
    occ = {"hits": hits, "sessions": nsess}
    need_h, need_s = config.get("autoapply.secret_min_hits", root), config.get("autoapply.secret_min_sessions", root)
    if hits < need_h or nsess < need_s:
        return False, "최근 %d일 발생 %d회·%d개 대화 < %d회·%d개" % (
            config.get("autoapply.secret_days", root), hits, nsess, need_h, need_s), occ
    res = signals.resolution(root, p, sess)
    if res and res.get("resolved"):
        return False, "이미 풀린 문제(%s 커밋 뒤 대화 %d개 0건)" % ((res.get("fix_at") or "")[:16], res["sessions_after"]), occ
    return True, None, occ


# ---------------------------------------------------------------- 같은 대상 묶기

def _ticked(text):
    return sorted({_norm(x) for x in _TICK.findall(text or "") if x.strip()})


def signature(p):
    """같은 대상 판단 재료 — 종류·대상(파일·경로 glob·규칙 문장의 `…` 토큰)·명령 정규식·게이트 조건·백테스트 장면."""
    kind = p.get("kind")
    files = set(_paths_of(p))
    if kind == "rule":
        files |= set(_ticked(_text(p)))
    c = p.get("condition") if kind == "gate" and isinstance(p.get("condition"), dict) else {}
    cmds = {str(c["regex"])} if c.get("regex") else set()
    sg = p.get("signal") if isinstance(p.get("signal"), dict) else {}
    if sg.get("type") == "command" and sg.get("regex"):
        cmds.add(str(sg["regex"]))
    cmds = sorted(cmds)
    gate = json.dumps({k: (sorted(v) if isinstance(v, list) else v) for k, v in sorted(c.items())},
                      ensure_ascii=False, sort_keys=True) if c else None
    bt = p.get("backtest") if isinstance(p.get("backtest"), dict) else {}
    scenes = sorted({"%s|%s|%s" % (s.get("source"), s.get("ref"), s.get("at")) for s in bt.get("scenes") or []
                     if isinstance(s, dict)})
    return {"kind": kind, "files": sorted(files), "cmds": cmds, "gate": gate, "scenes": scenes}


def _jac(a, b):
    a, b = set(a or []), set(b or [])
    return len(a & b) / float(len(a | b)) if a and b else 0.0


def same_target(a, b, th, hits=None):
    """(같은가, 무엇으로). hits(정규식) → 지난 대화에서 그 정규식이 맞은 명령 집합 — 글자가 다른 정규식도 같은 명령을 잡으면 같은 대상
    (이름만 바꾼 재제안 — 2026-11-18 실측 43건 중 약 10건)."""
    if not a or not b or a.get("kind") != b.get("kind"):
        return False, None
    if a.get("gate") and a.get("gate") == b.get("gate"):
        return True, "게이트 조건"
    if set(a.get("cmds") or []) & set(b.get("cmds") or []):
        return True, "명령 패턴"
    if hits is not None and a.get("cmds") and b.get("cmds"):
        ha = set().union(*[hits(x) for x in a["cmds"]])
        hb = set().union(*[hits(x) for x in b["cmds"]])
        if _jac(ha, hb) >= th:
            return True, "명령 패턴(같은 명령을 잡음)"
    if _jac(a.get("files"), b.get("files")) >= th:
        return True, "대상 파일"
    if _jac(a.get("scenes"), b.get("scenes")) >= th:
        return True, "백테스트 장면"
    return False, None


def known_targets(root, hist, skip_titles=()):
    """이미 낸·반영한·제외한 것의 대상. [(sig, ref)] — ref: {"from", "title", "entry"|"candidate_id"}."""
    from vmc_harness.bootstrap import diagnose
    skip = {diagnose._key({"title": t}) for t in skip_titles}
    out = []
    for name in ("shown", "auto"):
        for e in hist.get(name) or []:
            if isinstance(e, dict) and e.get("sig") and diagnose._key(e) not in skip:
                out.append((e["sig"], {"from": "진단 이력", "title": e.get("title"), "entry": e}))
    for c in ledger.list_candidates(root):
        if c and c.get("kind") == "ticket" and c.get("targets") and diagnose._key({"title": c.get("summary")}) not in skip:
            out.append(({"kind": "structure", "files": sorted({_norm(t) for t in c["targets"]})},
                        {"from": "루프 티켓", "title": c.get("summary"), "candidate_id": c["candidate_id"]}))
    data = read_json(paths.harness_dir(root) / "protected.json", None) or {}
    by_action = {}
    for it in (data.get("paths", []) if isinstance(data, dict) else data) or []:
        g = it if isinstance(it, str) else (it or {}).get("glob")
        if g:
            by_action.setdefault((it.get("action_id") if isinstance(it, dict) else None) or "-", set()).add(_norm(g))
    for aid, gl in by_action.items():
        out.append(({"kind": "protected", "files": sorted(gl)}, {"from": "보호 영역", "title": ", ".join(sorted(gl)[:3])}))
    rules = read_json(paths.harness_dir(root) / "rules.json", None) or {}
    for r in (rules.get("rules") if isinstance(rules, dict) else rules) or []:
        if not isinstance(r, dict) or r.get("enabled") is False:
            continue
        toks = _ticked("%s %s" % (r.get("rule_text") or "", (r.get("basis") or {}).get("quote") or ""))
        if toks:
            out.append(({"kind": "rule", "files": toks}, {"from": "규칙", "title": (r.get("rule_text") or "")[:80]}))
    return out


def dedupe(root, proposals, hist, write=True, skip_titles=()):
    """같은 대상으로 이미 있는 것과 같은 제안은 빼고 앞 것에 묶는다(repeats + 1, 티켓은 count + 1). (남은 것, 묶은 것)."""
    th = config.get("autoapply.same_target_jaccard", root)
    known = known_targets(root, hist, skip_titles)
    hits = command_hits(root)
    keep, merged = [], []
    for p in proposals:
        sig = signature(p)
        hit = None
        for ks, ref in known:
            same, by = same_target(sig, ks, th, hits)
            if same:
                hit = (ref, by)
                break
        if not hit:
            keep.append(p)
            known.append((sig, {"from": "이번 진단", "title": p.get("title"), "entry": None}))
            continue
        ref, by = hit
        merged.append({"title": p.get("title"), "kind": p.get("kind"), "into": ref.get("title"), "from": ref["from"], "by": by})
        if write:
            if ref.get("entry") is not None:
                # 기존 제안 갱신 — 다시 났다는 사실과 마지막 근거를 앞 것에 싣는다(새 제안으로 다시 묻지 않는다)
                ref["entry"]["repeats"] = int(ref["entry"].get("repeats") or 0) + 1
                ref["entry"]["last_seen"] = now_iso()
                ref["entry"]["last_evidence"] = str(p.get("evidence") or "")[:300]
            if ref.get("candidate_id"):
                c = ledger.get_candidate(root, ref["candidate_id"]) or {}
                ledger.update_candidate(root, ref["candidate_id"], count=int(c.get("count") or 1) + 1)
    return keep, merged


def command_hits(root, sessions=None):
    """정규식 → 지난 대화에서 그 정규식이 맞은 명령 id 집합(언급 제외). 같은 정규식은 한 번만 센다."""
    from vmc_harness.bootstrap import diagnose, signals
    cache, rows = {}, []
    for s in (sessions if sessions is not None else diagnose.read_sessions_index(root)):
        for i, tc in enumerate(s.get("tool_calls") or []):
            for j, c in enumerate(tc.get("commands") or []):
                rows.append(("%s#%d.%d" % (s.get("session_id"), i, j), c))

    def hits(src):
        if src not in cache:
            try:
                rx = re.compile(src, re.I | re.M)
            except re.error:
                cache[src] = frozenset()
            else:
                cache[src] = frozenset(k for k, c in rows if signals.cmd_search(rx, c))
        return cache[src]
    return hits


# ---------------------------------------------------------------- 반영 전 기준값(판정이 전후를 비교한다)

def _tests_now(root):
    from vmc_harness.bootstrap import facts
    raw = (facts.load_raw(root) or {}).get("facts") or {}
    read_at = (raw.get("test_baseline") or {}).get("read_at")
    fails = (facts.load(root).get("test_baseline") or {}).get("failures") or []
    return {"failures": sorted(set(fails)), "read_at": read_at}


def snapshot(root, p, ev):
    snap = {"title": (p.get("title") or "")[:120], "kind": p.get("kind"), "tests_before": _tests_now(root)}
    if ev.get("signal_before"):
        snap.update(signal=p.get("signal"), signal_before=ev["signal_before"])
    return snap


def auto_basis(d, p, checks):
    from vmc_harness.loop import tickets
    return {"type": "diagnosis_auto", "ref": "diagnose:%s#%s" % ((d or {}).get("generated_at") or "-", p.get("no")),
            "key": tickets.proposal_key(p), "checks": dict(checks)}


def notice_line(title, action_id):
    return "진단 제안 자동 반영: %s — 되돌리기: vmc-harness revert %s" % (title, action_id)


# ---------------------------------------------------------------- 반영

def apply(root, d=None, structure=True, dry_run=False):
    """진단 결과의 제안을 검사해 통과한 것을 반영한다. dry_run이면 아무것도 쓰지 않고 표만.
    {"rows": [{"no","title","kind","eligible","reasons","result"}], "pending": [structure 티켓 id]}"""
    from vmc_harness.bootstrap import diagnose
    from vmc_harness.loop import tickets
    d = d if d is not None else (diagnose.load(root) or {})
    hist = read_json(_hist_path(root), {}) or {}
    sess = diagnose.read_sessions_index(root)
    rows, changed = [], False
    for p in d.get("proposals") or []:
        if not isinstance(p, dict):
            continue
        row = {"no": p.get("no"), "title": p.get("title"), "kind": p.get("kind")}
        done = p.get("accepted") and "사람이 [추가]함" or p.get("excluded") and "사람이 [제외]함" or \
            p.get("auto_applied") and "이미 자동 반영함"
        if done:
            rows.append(dict(row, eligible=False, reasons=[done], result="skip"))
            continue
        ev = evaluate(root, p, sess)
        row.update(eligible=ev["eligible"], reasons=ev["reasons"], checks=ev["checks"])
        if dry_run or not ev["eligible"]:
            if not dry_run:
                p["auto_checks"] = {"at": now_iso(), "checks": ev["checks"], "reasons": ev["reasons"]}
                changed = True
            rows.append(dict(row, result="dry_run" if dry_run else "excluded"))
            continue
        basis, snap = auto_basis(d, p, ev["checks"]), snapshot(root, p, ev)
        p["auto_checks"] = {"at": now_iso(), "checks": ev["checks"], "reasons": []}
        changed = True
        if p["kind"] == "structure":
            cid = p.get("ticket_id") or tickets.register_ticket(root, p)["candidate_id"]
            p["ticket_id"] = cid
            ledger.update_candidate(root, cid, auto_apply=True, auto_basis=basis, auto_snapshot=snap)
            p["auto_applied"] = {"at": now_iso(), "ticket_id": cid}
            _mark_auto(hist, p)
            events.log(root, "diagnosis_auto_queued", candidate_id=cid, proposal_kind="structure")
            rows.append(dict(row, result="queued", ticket_id=cid))
            continue
        try:
            r = diagnose.apply_proposal(root, p, basis)
        except (ledger.MetaGateError, ValueError) as e:
            r = {"error": str(e)}
        if r.get("error") or not r.get("action_id"):
            p["auto_checks"]["reasons"] = ["반영 실패: %s" % str(r.get("error"))[:200]]
            rows.append(dict(row, result="error", error=r.get("error")))
            continue
        ledger.update_action(root, r["action_id"], auto=snap)
        p["auto_applied"] = {"at": now_iso(), "action_id": r["action_id"]}
        _mark_auto(hist, p)
        hist.setdefault("auto", []).append({"title": p["title"], "at": p["auto_applied"]["at"], "kind": p["kind"],
                                            "action_id": r["action_id"], "sig": signature(p)})
        notices.add(root, "diagnosis_auto_applied", "flow", notice_line(p["title"], r["action_id"]),
                    refs={"action_id": r["action_id"]})
        events.log(root, "diagnosis_auto_applied", action_id=r["action_id"], proposal_kind=p["kind"])
        rows.append(dict(row, result="applied", action_id=r["action_id"]))
    if changed and not dry_run:
        write_json(diagnose.diagnosis_path(root), d)
        write_json(_hist_path(root), hist)
    pending = [] if dry_run else queued_tickets(root)
    out = {"rows": rows, "pending": pending}
    if structure and pending and not dry_run:
        out["structure"] = run_structure(root, pending)
    return out


def _mark_auto(hist, p):
    """사람이 고른 제안이 아니다 — 진단 이력에 표시해 수용률 분모에서 뺀다."""
    from vmc_harness.bootstrap import diagnose
    for e in hist.get("shown") or []:
        if isinstance(e, dict) and diagnose._key(e) == diagnose._key(p):
            e["auto"] = True


def queued_tickets(root):
    return [c["candidate_id"] for c in ledger.list_candidates(root)
            if c and c.get("kind") == "ticket" and c.get("auto_apply") and c.get("status") in (None, "open")]


def run_structure(root, cids):
    """자동 반영 structure 티켓을 지금 처리(worktree — 유휴 조건 없이, 밤당 토큰 상한 그대로) → 검사를 통과해 준비된 것은 바로 머지.
    사람 미커밋 변경과 겹치면 그날은 건너뛴다(loop.merge). 못 끝낸 티켓은 밤 루프·다음 진단이 이어 받는다."""
    from vmc_harness.loop import merge, tick
    res = tick.run_tickets(root, cids, trigger="diagnose")
    acts = [a for a in merge.prepared_actions(root)
            if (a.get("basis") or {}).get("type") == "diagnosis_auto" and a.get("candidate_id") in cids]
    res["merge"] = merge.merge_actions(root, acts) if acts else []
    return res


def after_diagnosis(root, d, inline=False):
    """진단 직후(diagnose.run). gate·rule·protected는 그 자리에서, structure는 inline이면 그 자리에서·아니면 분리 실행."""
    if not config.get("autoapply.enabled", root):
        return {"enabled": False}
    try:
        out = apply(root, d, structure=inline)
    except Exception as e:  # 편의 경로 — 자동 반영 고장이 진단을 멈추지 않게. 기록은 남긴다
        events.log(root, "diagnosis_auto_error", error=str(e)[:300])
        return {"error": str(e)[:300]}
    if not inline and out.get("pending"):
        from vmc_harness.core import llm
        if not llm.nested():
            _spawn(root)
            out["spawned"] = True
    return out


def _spawn(root):
    from vmc_harness.core.proc import spawn_detached
    env = dict(os.environ)
    eng = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = eng + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        spawn_detached([sys.executable, "-m", "vmc_harness", "diagnose", "autoapply"], cwd=str(root), env=env)
    except Exception as e:
        events.log(root, "diagnosis_auto_error", error="분리 실행 시작 실패: %s" % str(e)[:200])


# ---------------------------------------------------------------- structure 결과물 검사(worktree diff — 조이기만)

def _git_show(wt, ref, rel):
    from vmc_harness.core.gitutil import git
    rc, out, _ = git(wt, "show", "%s:%s" % (ref, rel))
    return out if rc == 0 else None


def _hook_counts(data):
    hooks = (data or {}).get("hooks") if isinstance(data, dict) else None
    out = {}
    for ev, groups in (hooks or {}).items():
        n = 0
        for g in groups if isinstance(groups, list) else []:
            n += len([h for h in (g or {}).get("hooks") or [] if isinstance(h, dict)]) if isinstance(g, dict) else 0
        out[ev] = n
    return out


def _deny(data):
    perm = (data or {}).get("permissions") if isinstance(data, dict) else None
    return set(map(str, (perm or {}).get("deny") or [])) if isinstance(perm, dict) else set()


def _flatten(v, pre=""):
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            out.update(_flatten(x, "%s/%s" % (pre, k)))
        return out
    return {pre: json.dumps(v, sort_keys=True, ensure_ascii=False)}


def diff_problems(root, wt, base, head, files):
    """자동 반영 structure 변경이 지켜야 할 것(기계 검사). 어긴 것 목록 — 비면 통과.
    파일 삭제 없음 · 하네스 상태(.harness/) 안 바꿈(config.json은 키 추가만) · 훅 설정의 훅 수가 줄지 않고 deny가 빠지지 않음 ·
    훅 스크립트의 막는 표지(exit 2·block·deny)가 줄지 않음 · JSON은 읽힌다 · AGENTS.md·CLAUDE.md는 하네스 블록 밖을 안 바꿈 ·
    변경 크기 ≤ loop.max_change_lines."""
    from vmc_harness.core.gitutil import git
    from vmc_harness.loop import gitops
    bad = []
    rc, out, _ = git(wt, "diff", "--name-status", "--no-renames", base, head)
    for line in out.splitlines() if rc == 0 else []:
        st, _, rel = line.partition("\t")
        if st.startswith("D"):
            bad.append("파일 삭제: %s" % rel)
    for rel in files:
        r = _norm(rel)
        old, new = _git_show(wt, base, r), _git_show(wt, head, r)
        if r.endswith(".json") and new is not None:
            try:
                nj = json.loads(new)
            except ValueError:
                bad.append("JSON이 읽히지 않음: %s" % r)
                continue
            oj = None
            if old is not None:
                try:
                    oj = json.loads(old)
                except ValueError:
                    oj = None
            if r in HOOK_SETTINGS and oj is not None:
                oc, nc = _hook_counts(oj), _hook_counts(nj)
                fewer = [e for e, n in oc.items() if nc.get(e, 0) < n]
                if fewer:
                    bad.append("훅이 줄어듦(%s): %s" % (", ".join(fewer), r))
                if _deny(oj) - _deny(nj):
                    bad.append("권한 deny가 빠짐: %s" % r)
                if nj.get("disableAllHooks") and not oj.get("disableAllHooks"):
                    bad.append("훅 전체 끄기: %s" % r)
            if r == HARNESS_CONFIG and oj is not None:
                fo, fn = _flatten(oj), _flatten(nj)
                if [k for k in fo if fn.get(k) != fo[k]]:
                    bad.append("하네스 설정의 기존 값 변경: %s" % r)
        if _kernel_path(r):
            bad.append("하네스 상태·레포 밖 경로: %s" % r)
        if old is not None and new is not None and (r.startswith(HOOK_DIRS) or "hook" in Path(r).name.lower()):
            if len(_BLOCK_MARK.findall(new)) < len(_BLOCK_MARK.findall(old)):
                bad.append("훅 스크립트의 막는 표지가 줄어듦: %s" % r)
        if Path(r).name in HUMAN_DOCS and old is not None and new is not None and _strip_block(old) != _strip_block(new):
            bad.append("사람이 쓴 지침(%s)의 하네스 블록 밖을 바꿈" % r)
    size = gitops.diff_size(wt, base, head)
    lim = config.get("loop.max_change_lines", root)
    if size > lim:
        bad.append("변경 크기 %d줄 > %d" % (size, lim))
    return bad


# ---------------------------------------------------------------- 판정용 지표(ledger.judge가 부른다 — 코드로 센 숫자만)

def signal_after(root, action):
    """{"before", "after", "sessions", "hits"[, "undeterminable"]} — 반영 뒤 사람이 연 대화에서 근거 신호의 대화당 횟수. 잴 수 없으면 None.
    before는 반영 직전 대화 judge.auto_signal_before_sessions개에서 다시 잰다(진단 때 잰 200세션 창 값이 아니다) — 그 사이 다른 변경으로
    이미 줄어든 신호를 이 조치 공으로 돌리지 않게. 그 값이 judge.auto_signal_near_zero 이하면 효과를 잴 수 없다(undeterminable)."""
    from vmc_harness.bootstrap import diagnose, signals
    auto = action.get("auto") or {}
    sig, snap = auto.get("signal"), auto.get("signal_before") or {}
    if signal_error(sig) or snap.get("rate") is None:
        return None
    start = action.get("merged_at") or (action.get("trial") or {}).get("started_at") or action.get("created_at")
    sess = diagnose.read_sessions_index(root)
    w_since, w_until = signals.last_sessions_window(sess, start, config.get("judge.auto_signal_before_sessions", root))
    recent = signal_count(sig, sess, since=w_since, until=w_until) if w_until is not None else {"rate": None, "sessions": 0}
    after = signal_count(sig, sess, since=start)
    out = {"before": recent["rate"], "before_sessions": recent["sessions"], "before_snapshot": snap["rate"],
           "after": after["rate"], "sessions": after["sessions"], "hits": after["hits"]}
    if recent["rate"] is None or recent["rate"] <= config.get("judge.auto_signal_near_zero", root):
        out["undeterminable"] = True
    return out


def new_test_failures(root, action):
    """반영 뒤 다시 잰 테스트 기준선에서 그 조치가 바꾼 파일·모듈의 새 실패 id. 잴 수 없으면(파일 없음·그 뒤로 안 잼) None."""
    from vmc_harness.loop import detect
    auto = action.get("auto") or {}
    tb, files = auto.get("tests_before"), [_norm(f) for f in action.get("files") or []]
    if not tb or not files:
        return None
    cur = _tests_now(root)
    t_cur, t_app = parse_iso(cur.get("read_at")), parse_iso(action.get("merged_at") or action.get("created_at"))
    if not t_cur or not t_app or t_cur <= t_app:
        return None
    mods = {detect.module_of(root, f) for f in files}
    new = set(cur["failures"]) - set(tb.get("failures") or [])
    return sorted(x for x in new if x.split("::")[0] in files or detect.module_of(root, x.split("::")[0]) in mods)


# ---------------------------------------------------------------- 드라이런(쓰기 없음)

def dry_run(root):
    """지금 진단 결과의 제안마다 검사 결과 + 진단 이력 전체의 같은 대상 묶음·글자 검사. 아무것도 쓰지 않는다."""
    from vmc_harness.bootstrap import diagnose
    d = diagnose.load(root) or {}
    hist = read_json(_hist_path(root), {}) or {}
    props = [dict(p) for p in d.get("proposals") or [] if isinstance(p, dict)]
    cur = apply(root, dict(d, proposals=props), structure=False, dry_run=True)["rows"]
    _, merged = dedupe(root, props, hist, write=False, skip_titles=[p.get("title") for p in props])
    by_title = {m["title"]: m for m in merged}
    for r in cur:
        if r["title"] in by_title:
            r["merged"] = by_title[r["title"]]
    tickets = {c.get("summary"): c for c in ledger.list_candidates(root) if c and c.get("kind") == "ticket"}
    past = []
    for e in hist.get("shown") or []:
        if not isinstance(e, dict):
            continue
        c = tickets.get(e.get("title"))
        p = {"title": e.get("title"), "kind": e.get("kind") or ("structure" if e.get("ticket") else None),
             "targets": (c or {}).get("targets") or []}
        dh = domain_hits(p)
        m = LOOSEN.search(_text(p))
        past.append({"at": (e.get("at") or "")[:10], "title": e.get("title"), "kind": p["kind"] or "gate|rule|protected",
                     "targets": p["targets"], "domain": dh, "loosen": m.group(0) if m else None,
                     "sig": signature(p) if p["targets"] else None})
    th = config.get("autoapply.same_target_jaccard", root)
    for i, a in enumerate(past):
        for b in past[:i]:
            if a["sig"] and b["sig"] and same_target(a["sig"], b["sig"], th)[0]:
                a["same_as"] = b["title"]
                break
    return {"current": cur, "history": past, "excluded_titles": hist.get("excluded") or [],
            "accepted": hist.get("accepted") or []}
