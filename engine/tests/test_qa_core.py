"""QA: 시나리오 출처 메타 게이트 · 결과 원본(상태·예산·필수 칸) · 회귀 · 이어서 · 보고서 · 데이터 범위."""

import datetime as _dt
import io
import json
from contextlib import redirect_stdout, redirect_stderr

from tests.util import HarnessTestCase
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.ledger import MetaGateError


def scenario(sid="login", **over):
    sc = {"id": sid, "title": "로그인", "pages": ["/login"], "paths": ["fe/app/login/**"],
          "expectations": [
              {"text": "틀린 비밀번호면 오류 문구", "source": {"type": "human_quote", "quote": "비번 틀리면 빨간 글씨"}},
              {"text": "성공하면 대시보드", "source": {"type": "current_behavior", "ref": "fe/app/login/page.tsx::submit"}}],
          "data": {"writes": []}}
    sc.update(over)
    return sc


class ScenarioGateTest(HarnessTestCase):
    def test_source_required(self):
        from vmc_harness.qa import scenarios
        bad = scenario(expectations=[{"text": "보인다"}])
        with self.assertRaises(MetaGateError) as cm:
            scenarios.save(self.root, bad)
        self.assertIn("출처 없음", str(cm.exception))
        bad2 = scenario(expectations=[{"text": "보인다", "source": {"type": "human_quote", "quote": " "}}])
        self.assertFalse(scenarios.is_valid(bad2)[0])
        bad3 = scenario(expectations=[{"text": "보인다", "source": {"type": "current_behavior"}}])
        self.assertFalse(scenarios.is_valid(bad3)[0])
        self.assertFalse((self.root / ".harness/qa/scenarios/login.json").exists())

    def test_save_and_no_overwrite(self):
        from vmc_harness.qa import scenarios
        scenarios.save(self.root, scenario())
        self.assertTrue((self.root / ".harness/qa/scenarios/login.json").exists())
        with self.assertRaises(MetaGateError):
            scenarios.save(self.root, scenario())

    def test_check_write_and_covering(self):
        from vmc_harness.qa import scenarios
        ok, _ = scenarios.check_write(self.root, ".harness/qa/scenarios/login.json", json.dumps(scenario()))
        self.assertTrue(ok)
        ok, reason = scenarios.check_write(self.root, ".harness/qa/scenarios/other.json", json.dumps(scenario()))
        self.assertFalse(ok)
        self.assertTrue(scenarios.check_write(self.root, "src/a.py", "x")[0])
        scenarios.save(self.root, scenario())
        self.assertEqual(scenarios.covering(self.root, ["fe/app/login/page.tsx"]), ["login"])
        self.assertEqual(scenarios.covering(self.root, ["be/x.py"]), [])

    def test_invalid_write_entries(self):
        from vmc_harness.qa import scenarios
        self.assertFalse(scenarios.is_valid(scenario(data={"writes": [{"entity": "x", "op": "drop"}]}))[0])


class ResultsTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        from vmc_harness.qa import scenarios
        scenarios.save(self.root, scenario())
        self.ev = self.write(".harness/cache/evidence/T-1/shot.png", "png")

    def test_required_fields_and_flow(self):
        from vmc_harness.qa import results
        with self.assertRaises(MetaGateError):  # qa run 없이 갱신 불가
            results.set_status(self.root, "login", "PASS", evidence=[str(self.ev)])
        results.begin(self.root, "login")
        with self.assertRaises(MetaGateError) as cm:
            results.set_status(self.root, "login", "FAIL", cause="버튼 비활성")
        self.assertIn("사람 필요", str(cm.exception))
        with self.assertRaises(MetaGateError):
            results.set_status(self.root, "login", "FAIL", cause="x", needs_human=False)
        with self.assertRaises(MetaGateError):
            results.set_status(self.root, "login", "PASS")  # 증거 없음
        with self.assertRaises(MetaGateError):
            results.set_status(self.root, "login", "PASS", evidence=["nope.png"])
        r = results.set_status(self.root, "login", "FAIL", cause="버튼 비활성", needs_human=False, needs_db_change=False)
        self.assertEqual(r["status"], "FAIL")
        results.begin(self.root, "login")
        r = results.set_status(self.root, "login", "PASS", evidence=[str(self.ev)])
        self.assertEqual(r["status"], "PASS")
        self.assertTrue(r["ever_passed"])
        self.assertEqual(r["evidence"], [".harness/cache/evidence/T-1/shot.png"])
        self.assertEqual(len(r["attempts"]), 2)
        data = read_json(self.root / ".harness/qa/results.json")
        self.assertEqual(data["scenarios"]["login"]["status"], "PASS")

    def test_budget_hold_by_code(self):
        from vmc_harness.qa import results
        results.begin(self.root, "login")
        p = self.root / ".harness/qa/results.json"
        data = read_json(p)
        past = (_dt.datetime.now().astimezone() - _dt.timedelta(hours=10)).isoformat(timespec="seconds")
        data["scenarios"]["login"]["clock"]["since"] = past
        write_json(p, data)
        r = results.set_status(self.root, "login", "FAIL", cause="느림", needs_human=False, needs_db_change=False)
        self.assertEqual(r["status"], "HOLD")
        self.assertTrue(r["budget"]["exceeded"])
        self.assertEqual([h["id"] for h in results.human_list(self.root)], ["login"])
        self.assertIn("예산 초과", results.human_list(self.root)[0]["reasons"])
        # 예산 초과 HOLD는 다시 시작 안 됨, 사람이 reset하면 됨
        from vmc_harness.qa import runner
        with self.assertRaises(runner.RunRefused):
            runner.start(self.root, "login")
        results.set_status(self.root, "login", "FAIL", cause="다시", needs_human=False, needs_db_change=False,
                           by="human", reset_budget=True)
        rec = results.begin(self.root, "login")
        self.assertFalse(rec["budget"]["exceeded"])

    def test_regressions_human_resume(self):
        from vmc_harness.qa import results, scenarios
        scenarios.save(self.root, scenario("list", paths=["fe/app/list/**"], pages=["/list"]))
        scenarios.save(self.root, scenario("approve", paths=["fe/app/approve/**"], pages=["/approve"]))
        results.begin(self.root, "login")
        results.set_status(self.root, "login", "PASS", evidence=[str(self.ev)])
        results.begin(self.root, "login")
        results.set_status(self.root, "login", "FAIL", cause="회귀", needs_human=False, needs_db_change=False)
        self.assertEqual(results.regressions(self.root), ["login"])
        self.assertEqual(results.regressions(self.root, ["list"]), [])
        results.begin(self.root, "approve")
        results.set_status(self.root, "approve", "HOLD", cause="결재선 정책 결정 필요", needs_human=True,
                           needs_db_change=True)
        r = results.resume(self.root)
        self.assertEqual(r["next"]["id"], "login")  # FAIL 먼저, 그다음 미실행
        self.assertEqual([x["id"] for x in r["remaining"]], ["login", "list"])
        self.assertEqual([h["id"] for h in r["human"]], ["approve"])
        self.assertEqual(set(r["human"][0]["reasons"]), {"사람 결정", "DB 변경"})


