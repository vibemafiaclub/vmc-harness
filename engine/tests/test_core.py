from tests.util import HarnessTestCase

from vmc_harness.core import config, ledger, inject, notices
from vmc_harness.core.ledger import MetaGateError


class CoreTest(HarnessTestCase):
    def test_config_profile(self):
        self.assertEqual(config.get("judge.exposures", self.root), 10)
        self.assertTrue(config.get("loop.enabled", self.root))
        self.assertFalse(config.get("loop.enabled", profile="gxp"))
        with self.assertRaises(KeyError):
            config.get("nope.key")

    def test_finding_only_explicit(self):
        with self.assertRaises(MetaGateError):
            ledger.create_finding(self.root, "llm_correction", {"action_id": "A-1"})
        f = ledger.create_finding(self.root, "revert", {"action_id": "A-1"})
        self.assertTrue(f["finding_id"].startswith("F-"))

    def test_learning_card_only(self):
        with self.assertRaises(MetaGateError):
            ledger.create_action(self.root, {"type": "learning", "ref": "C-1"}, "rule", "h", why_not_higher="x")
        with self.assertRaises(MetaGateError):
            ledger.create_action(self.root, {"type": "learning", "ref": "C-1"}, "card", "h")
        a = ledger.create_action(self.root, {"type": "learning", "ref": "C-1"}, "card", "h", why_not_higher="취향")
        self.assertEqual(a["status"], "trial")

    def test_gate_basis(self):
        with self.assertRaises(MetaGateError):
            ledger.create_action(self.root, {"type": "cleansing"}, "gate", "h")
        with self.assertRaises(MetaGateError):
            ledger.create_action(self.root, {"type": "explicit_request"}, "gate", "h")
        ledger.create_action(self.root, {"type": "explicit_request", "quote": "앞으로 이건 막아"}, "gate", "h")

    def test_approval_reject_is_finding(self):
        p = ledger.create_approval(self.root, "T-1", 1, "운영 반영", "approve", True, "git revert", ["pytest ok"], 6)
        ledger.answer_approval(self.root, p["approval_id"], "reject")
        self.assertEqual(ledger.list_findings(self.root)[0]["kind"], "reject")

    def test_inject_allowlist(self):
        self.assertIsNone(inject.message("budget", "x"))
        self.assertIsNone(inject.message("gate_violation", "P80 넘음"))
        self.assertTrue(inject.message("gate_violation", "DB 직접 접속 차단됨"))
        # 숫자 압박만 걸러낸다 — 파일 경로 속 낱말 token은 통과
        self.assertTrue(inject.message("gate_violation", "보호 영역 쓰기 차단: auth/token.py, src/tokens/refresh_token.ts"))
        for t in ("예산 초과", "토큰 4만 썼음", "40000 tokens", "tokens: 300", "8분 지남", "남은 시간 3분", "90% 사용"):
            self.assertIsNone(inject.message("gate_violation", t), t)

    def test_mask_json_and_strings(self):
        from vmc_harness.core.mask import mask, mask_obj
        self.assertEqual(mask('{"password": "x", "user": "a"}'), '{"password": "****", "user": "a"}')
        self.assertEqual(mask('{"password": ""}'), '{"password": ""}')
        self.assertEqual(mask("Server=a;Password=Hunter 2;"), "Server=a;Password=****;")
        self.assertEqual(mask('-H "Authorization: Bearer abcdefghijk123"'), '-H "Authorization: Bearer ****"')
        self.assertEqual(mask("auth/token.py"), "auth/token.py")
        self.assertEqual(mask_obj({"password": "x", "a": [{"api_key": "k"}], "s": "pwd=1"}),
                         {"password": "****", "a": [{"api_key": "****"}], "s": "pwd=****"})

    def test_notice_mute(self):
        n = notices.add(self.root, "gate_disabled", "must", "게이트 꺼짐")
        self.assertEqual(len(notices.pending(self.root)), 1)
        notices.mute(self.root, n["id"])
        self.assertIsNone(notices.add(self.root, "gate_disabled", "must", "또"))


