"""bootstrap: 진단 출력 검사·[추가]·[제외]·리포트·init·remove (합성 데이터, LLM 없음)."""

import io
import os
from contextlib import redirect_stdout
from unittest import mock

from tests.util import HarnessTestCase
from vmc_harness.bootstrap import diagnose, facts, init, remove, report
from vmc_harness.core import ledger
from vmc_harness.core.jsonio import read_json, write_json


GOOD = {"summary": "요약", "proposals": [
    {"title": "SQL 파일은 SQL/ 아래에만", "kind": "gate", "evidence": "세션 12건 밖에 씀", "blocks": "엉뚱한 곳 SQL",
     "scope": "*.sql 쓰기", "cost": "작업당 0.1초", "condition": {"type": "write_outside", "ext": [".sql"], "allowed": ["SQL/**"]}},
    {"title": "감사 모듈 보호", "kind": "protected", "evidence": "\"감사 저장 실패면 업무도 실패\"", "blocks": "감사 누락",
     "scope": "be/audit/**", "cost": "승인 1회", "protected_globs": ["be/audit/**"]},
    {"title": "OPENJSON 금지", "kind": "gate", "evidence": "2회", "blocks": "호환성 오류", "scope": "*.sql",
     "cost": "0.1초", "condition": {"type": "diff_regex", "regex": "OPENJSON", "globs": ["**/*.sql"]}},
]}


class DiagnoseTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.write("be/audit/x.py", "def a():\n    pass\n")
        self.write("SQL/a.sql", "select 1\n")
        self.commit("base")
        facts.collect(self.root, run_tests=False, use_llm=False)

    def test_validate(self):
        ok, err = diagnose.validate(GOOD)
        self.assertEqual(len(ok), 3)
        self.assertEqual(err, [])
        bad = {"proposals": [{"title": "x", "kind": "gate", "evidence": "느낌상", "blocks": "b", "scope": "s", "cost": "c",
                              "condition": {"type": "path_write", "globs": ["a"]}},
                             {"title": "y", "kind": "gate", "evidence": "3건", "blocks": "b", "scope": "s", "cost": "c"},
                             {"title": "z", "kind": "weird", "evidence": "3", "blocks": "b", "scope": "s", "cost": "c"}]}
        ok, err = diagnose.validate(bad)
        self.assertEqual(ok, [])
        self.assertEqual(len(err), 3)
        self.assertEqual(diagnose.extract_json("앞말\n```json\n{\"a\":1}\n```"), {"a": 1})

    def test_prompt_has_no_procedure(self):
        inputs = diagnose.prepare_inputs(self.root, [], None)
        p = diagnose.build_prompt(self.root, inputs, ["이미 보인 것"])
        for head in ("목표:", "참고할 수 있는 것:", "제약:", "출력 형식:"):
            self.assertIn(head, p)
        self.assertNotRegex(p, r"(1단계|2단계|먼저 .* 다음)")
        self.assertIn("이미 보인 것", p)

    def _run_with(self, text):
        fake = {"text": text, "tokens": 100, "sec": 1.0}
        with mock.patch("vmc_harness.core.llm.available", return_value=True), \
                mock.patch("vmc_harness.core.llm.nested", return_value=False), \
                mock.patch("vmc_harness.core.llm.claude_agent_capped", return_value=fake) as m, \
                mock.patch.object(diagnose, "render_proposal", return_value={}):
            d = diagnose.run(self.root, sessions=[])
        return d, m

    def test_run_retry_accept_exclude(self):
        import json
        d, m = self._run_with("이건 JSON이 아님")
        self.assertEqual(m.call_count, 2)  # 형식이 안 맞으면 1회 다시
        self.assertEqual(d["status"], "no_proposals")
        d, m = self._run_with(json.dumps(GOOD, ensure_ascii=False))
        self.assertEqual(m.call_count, 1)
        self.assertEqual([p["no"] for p in d["proposals"]], [1, 2, 3])
        self.assertIn("backtest", d["proposals"][0])
        self.assertFalse(d["checks"]["tree_changed"])
        # 다시 돌려도 이미 보인 제안은 다시 내지 않는다
        d2, _ = self._run_with(json.dumps(GOOD, ensure_ascii=False))
        self.assertEqual(d2["proposals"], [])
        write_json(diagnose.diagnosis_path(self.root), d)

        res = {r["no"]: r for r in diagnose.accept(self.root, [1, 2])}
        self.assertFalse(res[2].get("error"), res[2])
        prot = read_json(self.root / ".harness" / "protected.json")
        e = prot["paths"][0]
        self.assertEqual(e["glob"], "be/audit/**")
        self.assertEqual(e["basis"]["type"], "explicit_request")
        self.assertEqual(e["basis"]["quote"], "감사 모듈 보호")  # 원문 = 제안 문구 그대로
        self.assertEqual(read_json(self.root / ".harness/config.json")["db"]["patch_dir"], "SQL")
        acts = ledger.list_actions(self.root)
        self.assertTrue(all(a["status"] in ("trial", "prepared") for a in acts))
        self.assertTrue(all(a["human_requested"] for a in acts))
        if not res[1].get("error"):
            self.assertTrue(res[1].get("gate_id"))
        again = diagnose.accept(self.root, [2])
        self.assertEqual(again[0].get("skipped"), "이미 추가함")
        ex = diagnose.exclude(self.root, [3])
        self.assertTrue(ex[0]["finding_id"].startswith("F-"))
        self.assertEqual(ledger.list_findings(self.root)[0]["button"], "exclude")

    def test_run_registers_structure_as_loop_ticket(self):
        import json
        from vmc_harness.render import growth
        data = dict(GOOD, proposals=GOOD["proposals"] + [
            {"title": "감사 밖 계산 모듈 테스트 보강", "kind": "structure", "evidence": "실패 3건", "blocks": "회귀",
             "scope": "calc", "cost": "1h", "targets": ["calc.py"]}])
        d, _ = self._run_with(json.dumps(data, ensure_ascii=False))
        st = [p for p in d["proposals"] if p["kind"] == "structure"][0]
        self.assertTrue(st["ticket_id"])
        self.assertEqual(ledger.get_candidate(self.root, st["ticket_id"])["kind"], "ticket")
        self.assertTrue(all(not p.get("ticket_id") for p in d["proposals"] if p["kind"] != "structure"))
        self.assertTrue(diagnose.load(self.root)["proposals"][-1]["ticket_id"])  # 저장된 결과에도
        import datetime as _dt
        tz = _dt.timezone.utc
        acc = growth.acceptance(self.root, _dt.datetime(2000, 1, 1, tzinfo=tz), _dt.datetime(2100, 1, 1, tzinfo=tz))
        self.assertEqual(acc["shown"], 3)  # 루프 티켓은 수용률 분모에서 뺀다

    def test_gate_script_valid(self):
        for c in GOOD["proposals"]:
            if c["kind"] == "gate":
                s = diagnose.gate_script(c["condition"], c["title"])
                compile(s, "g", "exec")
                try:
                    from vmc_harness.gates import generated
                    generated.validate_script(s)
                except ImportError:
                    pass

    def test_report_html(self):
        import json
        d, _ = self._run_with(json.dumps(GOOD, ensure_ascii=False))
        with mock.patch.object(report, "to_pdf", return_value=(None, "no browser")):
            r = report.build(self.root, open_browser=False)
        with open(r["html"], encoding="utf-8") as fh:
            html = fh.read()
        for s in ("위험 지도", "이 장치가 있었다면", "기준선", "제안 상세", "vmc-harness diagnose accept 1", "<svg"):
            self.assertIn(s, html)
        self.assertTrue(r["html"].replace("\\", "/").endswith(".html"))
        self.assertIn("/.harness/cache/reports/diagnosis-", r["html"].replace("\\", "/"))

    def test_report_structured_summary(self):
        import json
        g = dict(GOOD, summary={"headline": "검증 고리가 문제다", "observations": ["완료 보고 뒤 재확인 (01a06b39 H12)"],
                                "recommendation": "DB 직접 접속부터 막는다"})
        self._run_with(json.dumps(g, ensure_ascii=False))
        h = report.build_html(self.root)
        for s in ("핵심", "검증 고리가 문제다", "<li>완료 보고 뒤 재확인", "<span class='ref'>01a06b39 H12</span>", "제안드리는 순서"):
            self.assertIn(s, h)