class CliTest(HarnessTestCase):
    def run_cli(self, *argv):
        import os
        from vmc_harness.cli import main
        out, err = io.StringIO(), io.StringIO()
        cwd = os.getcwd()
        os.chdir(str(self.root))
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = main(list(argv))
        finally:
            os.chdir(cwd)
        return rc, out.getvalue(), err.getvalue()

    def test_run_status_resume_report(self):
        from vmc_harness.qa import scenarios
        scenarios.save(self.root, scenario(data={"writes": [{"entity": "work_requests", "op": "create"}]}))
        rc, out, err = self.run_cli("qa", "run")
        self.assertEqual(rc, 1)
        self.assertIn("허용 범위 밖 쓰기", err)
        write_json(self.root / ".harness/qa/data-policy.json",
                   {"db_access": "browser_only", "allowed": [{"entity": "work_requests", "ops": ["create"]}]})
        rc, out, err = self.run_cli("qa", "run")
        self.assertEqual(rc, 0, err)
        self.assertIn("목표", out)
        self.assertIn("사람에게 묻지 않는다", out)
        self.assertIn("qa status set login", out)
        self.assertNotIn("900", out)  # 예산 숫자는 보이지 않는다(01 P5)
        self.assertTrue((self.root / ".harness/cache/qa/active.json").exists())
        rc, out, err = self.run_cli("qa", "status", "set", "login", "FAIL", "--cause", "버튼 안 눌림",
                                    "--needs-human", "no", "--needs-db-change", "no")
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.root / ".harness/cache/qa/active.json").exists())
        rc, out, _ = self.run_cli("qa", "resume")
        self.assertIn("다음: vmc-harness qa run login", out)
        rc, out, _ = self.run_cli("qa", "status", "--human")
        self.assertIn("사람이 볼 것 없음", out)
        rc, out, _ = self.run_cli("qa", "report")
        from pathlib import Path
        html = Path(out.strip()).read_text(encoding="utf-8")
        self.assertIn("보기 전용", html)
        self.assertNotIn("<form", html)
        self.assertNotIn("<input", html)

    def test_status_set_missing_required(self):
        from vmc_harness.qa import scenarios
        scenarios.save(self.root, scenario())
        self.run_cli("qa", "run", "login")
        rc, _, err = self.run_cli("qa", "status", "set", "login", "FAIL", "--cause", "x")
        self.assertEqual(rc, 1)
        self.assertIn("필수 칸 없음", err)


class PolicyTest(HarnessTestCase):
    def test_data_and_commands(self):
        from vmc_harness.qa import policy
        pol = {"db_access": "browser_only",
               "allowed": [{"entity": "work_requests", "ops": ["create", "update"], "match": "QA_*"}],
               "forbidden": [{"entity": "audit_*", "ops": ["create", "update", "delete"]}],
               "forbidden_urls": ["*/admin/users/*/delete*"], "allowed_hosts": ["localhost"],
               "write_methods": ["POST", "PUT", "PATCH", "DELETE"]}
        write_json(self.root / ".harness/qa/data-policy.json", pol)
        cw = lambda t: policy.check_write(self.root, t)[0]
        self.assertTrue(cw({"kind": "data", "entity": "work_requests", "op": "create", "record": "QA_1"}))
        self.assertFalse(cw({"kind": "data", "entity": "work_requests", "op": "create", "record": "real"}))
        self.assertFalse(cw({"kind": "data", "entity": "work_requests", "op": "delete"}))
        self.assertFalse(cw({"kind": "data", "entity": "audit_log", "op": "create"}))
        self.assertFalse(cw({"kind": "command", "command": "sqlcmd -S dev -Q \"select 1\""}))
        self.assertFalse(cw({"kind": "command", "command": "python -c 'import pyodbc'"}))
        self.assertFalse(cw({"kind": "command", "command": "curl -X DELETE https://prod.example.com/api/x"}))
        self.assertTrue(cw({"kind": "command", "command": "curl -X POST http://localhost:3000/api/x"}))
        self.assertTrue(cw({"kind": "command", "command": "curl https://prod.example.com/health"}))
        self.assertFalse(cw({"kind": "url", "url": "http://localhost:3000/admin/users/3/delete"}))

    def test_widening(self):
        from vmc_harness.qa import policy
        old = {"db_access": "browser_only", "allowed": [{"entity": "a", "ops": ["create"]}],
               "forbidden": [{"entity": "audit", "ops": ["delete"]}]}
        self.assertTrue(policy.is_widening(old, dict(old, allowed=[{"entity": "a", "ops": ["create", "delete"]}]))[0])
        self.assertTrue(policy.is_widening(old, dict(old, forbidden=[]))[0])
        self.assertTrue(policy.is_widening(old, dict(old, db_access="direct"))[0])
        self.assertFalse(policy.is_widening(old, dict(old, allowed=[]))[0])  # 좁히기는 자유
