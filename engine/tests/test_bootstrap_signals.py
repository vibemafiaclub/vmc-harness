"""진단 신호 공통(언급 제외·해결 이벤트)·명령 차단 제안 분류·판정 기준선·지침 충돌 부정 (2026-11-18 실측 반영). 합성 데이터."""

import datetime as _dt
import re
import subprocess

from tests.util import HarnessTestCase
from vmc_harness.bootstrap import autoapply, diagnose, signals
from vmc_harness.core import config, events, ledger, notices
from vmc_harness.core.clock import now
from vmc_harness.ledger import judge


def _s(sid, at, cmds=()):
    at = at.isoformat(timespec="seconds")
    return {"session_id": sid, "kind": "main", "started_at": at,
            "tool_calls": [{"at": at, "name": "Bash", "commands": list(cmds)}] if cmds else []}


class MentionTest(HarnessTestCase):
    def test_mentions_are_not_signal(self):
        rx = re.compile(r"\.env\.local[^\n]*DATABASE_URL", re.I)
        self.assertFalse(signals.cmd_search(rx, "grep -n 'DATABASE_URL' .env.local"))  # 순서가 달라도
        self.assertFalse(signals.cmd_search(rx, 'git commit -m "fix: .env.local DATABASE_URL 정리"'))
        self.assertFalse(signals.cmd_search(rx, 'vmc-harness declare --intent ".env.local DATABASE_URL 읽기 금지"'))
        self.assertFalse(signals.cmd_search(rx, "echo .env.local DATABASE_URL"))
        self.assertTrue(signals.cmd_search(rx, "source .env.local; psql $DATABASE_URL"))
        # heredoc 본문은 뺀다고 보지 않는다 — 인라인 스크립트가 실제로 읽는다
        self.assertTrue(signals.cmd_search(rx, "python3 - <<'EOF'\nopen('.env.local') # DATABASE_URL\nEOF"))


class ResolutionTest(HarnessTestCase):
    def _proposal(self):
        return {"kind": "rule", "title": "운영 DB는 `pnpm -C packages/db q` 로만 본다",
                "rule_text": "운영 DB 조회는 `pnpm -C packages/db q`로만 하고 `.env.local`의 값을 셸로 불러오지 않는다.",
                "signal": {"type": "command", "regex": r"\.env\.local[^\n]*DATABASE_URL"}}

    def test_fix_commit_then_quiet_sessions_is_resolved(self):
        t0 = now() - _dt.timedelta(days=3)
        sess = [_s("a%d" % i, t0 + _dt.timedelta(minutes=i), ["source .env.local; echo x; psql $DATABASE_URL"]) for i in range(3)]
        self.write("AGENTS.md", "운영 DB 조회는 `pnpm -C packages/db q`로만 한다\n")
        self.commit("docs: q")
        need = config.get("diagnose.resolved_min_sessions", self.root)
        later = now() + _dt.timedelta(minutes=1)
        sess_more = sess + [_s("b%d" % i, later + _dt.timedelta(minutes=i)) for i in range(need)]
        r = signals.resolution(self.root, self._proposal(), sess_more)
        self.assertTrue(r["resolved"], r)
        r = signals.resolution(self.root, self._proposal(), sess + [_s("b0", later)])
        self.assertFalse(r["resolved"])  # 해결 이벤트 뒤 대화가 모자라면 아직 모른다

    def test_no_fix_event_is_not_resolved_even_if_quiet(self):
        # 이틀 조용해도 고칠 말이 커밋으로 안 들어갔으면 풀린 것이 아니다(재발 실측)
        t0 = now() - _dt.timedelta(days=3)
        sess = [_s("a", t0, ["source .env.local; psql $DATABASE_URL"])] + \
               [_s("b%d" % i, t0 + _dt.timedelta(hours=1, minutes=i)) for i in range(40)]
        r = signals.resolution(self.root, self._proposal(), sess)
        self.assertFalse(r["resolved"])
        self.assertIsNone(r["fix_at"])


class GateClassTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        cfg = self.root / ".harness" / "config.json"
        import json
        d = json.loads(cfg.read_text())
        d["profile"] = "solo"
        cfg.write_text(json.dumps(d))

    def _p(self, title, regex, **kw):
        return dict({"title": title, "kind": "gate", "evidence": "e", "blocks": title, "scope": "Bash", "cost": "c",
                     "condition": {"type": "command", "regex": regex}, "direction": "tighten",
                     "human_rule_conflict": {"conflicts": False}, "backtest": {"would_block": 3, "problem_changes": 0}}, **kw)

    def test_secret_exfil_by_occurrence(self):
        p = self._p("운영 환경변수 `vercel env pull --environment=production` 막기", r"vercel env pull[^;&|]*--environment[= ]?prod")
        self.assertEqual(autoapply.gate_class(p), "secret_exfil")
        t = now() - _dt.timedelta(days=1)
        cmd = "vercel env pull /tmp/e --environment=production --yes"
        sess = [_s("a", t, [cmd]), _s("b", t + _dt.timedelta(hours=1), [cmd]), _s("b", t + _dt.timedelta(hours=2), [cmd])]
        ev = autoapply.evaluate(self.root, p, sess)
        self.assertTrue(ev["eligible"], ev["reasons"])
        ev = autoapply.evaluate(self.root, p, sess[:1])
        self.assertFalse(ev["eligible"])
        self.assertIn("최근 7일 발생 1회", " ".join(ev["reasons"]))
        # 언급만 있으면 세지 않는다
        sess2 = [_s("a", t, ["grep -n 'vercel env pull --environment=production' notes.md"] * 3),
                 _s("b", t, ["echo vercel env pull --environment=production"] * 3)]
        self.assertFalse(autoapply.evaluate(self.root, p, sess2)["eligible"])

    def test_external_action_only_shape_blocks(self):
        p = self._p("단체 문자 발송 막기", r"send\.py[^;&|]*--confirm")
        self.assertEqual(autoapply.gate_class(p), "external_action")
        ev = autoapply.evaluate(self.root, p, [])
        self.assertFalse(ev["eligible"])
        self.assertIn("형태 차단", " ".join(ev["reasons"]))
        p2 = dict(p, title="발행 스크립트가 생긴 뒤 세금계산서 직접 발행 막기", requires=[{"type": "file_exists", "path": "x.sh"}])
        ev = autoapply.evaluate(self.root, p2, [])
        self.assertTrue(ev["checks"]["domain"], ev["reasons"])  # 조이는 게이트는 분야 이름만으로 막지 않는다
        self.assertTrue(ev["checks"]["backtest"], ev["reasons"])

    def test_renamed_reproposal_merges_by_same_commands(self):
        t = now()
        sess = [_s("a", t, ["vercel env pull /tmp/e --environment=production"]),
                _s("b", t, ["vercel env pull x --environment production --yes"])]
        hits = autoapply.command_hits(self.root, sess)
        a = autoapply.signature(self._p("A", r"vercel env pull[^;&|]*--environment[= ]?prod"))
        b = autoapply.signature(self._p("B", r"vercel\s+env\s+pull.*production"))
        self.assertEqual(autoapply.same_target(a, b, 0.6, hits), (True, "명령 패턴(같은 명령을 잡음)"))
        self.assertFalse(autoapply.same_target(a, b, 0.6)[0])


class JudgeBaselineTest(HarnessTestCase):
    def _auto_rule(self, before_rate=0.45):
        a = ledger.create_action(self.root, {"type": "diagnosis_auto", "ref": "diagnose:x#1", "key": "k", "checks": {"signal": True}},
                                 "rule", "운영 DB 접속 문자열을 셸로", why_not_higher="기계로 판정할 수 없음", target={"rule_id": "r-1"})
        ledger.update_action(self.root, a["action_id"], auto={
            "signal": {"type": "command", "regex": r"\.env\.local[^\n]*DATABASE_URL"},
            "signal_before": {"hits": 89, "sessions": 200, "rate": before_rate}})
        return ledger.get_action(self.root, a["action_id"])

    def test_signal_already_gone_before_apply_is_not_credited(self):
        a = self._auto_rule()
        created = _dt.datetime.fromisoformat(a["created_at"])
        old = [_s("o%d" % i, created - _dt.timedelta(days=3, minutes=i), ["source .env.local; psql $DATABASE_URL"]) for i in range(30)]
        quiet = [_s("q%d" % i, created - _dt.timedelta(hours=10, minutes=i)) for i in range(25)]  # 다른 변경으로 이미 0
        after = [_s("n%d" % i, created + _dt.timedelta(minutes=5 + i)) for i in range(30)]
        diagnose.write_sessions_index(self.root, old + quiet + after)
        sg = autoapply.signal_after(self.root, a)
        self.assertTrue(sg["undeterminable"], sg)
        at = created + _dt.timedelta(days=config.get("judge.min_days", self.root), hours=1)
        res, reason, nums = judge.evaluate(self.root, a, [], [], at)
        self.assertNotEqual(res, "kept", reason)

    def test_signal_present_right_before_and_dropped_keeps(self):
        a = self._auto_rule()
        created = _dt.datetime.fromisoformat(a["created_at"])
        before = [_s("p%d" % i, created - _dt.timedelta(hours=5, minutes=i), ["source .env.local; psql $DATABASE_URL"]) for i in range(25)]
        after = [_s("n%d" % i, created + _dt.timedelta(minutes=5 + i)) for i in range(30)]
        diagnose.write_sessions_index(self.root, before + after)
        at = created + _dt.timedelta(days=config.get("judge.min_days", self.root), hours=1)
        res, reason, _ = judge.evaluate(self.root, a, [], [], at)
        self.assertEqual(res, "kept", reason)


class ConflictNegativeTest(HarnessTestCase):
    def _card_action(self):
        return ledger.create_action(self.root, {"type": "learning", "session": "s"}, "card", "확인하라",
                                    why_not_higher="취향", target={"card": "K-1"})

    def test_two_undismissed_conflicts_revert(self):
        a = self._card_action()
        notices.add(self.root, "guide_conflict", "flow", "x", refs={"guide_id": "K-1", "session_id": "S1"})
        self.assertIsNone(judge.evaluate(self.root, a, [], [])[0])
        events.log(self.root, "guide_conflict_seen", guide_id="K-1", session_id="S2")
        res, reason, nums = judge.evaluate(self.root, a, [], [])
        self.assertEqual(res, "reverted", reason)
        self.assertEqual(nums["conflicts"], 2)

    def test_dismissed_conflicts_do_not_count(self):
        a = self._card_action()
        notices.add(self.root, "guide_conflict", "flow", "x", refs={"guide_id": "K-1", "session_id": "S1"})
        events.log(self.root, "guide_conflict_seen", guide_id="K-1", session_id="S2")
        events.log(self.root, "guide_conflict_dismissed", guide_id="K-1", notice_id="n")
        self.assertIsNone(judge.evaluate(self.root, a, [], [])[0])
