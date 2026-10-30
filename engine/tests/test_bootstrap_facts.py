"""bootstrap: 기계 사실·규칙 블록·세션 입력·기준선·백테스트 (합성 데이터)."""

import json
import sys

from tests.util import HarnessTestCase
from vmc_harness.bootstrap import backtest, baseline, facts, sessions_input


class FactsTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.write("be/pyproject.toml", "[project]\ndependencies=['pyodbc']\n[tool.pytest.ini_options]\ntestpaths=['tests']\n")
        self.write("be/app/__init__.py", "")
        self.write("be/app/api/__init__.py", "")
        self.write("be/app/api/x.py", "def f():\n    return 1\n")
        self.write("be/tests/test_ok.py", "def test_ok():\n    assert True\n")
        self.write("be/tests/test_bad.py", "def test_bad():\n    assert False\n")
        self.write("be/tests/test_broken.py", "from app.nope import X\n")
        self.write("fe/package.json", '{"scripts":{"build":"next build","lint":"eslint .","typecheck":"tsc --noEmit"}}')
        self.write("fe/src/app/page.tsx", "export default function P(){}\n")
        self.write("SQL/Table/a.sql", "create table a(x int)")
        self.write("SQL/deploy.py", "ROOT = 'x'  # SQL/Patch 번호순\n")
        self.write("run-dev.cmd", "@echo off\n")
        self.write(".gitattributes", "*.sh text eol=lf\n")

    def test_source_roots_and_commands(self):
        sr, why = facts.detect_source_roots(self.root)
        self.assertEqual(sr, ["SQL", "be", "fe"])
        self.assertTrue(why)
        cmds = facts.detect_commands(self.root, sr)
        names = {(c["dir"], c["name"]) for c in cmds}
        self.assertIn(("be", "pytest"), names)
        self.assertIn(("fe", "lint"), names)
        self.assertTrue(all(c["command"] for c in cmds))

    def test_collect_load_shape_and_baseline(self):
        try:
            import pytest  # noqa: F401
        except ImportError:
            self.skipTest("pytest 없음")
        from vmc_harness.core.jsonio import write_json
        write_json(self.root / ".harness" / "config.json", {"project_id": self.project_id, "profile": "standard"})
        facts.collect(self.root, run_tests=True, use_llm=False)
        f = facts.load(self.root)
        # 다른 영역이 읽는 모양(gates·db·loop)
        self.assertEqual(f["commands"]["test"]["command"], "python -m pytest be")
        self.assertEqual(f["commands"]["build"]["command"], "npm --prefix fe run build")
        self.assertEqual(f["db_kind"], "mssql")
        self.assertTrue(f["db"])
        tb = f["test_baseline"]
        self.assertIn("tests/test_bad.py::test_bad", tb["failures"])
        self.assertIn("be/tests/test_bad.py::test_bad", tb["failures"])
        self.assertIn("tests/test_broken.py", tb["failures"])  # 수집 오류도 기준선
        run = tb["runs"][0]
        self.assertEqual(run["collection_errors"][0]["path"], "tests/test_broken.py")
        self.assertIn("No module named", run["collection_errors"][0]["reason"])
        self.assertEqual(f["sql_patch_dir"]["suggested"], "SQL")
        self.assertIn("be/app/api/**", f["behavior_globs"]["api"])
        self.assertTrue(all(f["_commands"][k] for k in ("commands", "db", "test_baseline")))
        # 규칙 블록용 환경 사실 → rules.json(gates 형식)
        rules = facts.write_machine_fact_rules(self.root, f)
        ids = {r["id"] for r in rules}
        self.assertIn("mf-db", ids)
        self.assertIn("mf-test-be", ids)
        self.assertIn("mf-os", ids)
        for r in rules:
            self.assertEqual(r["basis"]["type"], "machine_fact")
            self.assertEqual(r["basis"]["command"][:3], ["vmc-harness", "init", "--print-fact"])
        self.assertEqual(facts.print_fact(self.root, "mf-db"), "MSSQL")
        # 사람 규칙은 건드리지 않는다
        data = facts.read_rules(self.root)
        data["rules"].append({"id": "r-1", "basis": {"type": "explicit_request", "quote": "앞으로 X", "source": {"date": "2026-10-22"}}})
        from vmc_harness.core.jsonio import write_json
        write_json(facts.rules_path(self.root), data)
        facts.write_machine_fact_rules(self.root, f)
        self.assertTrue(any(r["id"] == "r-1" for r in facts.read_rules(self.root)["rules"]))


def _export_session(sid, agent, prompts, tools, start="2026-09-01T00:00:00Z", sub=False):
    msgs = [{"role": "user", "content": p, "timestamp": start} for p in prompts]
    msgs.append({"role": "user", "content": "# AGENTS.md instructions for C:\\proj\\X", "timestamp": start})
    for name, inp in tools:
        msgs.append({"role": "assistant", "content": "", "toolName": name, "toolInput": json.dumps(inp), "timestamp": start})
    return {"id": sid, "agent": agent.upper(), "isSubagent": sub, "startedAt": start, "endedAt": start,
            "inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 0, "cacheCreationTokens": 0,
            "title": prompts[0] if prompts else "", "messages": msgs, "_source": {"cwd": "C:\\proj\\X"}}