class InitRemoveTest(HarnessTestCase):
    def test_init_dry_then_remove(self):
        (self.root / ".harness" / "config.json").unlink()
        self.write("app/main.py", "def main():\n    pass\n")
        self.commit("base")
        settings = self._tmp / "claude" / "settings.json"
        write_json(settings, {"cleanupPeriodDays": 30})
        os.environ["CLAUDE_CONFIG_DIR"] = str(settings.parent)
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(report, "to_pdf", return_value=(None, "x")):
            s = init.run(self.root, dry_system=True, run_tests=False, use_llm=False, do_diagnose=False,
                         learning=False, open_report=False, out=print)
        self.assertEqual(read_json(settings)["cleanupPeriodDays"], 30)  # --dry-system: 사용자 설정 그대로
        cfg = read_json(self.root / ".harness" / "config.json")
        self.assertEqual(cfg["profile"], "standard")
        self.assertIn("cache/", (self.root / ".harness" / ".gitignore").read_text())
        self.assertEqual((self.root / "CLAUDE.md").read_text().strip(), "@AGENTS.md")
        self.assertIn("vmc-harness:begin", (self.root / "AGENTS.md").read_text())
        self.assertTrue((self.root / ".harness/cache/facts.json").is_file())
        pid = cfg["project_id"]
        # 두 번째 init = 갱신(project_id 그대로)
        with redirect_stdout(io.StringIO()), mock.patch.object(report, "to_pdf", return_value=(None, "x")):
            init.run(self.root, dry_system=True, run_tests=False, use_llm=False, do_diagnose=False,
                     learning=False, open_report=False, out=print)
        self.assertEqual(read_json(self.root / ".harness" / "config.json")["project_id"], pid)
        # cleanupPeriodDays: 실제 모드 — 더 작으면 늘리고, 더 크면 그대로
        changed, note = init.ensure_cleanup_period(self.root)
        self.assertTrue(changed)
        self.assertEqual(read_json(settings)["cleanupPeriodDays"], 365)
        write_json(settings, {"cleanupPeriodDays": 999})
        self.assertEqual(init.ensure_cleanup_period(self.root), (False, None))
        with redirect_stdout(io.StringIO()):
            r = remove.run(self.root)
        self.assertFalse((self.root / "CLAUDE.md").exists())
        self.assertFalse((self.root / "AGENTS.md").exists() and "vmc-harness" in (self.root / "AGENTS.md").read_text())
        self.assertTrue((self.root / ".harness").exists())  # 기본 남김
        from vmc_harness.core import paths
        self.assertFalse(paths.local_project_dir(pid).exists())
        with redirect_stdout(io.StringIO()):
            remove.run(self.root, purge_harness=True)
        self.assertFalse((self.root / ".harness").exists())
