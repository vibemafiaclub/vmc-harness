"""브리핑: 09 스키마·상태 줄·사람마다 거름·한 번 보인 건 다시 안 보임·하루 1회 2줄·성장 HTML (spec 08)."""

import io
import json
import os
from contextlib import redirect_stdout
from unittest import mock

from tests.util import HarnessTestCase

from vmc_harness.core import ledger, notices
from vmc_harness.core.clock import now_iso
from vmc_harness.render import brief, growth, handlers
from vmc_harness.hooks.contract import HookContext


class BriefTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        p = mock.patch("vmc_harness.core.llm.claude_p", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def test_schema_and_status_line(self):
        notices.add(self.root, "gate_blocked", "must", "운영 DB 쓰기 시도 차단")
        notices.add(self.root, "structure_change", "flow", "work_requests 조회 함수 분리", area="be/work_requests")
        b = brief.build(self.root)
        for k in ("generated_at", "me", "team", "status_line"):
            self.assertIn(k, b)
        for k in ("current_task", "approvals", "notices", "learned"):
            self.assertIn(k, b["me"])
        for k in ("week", "numbers", "spread", "harness_did", "report_line"):
            self.assertIn(k, b["team"])
        self.assertEqual(b["status_line"], "🛡 0 · 증거 - · 확인 0 · 알림 2")
        self.assertTrue((self.root / ".harness" / "cache" / "brief.json").is_file())
        self.assertEqual(b["me"]["notices"][0]["buttons"], ["자세히", "알림 끄기"])

    def test_status_line_shows_auto_update_off(self):
        from vmc_harness.dist import updater
        self.assertNotIn("자동 업데이트", brief.build(self.root)["status_line"])
        updater.set_auto(False)
        b = brief.build(self.root)
        self.assertIs(b["status"]["auto_update"], False)
        self.assertTrue(b["status_line"].endswith(" · 자동 업데이트 꺼짐"))
        self.assertIn("자동 업데이트 꺼짐", brief.text(b))

    def test_inline_notices_once_by_area(self):
        notices.add(self.root, "gate_disabled", "must", "'정산 페이지 폭' 게이트 꺼짐", area="fe/settle")
        notices.add(self.root, "gate_blocked", "must", "다른 영역", area="be/other")
        lines = brief.inline_notices(self.root, "fe/settle/page.tsx")
        self.assertEqual(len(lines), 1)
        self.assertIn("정산 페이지 폭", lines[0])
        self.assertEqual(brief.inline_notices(self.root, "fe/settle/page.tsx"), [])  # 한 번 보인 건 다시 안 보임
        # 패널에는 '본 알림'으로 잠시 남는다(다시 알리지 않음)
        b = brief.build(self.root)
        seen = [n for n in b["me"]["notices"] if n["seen"]]
        self.assertEqual(len(seen), 1)
        self.assertEqual(b["status"]["notices"], 1)

    def test_daily_two_lines_once(self):
        notices.add(self.root, "gate_blocked", "must", "운영 DB 쓰기 시도 차단")
        lines = brief.daily_two_lines(self.root, agent="codex")
        self.assertEqual(len(lines), 2)
        self.assertIn("알림 1", lines[0])
        self.assertEqual(brief.daily_two_lines(self.root, agent="codex"), [])

    def test_daily_two_lines_nothing_keeps_day(self):
        self.assertEqual(brief.daily_two_lines(self.root), [])
        notices.add(self.root, "gate_blocked", "must", "차단")
        self.assertEqual(len(brief.daily_two_lines(self.root)), 2)

    def test_learned_author_only_on_own_screen(self):
        ledger.create_action(self.root, {"type": "explicit_request", "quote": "앞으로 페이지 하나 먼저"}, "card", "h",
                             why_not_higher="취향", extra={"title": "페이지 하나 먼저 적용 후 확산", "author": "t"})
        ledger.create_action(self.root, {"type": "explicit_request", "quote": "패치 sql"}, "card", "h2",
                             why_not_higher="취향", extra={"title": "패치 sql 같은 배치 내 새 컬럼 참조 금지", "author": "다른사람"})
        b = brief.build(self.root)
        by = {x["text"]: x for x in b["me"]["learned"]}
        self.assertEqual(by["페이지 하나 먼저 적용 후 확산"].get("author"), "t")  # git user.name = t (본인)
        self.assertNotIn("author", by["패치 sql 같은 배치 내 새 컬럼 참조 금지"])
        self.assertEqual(b["team"]["spread"]["shared_judgments"], 2)
        self.assertEqual(b["team"]["harness_did"]["added"], 2)

    def test_approvals_only_from_my_tasks(self):
        ledger.create_approval(self.root, "T-not-mine", 1, "남의 작업", "approve", True, "git revert", ["ok"], 1)
        b = brief.build(self.root)
        self.assertEqual(b["me"]["approvals"], [])

    def test_text_view(self):
        notices.add(self.root, "gate_blocked", "must", "운영 DB 쓰기 시도 차단")
        b = brief.build(self.root)
        t = brief.text(b)
        self.assertIn("[내 작업]", t)
        self.assertIn("알림 끄기", t)
        self.assertIn("팀 대시보드", brief.text(b, tab="team"))
        self.assertIn("보고용 한 줄", brief.text(b, tab="team"))

    def test_weekly_html_and_no_browser(self):
        os.environ["VMC_HARNESS_BROWSER"] = str(self.root / "no-browser")
        out = growth.generate_weekly(self.root)
        self.assertTrue(os.path.isfile(out["growth_html"]))
        self.assertTrue(os.path.isfile(out["team_html"]))
        self.assertIsNone(out["team_pdf"])  # 브라우저 없으면 HTML만
        self.assertFalse(growth.ensure_weekly(self.root))  # 그 주 것이 있으면 다시 안 만든다
        with open(out["growth_html"], encoding="utf-8") as f:
            html = f.read()
        self.assertIn("새로 배운 판단", html)
        self.assertIn("보고용 한 줄", html)

    def test_report_line_words(self):
        t = growth.team(self.root)
        self.assertIn("표준 체계", t["report_line"])
        self.assertIn("1주차", t["report_line"])

    def test_handlers_no_inject(self):
        ctx = HookContext(event="SessionStart", agent="claude", raw={}, root=str(self.root))
        with mock.patch.object(handlers, "_spawn") as sp:
            self.assertIsNone(handlers.on_session_start(ctx))
        args = sp.call_args.args[1:]
        self.assertIn("--refresh", args)
        self.assertIn("--weekly", args)
        ctx = HookContext(event="UserPromptSubmit", agent="claude", raw={}, root=str(self.root),
                          prompt="'멱등성'이 뭔 말이야")
        self.assertIsNone(handlers.on_user_prompt(ctx))
        from vmc_harness.render import profile
        self.assertIn("멱등성", profile.load(self.root)["avoid"])

    def test_cli_json_and_buttons(self):
        from vmc_harness import cli
        old = os.getcwd()
        os.chdir(str(self.root))
        self.addCleanup(os.chdir, old)
        n = notices.add(self.root, "gate_blocked", "must", "운영 DB 쓰기 시도 차단")
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch("vmc_harness.core.proc.spawn_detached"):
            cli.main(["brief", "--json"])
        b = json.loads(buf.getvalue())
        self.assertTrue(b["installed"])
        self.assertEqual(b["status"]["notices"], 1)
        with redirect_stdout(io.StringIO()):
            cli.main(["notice", "mute", n["id"]])
        self.assertEqual(ledger.list_findings(self.root)[0]["button"], "mute")
        self.assertIsNone(notices.add(self.root, "gate_blocked", "must", "또"))
        with redirect_stdout(io.StringIO()):
            cli.main(["notice", "unmute", "gate_blocked"])
        self.assertIsNotNone(notices.add(self.root, "gate_blocked", "must", "다시"))
        p = ledger.create_approval(self.root, "T-1", 2, "보호 영역 변경", "approve", True, "git revert", ["ok"], 3)
        with redirect_stdout(io.StringIO()):
            cli.main(["approval", p["approval_id"], "approve"])
        self.assertEqual(ledger.list_approvals(self.root)[0]["answer"], "approve")


class ProposalTest(HarnessTestCase):
    def test_diagnose_proposals_in_brief(self):
        try:
            from vmc_harness.bootstrap import diagnose
        except ImportError:
            self.skipTest("진단 영역 없음")
        from vmc_harness.core.jsonio import write_json
        write_json(diagnose.diagnosis_path(self.root), {"proposals": [
            {"no": 1, "title": "SQL 폴더 밖 sql 쓰기 막기", "display": {"rendered": "SQL 폴더 밖 sql 쓰기 막기\n근거 3건"}},
            {"no": 2, "title": "이미 추가함", "accepted": "2026-10-22T10:00:00+09:00"}]})
        with mock.patch("vmc_harness.core.llm.claude_p", return_value=None):
            b = brief.build(self.root)
        self.assertEqual([(p["id"], p["text"]) for p in b["me"]["proposals"]], [("1", "SQL 폴더 밖 sql 쓰기 막기")])
        self.assertEqual(b["me"]["proposals"][0]["buttons"], ["추가", "제외"])