class SessionsBacktestTest(HarnessTestCase):
    def _dir(self):
        d = self._tmp / "export"
        (d / "sessions" / "claude").mkdir(parents=True)
        (d / "sessions" / "codex").mkdir(parents=True)
        s1 = _export_session("s1", "claude", ["DB 직접 붙지 말고 SQL 파일로만", "안 돼 에러 나"], [
            ("Bash", {"command": "sqlcmd -S srv -Q \"select 1\""}),
            ("Write", {"file_path": "C:\\proj\\X\\docs\\p.sql", "content": "x"}),
            ("Edit", {"file_path": "C:\\proj\\X\\be\\tests\\test_a.py"}),
            ("AskUserQuestion", {"questions": [{"q": 1}, {"q": 2}]})])
        s2 = _export_session("s2", "codex", ["DB 직접 붙지 말고 SQL 파일로만 해"], [
            ("exec", {"code": 'await tools.shell_command({"command":"python -c \\"import pyodbc; pyodbc.connect(\'DRIVER={ODBC Driver 18}\')\\""})'}),
            ("apply_patch", "*** Begin Patch\n*** Update File: SQL/Patch/001.sql\n+x\n*** End Patch")], start="2026-09-02T00:00:00Z")
        s3 = _export_session("s3", "claude", ["sub"], [("Bash", {"command": "ls"})], sub=True)
        for s, ag in ((s1, "claude"), (s2, "codex"), (s3, "claude")):
            (d / "sessions" / ag / ("%s.json" % s["id"])).write_text(json.dumps(s), encoding="utf-8")
        return d / "sessions"

    def test_export_parse_baseline_backtest(self):
        ss, info = sessions_input.load(self.root, self._dir(), None, limit=50)
        self.assertEqual(info["main"], 2)
        by = {s["session_id"]: s for s in ss}
        self.assertEqual(len(by["s1"]["human_prompts"]), 2)  # 주입 문구 제외
        self.assertEqual(by["s1"]["human_turns"], 4)  # 프롬프트 2 + AI 질문 2
        self.assertEqual(by["s3"]["kind"], "sub")
        w = [t for t in by["s1"]["tool_calls"] if t["write"]]
        self.assertEqual(w[0]["paths"], ["docs/p.sql"])
        self.assertIn("SQL/Patch/001.sql", by["s2"]["tool_calls"][1]["paths"])
        self.assertTrue(any("pyodbc.connect" in c for c in by["s2"]["tool_calls"][0]["commands"]))

        b = baseline.compute(ss)
        self.assertEqual(b["sessions"]["main_with_prompts"], 2)
        self.assertEqual(b["human_prompts"], 3)
        self.assertEqual(b["recheck_after_done_ratio"]["method"], "keyword")
        self.assertEqual(b["recheck_after_done_ratio"]["numerator"], 1)
        self.assertEqual(b["repeated_instructions"], 1)
        from vmc_harness.core import ledger
        baseline.build(self.root, ss, info)
        rec = ledger.read_baseline(self.root)
        self.assertIn("definitions", rec)
        self.assertIsNone(rec["after"])
        self.assertNotIn("SQL 파일로만", json.dumps(rec, ensure_ascii=False))  # 원문 없음(P9)

        r = backtest.run(self.root, {"type": "db_direct"}, ss)
        self.assertEqual(r["would_block"], 2)
        self.assertIn("최소 2건", r["note"])
        r = backtest.run(self.root, {"type": "write_outside", "ext": [".sql"], "allowed": ["SQL/**"]}, ss)
        self.assertEqual(r["would_block"], 1)
        self.assertEqual(r["scenes"][0]["match"], "docs/p.sql")
        r = backtest.run(self.root, {"type": "command", "regex": "sqlcmd"}, ss)
        self.assertEqual(r["sessions"], 1)
        with self.assertRaises(ValueError):
            backtest.run(self.root, {"type": "nope"}, ss)

    def test_backtest_commits(self):
        self.write("SQL/a.sql", "select 1\n")
        self.write("docs/b.sql", "select OPENJSON(x)\n")
        self.commit("add")
        self.write("docs/b.sql", "select 2\n")
        self.commit("fix: 오류 수정")
        r = backtest.run(self.root, {"type": "write_outside", "ext": [".sql"], "allowed": ["SQL/**"]}, [])
        self.assertEqual(r["commits"], 2)
        self.assertGreaterEqual(r["problem_changes"], 1)
        r = backtest.run(self.root, {"type": "diff_regex", "regex": "OPENJSON", "globs": ["**/*.sql"]}, [])
        self.assertEqual(r["would_block"], 1)
