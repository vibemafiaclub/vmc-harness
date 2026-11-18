"""`.harness/.gitignore` — solo는 장부·어휘집을 로컬 원본으로, 추적 중이면 git rm 대신 꼭 알릴 일 한 번(spec 09). 합성 데이터만."""

import subprocess

from tests.util import HarnessTestCase
from vmc_harness.bootstrap import gitignore
from vmc_harness.core.jsonio import read_json, read_jsonl, write_json
from vmc_harness.core.project import local_dir


class GitignoreTest(HarnessTestCase):
    def _profile(self, p):
        cfg = read_json(self.root / ".harness" / "config.json")
        cfg["profile"] = p
        write_json(self.root / ".harness" / "config.json", cfg)

    def test_standard_only_cache(self):
        gitignore.ensure(str(self.root))
        self.assertEqual((self.root / ".harness" / ".gitignore").read_text(), "cache/\n")

    def test_solo_adds_ledger_and_keeps_human_lines(self):
        self._profile("solo")
        (self.root / ".harness" / ".gitignore").write_text("cache/\nmine.txt", encoding="utf-8")
        added = gitignore.ensure(str(self.root))
        self.assertEqual(added, ["ledger/", "lexicon.json"])
        self.assertEqual((self.root / ".harness" / ".gitignore").read_text(), "cache/\nmine.txt\nledger/\nlexicon.json\n")
        self.assertEqual(gitignore.ensure(str(self.root)), [])

    def test_tracked_local_files_notice_once_without_untracking(self):
        self._profile("solo")
        self.write(".harness/lexicon.json", "{}")
        self.write(".harness/ledger/actions/A-1.json", "{}")
        self.commit("old install")
        r = gitignore.migrate(str(self.root))
        self.assertEqual(r["tracked"], 2)
        ls = subprocess.run(["git", "ls-files"], cwd=self.root, capture_output=True, text=True).stdout
        self.assertIn(".harness/lexicon.json", ls)                       # 하네스가 index를 바꾸지 않는다
        gitignore.migrate(str(self.root))
        ns = [n for n in read_jsonl(local_dir(str(self.root)) / "notices.jsonl") if n["kind"] == "ledger_tracked"]
        self.assertEqual(len(ns), 1)
        self.assertIn("git rm -r --cached", ns[0]["text"])