class BudgetRetentionTest(HarnessTestCase):
    """01 P8 가벼움 예산(편의 장치가 비싼 것부터 빠짐) · 09 로컬 원본 보관 기간."""

    def _tasks(self, n, sec, gate_sec, size="small"):
        for i in range(n):
            ledger.append_task(self.root, {"task_id": "T-%d" % i, "size": size, "ended_at": "2026-10-2%dT10:00:00+09:00" % (i % 9),
                                           "harness_overhead": {"sec": sec, "tokens": 0, "gate_sec": gate_sec}})

    def test_over_budget_disables_most_expensive_first(self):
        from vmc_harness.core import budget, events
        win = config.get("budget.window_tasks", self.root)
        over = config.get("budget.small_task_overhead_s", self.root) + 5
        self._tasks(win, over, 1.0)
        self.assertTrue(budget.enabled(self.root, "pm"))
        self.assertEqual(budget.check(self.root), "pm")
        self.assertFalse(budget.enabled(self.root, "pm"))
        self.assertTrue(budget.enabled(self.root, "overdesign_llm"))
        self.assertIsNone(budget.check(self.root))          # 하루 1회
        self.assertEqual(budget.check(self.root, force=True), "overdesign_llm")
        ev = [e for e in events.read(self.root, "convenience_disabled")]
        self.assertEqual([e["device"] for e in ev], ["pm", "overdesign_llm"])

    def test_within_budget_or_too_few_samples_keeps_all(self):
        from vmc_harness.core import budget
        win = config.get("budget.window_tasks", self.root)
        self._tasks(win - 1, 999, 999)                       # 표본 부족
        self.assertIsNone(budget.check(self.root))
        self.assertTrue(all(budget.enabled(self.root, d) for d in budget.DEVICES))

    def test_gate_time_over_budget(self):
        from vmc_harness.core import budget
        win = config.get("budget.window_tasks", self.root)
        self._tasks(win, 0.1, config.get("budget.gate_time_per_task_s", self.root) + 1, size="mid")
        self.assertEqual(budget.check(self.root), "pm")

    def test_render_rewrite_off_means_no_llm(self):
        from unittest import mock
        from vmc_harness.core import budget
        from vmc_harness.core.jsonio import write_json
        from vmc_harness.core.project import local_dir
        from vmc_harness import KERNEL_VERSION
        write_json(local_dir(self.root) / "budget.json",
                   {"kernel_version": KERNEL_VERSION, "disabled": {"render_rewrite": {"at": "x"}}})
        from vmc_harness.render import engine
        with mock.patch.object(engine, "_rewrite", side_effect=AssertionError("LLM 재작성 호출됨")), \
                mock.patch.object(engine, "_write", side_effect=AssertionError("LLM 작성 호출됨")):
            engine.render(self.root, None, {"n": 3}, draft="알 수 없는 낱말 zzqx 3", kind="notice")

    def test_retention_prunes_old_local_raw(self):
        import json
        import os
        import time
        from vmc_harness.core import paths, retention
        from vmc_harness.core.project import local_dir
        ld = local_dir(self.root)
        days = config.get("retention.local_days", self.root)
        (ld / "events.jsonl").write_text(
            json.dumps({"at": "2020-01-01T00:00:00+09:00", "kind": "old"}) + "\n" +
            json.dumps({"at": "2099-01-01T00:00:00+09:00", "kind": "new"}) + "\n", encoding="utf-8")
        old_ts = time.time() - (days + 1) * 86400
        q = ld / "quotes" / "C-1.txt"
        q.parent.mkdir(parents=True)
        q.write_text("x", encoding="utf-8")
        os.utime(str(q), (old_ts, old_ts))
        ed = paths.cache_dir(self.root) / "evidence" / "T-old"
        ed.mkdir(parents=True)
        f = ed / "m.json"
        f.write_text("{}", encoding="utf-8")
        os.utime(str(f), (old_ts, old_ts))
        os.utime(str(ed), (old_ts, old_ts))
        keep = paths.cache_dir(self.root) / "evidence" / "T-new"
        keep.mkdir(parents=True)
        r = retention.prune(self.root)
        self.assertEqual(r, {"events": 1, "quotes": 1, "evidence": 1})
        self.assertIn("new", (ld / "events.jsonl").read_text(encoding="utf-8"))
        self.assertTrue(keep.exists())
        self.assertIsNone(retention.prune(self.root))      # 하루 1회
