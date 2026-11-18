"""하네스 소유 파일의 경로 지정 커밋(규칙 블록·rules.json, spec 09) — 사람 index·사람 변경을 건드리지 않는다. 합성 데이터만."""

import subprocess

from tests.util import HarnessTestCase
from vmc_harness.core.jsonio import write_json
from vmc_harness.gates import rules, selfcommit


def git(root, *a):
    return subprocess.run(["git"] + list(a), cwd=root, capture_output=True, text=True).stdout


RULES = {"rules": [{"id": "r-x1", "basis": {"type": "explicit_request", "quote": "로그는 지우지 마",
                                             "source": {"date": "2026-11-18"}},
                    "action_id": "A-261118-aaaaaa", "enabled": True}]}


class SelfCommitTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.write("AGENTS.md", "# 팀 규칙\n\n사람이 쓴 줄\n")
        self.write("src/app.py", "x = 1\n")
        self.commit("init")

    def test_render_commits_only_harness_paths_with_trailer(self):
        self.write("src/app.py", "x = 2\n")                      # 사람 작업 중 변경(커밋되면 안 됨)
        git(self.root, "add", "src/app.py")                       # 사람이 staged 해 둔 것
        write_json(self.root / ".harness" / "rules.json", RULES)
        res = rules.render(str(self.root))
        c = res["commit"]
        self.assertTrue(c and c["ok"] and c["commit"], c)
        files = git(self.root, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(sorted(files), [".harness/rules.json", "AGENTS.md", "CLAUDE.md"])
        self.assertIn("Harness-Action: A-261118-aaaaaa", git(self.root, "log", "-1", "--format=%B"))
        st = git(self.root, "status", "--porcelain")
        self.assertIn("M  src/app.py", st)                         # 사람 staged 그대로
        self.assertNotIn("AGENTS.md", st)                          # 사람 index도 새 내용으로 맞춰짐
        self.assertNotIn("rules.json", st)

    def test_skips_agents_md_when_human_edited_outside_block(self):
        self.write("AGENTS.md", "# 팀 규칙\n\n사람이 고친 줄\n")
        write_json(self.root / ".harness" / "rules.json", RULES)
        res = rules.render(str(self.root))
        files = git(self.root, "show", "--name-only", "--format=", "HEAD").split()
        self.assertNotIn("AGENTS.md", files)
        self.assertIn(".harness/rules.json", files)
        self.assertIn("M AGENTS.md", git(self.root, "status", "--porcelain"))   # 사람 몫은 사람이 커밋
        self.assertTrue(res["commit"]["ok"])

    def test_skips_when_human_staged_same_path(self):
        write_json(self.root / ".harness" / "rules.json", RULES)
        git(self.root, "add", ".harness/rules.json")
        write_json(self.root / ".harness" / "rules.json", {"rules": []})
        head = git(self.root, "rev-parse", "HEAD")
        r = selfcommit.commit_paths(str(self.root), [".harness/rules.json"], "chore(harness): t")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "staged_by_human")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)

    def test_skips_during_rebase_and_when_disabled(self):
        (self.root / ".git" / "rebase-merge").mkdir()
        write_json(self.root / ".harness" / "rules.json", RULES)
        r = selfcommit.commit_paths(str(self.root), [".harness/rules.json"], "t")
        self.assertEqual(r["reason"], "busy")

    def test_no_change_no_commit(self):
        head = git(self.root, "rev-parse", "HEAD")
        r = selfcommit.commit_paths(str(self.root), ["AGENTS.md"], "t")
        self.assertTrue(r["ok"])
        self.assertIsNone(r["commit"])
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)
