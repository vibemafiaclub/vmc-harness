"""bootstrap: 측정·기준선 결함 재현 (D4·D5·D6·C5 — 합성 데이터).

- D4 Codex 자동 승인 심사(guardian) 요청이 섞인 세션 → 사람 지시에서 빠짐. 옛 기준선 정정 + 리포트 정정 문구
- D5 재진단이 기준선 latest·테스트 실패 기준선을 갱신, 리포트 머리말에 설치 전·이번 기간을 나란히
- D6 파일 목록이 .gitignore를 따른다(git 아니면 폴더 순회)
- C5 .sql이 있는 폴더 전체가 아니라 그 안의 .sql만 DB 동작
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from tests.util import HarnessTestCase
from vmc_harness.bootstrap import baseline, facts, report, sessions_input, util, weekly
from vmc_harness.core import ledger
from vmc_harness.core.jsonio import read_json, write_json

GUARD = "The following is the Codex agent history whose request action you are assessing. Approve or deny."


def _rollout(path, cwd, sid, day, prompts, guardian=0):
    rows = [{"timestamp": "%sT09:00:00Z" % day, "type": "session_meta", "payload": {"id": sid, "cwd": cwd}}]
    for i, text in enumerate(prompts):
        t = "%sT09:%02d:00Z" % (day, i + 1)
        rows += [{"timestamp": t, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "u%d" % i}},
                 {"timestamp": t, "type": "event_msg", "payload": {"type": "user_message", "message": text}},
                 {"timestamp": t, "type": "event_msg",
                  "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 10}}}},
                 {"timestamp": t, "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "u%d" % i}}]
    for j in range(guardian):  # 같은 세션에 섞인 심사 요청(옛 형식 user 메시지 항목으로)
        t = "%sT10:%02d:00Z" % (day, j)
        rows.append({"timestamp": t, "type": "response_item",
                     "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": GUARD}]}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


class GuardianTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        os.environ["VMC_CLAUDE_ROOT"] = str(self._tmp / "claude")
        os.environ["VMC_CODEX_ROOT"] = str(self._tmp / "codex")
        cwd = str(self.root.resolve())
        cx = self._tmp / "codex" / "2026" / "10"
        # 설치 전: 사람 세션 2개(지시 2·3개) + 심사 전용 세션 1개(요청 5건) + 사람 세션에 섞인 심사 요청 4건
        _rollout(cx / "20" / "rollout-a.jsonl", cwd, "a", "2026-10-20", ["고쳐줘", "테스트도"], guardian=4)
        _rollout(cx / "20" / "rollout-b.jsonl", cwd, "b", "2026-10-20", ["화면 확인", "다시", "좋아"])
        _rollout(cx / "20" / "rollout-g.jsonl", cwd, "g", "2026-10-20", [GUARD] * 5)
        # 설치 후 세션
        _rollout(cx / "28" / "rollout-c.jsonl", cwd, "c", "2026-10-28", ["새 작업"], guardian=2)
        cfg = read_json(self.root / ".harness" / "config.json")
        cfg["created_at"] = "2026-10-23T09:00:00+09:00"
        write_json(self.root / ".harness" / "config.json", cfg)

    def test_parse_paths_drop_guardian(self):
        ss, info = sessions_input.load(self.root)
        by = {s["session_id"]: s for s in ss}
        self.assertEqual(by["g"]["kind"], "guardian")
        self.assertEqual(by["g"]["human_prompts"], [])
        self.assertEqual(by["a"]["kind"], "main")
        self.assertEqual([p["text"] for p in by["a"]["human_prompts"]], ["고쳐줘", "테스트도"])
        self.assertEqual(by["a"]["human_turns"], 2)
        self.assertEqual(by["a"]["guardian_requests"], 4)
        b = baseline.compute([s for s in ss if s["started_at"] < "2026-10-23"])
        self.assertEqual(b["human_prompts"], 5)  # 심사 요청 9건은 사람 지시가 아니다
        self.assertEqual(b["sessions"]["guardian"], 1)
        # 내보내기 묶음 경로도 같은 함수로 거른다
        exp = sessions_input.from_export({"id": "e", "agent": "codex", "title": "x", "messages": [
            {"role": "user", "content": GUARD}, {"role": "user", "content": "사람 지시"}]})
        self.assertEqual([p["text"] for p in exp["human_prompts"]], ["사람 지시"])

    def test_old_baseline_corrected_and_reported(self):
        # 0.1.4가 쓴 옛 기준선: 심사 요청이 사람 지시로 섞임(표시 없음)
        old = {"definitions": baseline.DEFINITIONS, "source": {"source": "this_pc"}, "after": None,
               "before": {"period": ["2026-10-20T09:00:00Z", "2026-10-20T09:00:00Z"], "human_prompts": 14,
                          "sessions": {"main_with_prompts": 3}, "interventions_per_session": {"median": 5}}}
        ledger.write_baseline(self.root, old)
        ss, info = sessions_input.load(self.root)
        res = weekly.refresh_baselines(self.root, ss, info, run_tests=False)
        g = res["guardian"]
        self.assertEqual(g["status"], "corrected")
        self.assertEqual((g["human_prompts_before"], g["human_prompts_after"]), (14, 5))
        self.assertEqual(g["guardian_requests"], 9)
        bl = ledger.read_baseline(self.root)
        self.assertTrue(bl["guardian_filtered"])
        self.assertEqual(bl["before"]["human_prompts"], 5)
        self.assertEqual(bl["before_uncorrected"]["human_prompts"], 14)
        self.assertEqual(bl["latest"]["human_prompts"], 6)  # D5: 이번에 다시 읽은 전체(설치 후 포함)
        # 두 번째는 정정하지 않는다(표시가 있다)
        self.assertIsNone(weekly.refresh_baselines(self.root, ss, info, run_tests=False)["guardian"])
        self.assertEqual(ledger.read_baseline(self.root)["before"]["human_prompts"], 5)
        html = report.build_html(self.root)
        self.assertIn("기준선 정정", html)
        self.assertIn("지시 14건 → 5건", html)
        self.assertIn("설치 전 기준선:", html)  # D5 머리말 — 두 기간 나란히
        self.assertIn("2026-10-28", html)

    def test_export_baseline_only_marked(self):
        ledger.write_baseline(self.root, {"source": {"source": "sessions_dir"}, "before": {"human_prompts": 3}})
        self.assertEqual(baseline.correct_guardian(self.root, [])["status"], "marked")
        self.assertEqual(ledger.read_baseline(self.root)["before"]["human_prompts"], 3)


class WeeklyRefreshTest(HarnessTestCase):
    def test_weekly_job_refreshes_baselines(self):
        """재진단 본체가 기준선(latest)·테스트 실패 기준선을 다시 잰다."""
        self.write("a.py", "x = 1\n")
        self.commit("base")
        facts.collect(self.root, run_tests=False, use_llm=False)
        data = facts.load_raw(self.root)
        data["facts"]["test_baseline"]["value"] = [{"runner": "pytest", "dir": ".", "status": "fail",
                                                    "failed": ["t/test_a.py", "t/test_b.py"], "errors": [], "collection_errors": []}]
        data["facts"]["commands"]["value"] = [{"kind": "test", "name": "pytest", "dir": ".", "source": "pyproject.toml", "run": "pytest"}]
        write_json(facts.facts_path(self.root), data)
        fresh = {"runner": "pytest", "dir": ".", "status": "fail", "failed": ["t/test_b.py"], "errors": [], "collection_errors": []}
        sess = [{"session_id": "s", "agent": "claude", "kind": "main", "started_at": "2026-10-28T00:00:00Z",
                 "human_prompts": [{"at": "2026-10-28T00:00:00Z", "text": "고쳐줘"}], "tool_calls": []}]
        with mock.patch.object(facts, "pytest_baseline", return_value=fresh), \
                mock.patch.object(sessions_input, "load", return_value=(sess, {"main": 1, "source": "this_pc"})), \
                mock.patch("vmc_harness.bootstrap.diagnose.run", return_value={"status": "ok", "proposals": []}), \
                mock.patch.object(report, "build", return_value={"html": None, "pdf": None}):
            st = weekly.job(self.root, pdf=False)
        self.assertEqual(st["baselines"]["tests"], {"before": 2, "after": 1})
        self.assertEqual(facts.load(self.root)["test_baseline"]["failures"], ["t/test_b.py"])
        self.assertEqual(ledger.read_baseline(self.root)["before"]["human_prompts"], 1)
        # 잴 수 없던 실행(시간 초과)은 지난 값을 지킨다
        with mock.patch.object(facts, "pytest_baseline", return_value={"runner": "pytest", "dir": ".", "status": "timeout"}):
            facts.refresh_test_baseline(self.root)
        self.assertEqual(facts.load(self.root)["test_baseline"]["failures"], ["t/test_b.py"])


class FileListTest(HarnessTestCase):
    def test_walk_files_follows_gitignore(self):
        self.write(".gitignore", ".next*/\noutput/\n")
        self.write("app/page.ts", "export default 1\n")
        self.write(".next-master/server/chunk.js", "x\n")
        self.write("output/playwright/copy.ts", "x\n")
        self.write("docs/한글 폴더/a.ts", "x\n")
        self.commit("base")
        self.write("app/new.ts", "y\n")  # 미추적이지만 무시 대상 아님 → 포함
        files = sorted(util.rel(self.root, f) for f in util.walk_files(self.root, util.SOURCE_EXT)[0])
        self.assertEqual(files, ["app/new.ts", "app/page.ts", "docs/한글 폴더/a.ts"])
        sub = sorted(util.rel(self.root, f) for f in util.walk_files(self.root / "app")[0])
        self.assertEqual(sub, ["app/new.ts", "app/page.ts"])

    def test_walk_files_without_git_falls_back(self):
        d = Path(tempfile.mkdtemp(prefix="vmh-nogit-"))
        try:
            (d / "a").mkdir()
            (d / "a" / "x.py").write_text("x\n", encoding="utf-8")
            (d / "node_modules").mkdir()
            (d / "node_modules" / "y.py").write_text("y\n", encoding="utf-8")
            with mock.patch.object(util, "git_files", return_value=None):
                files = [p.relative_to(d).as_posix() for p in util.walk_files(d)[0]]
            self.assertEqual(files, ["a/x.py"])
        finally:
            shutil.rmtree(str(d), ignore_errors=True)


class SqlScopeTest(HarnessTestCase):
    def test_docs_with_sql_only_sql_is_db(self):
        from vmc_harness.gates import common as gcommon
        self.write("docs/reports/r1/query.sql", "select 1\n")
        self.write("docs/reports/r1/shots/a.png", "png")
        self.write("docs/reports/r1/README.md", "# r\n")
        self.write("be/app.py", "x = 1\n")
        self.commit("base")
        facts.collect(self.root, run_tests=False, use_llm=False)
        db = facts.load(self.root)["behavior_globs"]["db"]
        self.assertNotIn("docs/**", db)
        self.assertIn("docs/**/*.sql", db)
        self.assertIsNone(gcommon.behavior_kind(self.root, "docs/reports/r1/shots/a.png"))
        self.assertIsNone(gcommon.behavior_kind(self.root, "docs/reports/r1/README.md"))
        self.assertEqual(gcommon.behavior_kind(self.root, "docs/reports/r1/query.sql"), "db")
