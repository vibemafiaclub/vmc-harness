import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from tests.util import HarnessTestCase

from vmc_harness.task import declare as dec, pm, report, state as st, trailers

ENGINE = str(Path(__file__).resolve().parent.parent)


def raw(**kw):
    d = {"intent": "정렬 버그 수정", "scope": ["src/list.py"],
         "predict": {"tokens": [1, 2, 3], "agent_sec": [60, 120, 170]}}
    d.update(kw)
    return d


class TrailerTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        for k in st.SESSION_ENV:
            os.environ.pop(k, None)
        self.write("src/list.py", "a = 1\n")
        self.write("src/other.py", "b = 1\n")
        self.commit()
        st.session_touch(self.root, "S1", "claude")

    def test_apply_idempotent_and_before_comments(self):
        msg = "fix: 정렬\n\n본문\n# Please enter the commit message\n# ...\n"
        out = trailers.apply(msg, ["하네스 변경: .harness/gates/g.json"], ["Harness-Task: T-1"])
        self.assertEqual(out.split("\n# Please")[0],
                         "fix: 정렬\n\n본문\n\n하네스 변경: .harness/gates/g.json\n\nHarness-Task: T-1\n")
        self.assertEqual(trailers.apply(out, ["하네스 변경: .harness/gates/g.json"], ["Harness-Task: T-1"]), out)
        out2 = trailers.apply("x\n\nCo-Authored-By: a <b>\n", [], ["Harness-Task: T-1"])
        self.assertEqual(out2, "x\n\nCo-Authored-By: a <b>\nHarness-Task: T-1\n")

    def test_git_hook_end_to_end(self):
        t = dec.declare(self.root, raw())["task"]
        hook = trailers.install_commit_hook(self.root)
        self.assertTrue(hook.is_file())
        self.write("src/list.py", "a = 2\n")
        self.write("src/other.py", "b = 2\n")
        self.write(".harness/gates/g1.json", "{}\n")
        self.write(".harness/ledger/tasks/x.jsonl", "{}\n")
        env = dict(os.environ, PYTHONPATH=ENGINE, VMC_HARNESS_SESSION_ID="S1")
        subprocess.run(["git", "add", "-A"], cwd=str(self.root), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "fix: 정렬"], cwd=str(self.root), check=True, env=env)
        body = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=str(self.root), capture_output=True,
                              text=True, encoding="utf-8").stdout
        self.assertIn("Harness-Task: %s" % t["task_id"], body)
        self.assertIn("Harness-OutOfScope: src/other.py", body)
        self.assertIn("하네스 변경: .harness/gates/g1.json", body)
        self.assertNotIn("ledger", body)
        st.note_action(self.root, t["task_id"], "A-261022-aaaaaa")
        self.write(".harness/gates/g2.json", "{}\n")
        subprocess.run(["git", "add", "-A"], cwd=str(self.root), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "gate"], cwd=str(self.root), check=True, env=env)
        body = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=str(self.root), capture_output=True,
                              text=True, encoding="utf-8").stdout
        self.assertIn("Harness-Action: A-261022-aaaaaa", body)
        self.assertTrue(trailers.uninstall_commit_hook(self.root))
        self.assertFalse(hook.exists())

    def test_existing_hook_chained(self):
        hooks = self.root / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        old = hooks / "prepare-commit-msg"
        old.write_text("#!/bin/sh\necho mine >> \"$1\"\n", encoding="utf-8")
        old.chmod(0o755)
        trailers.install_commit_hook(self.root)
        self.assertTrue((hooks / "prepare-commit-msg.local").is_file())
        trailers.uninstall_commit_hook(self.root)
        self.assertIn("echo mine", old.read_text(encoding="utf-8"))

    def test_report_block(self):
        dec.declare(self.root, raw())
        st_task = st.current(self.root, "S1")
        self.write("src/list.py", "a = 3\n")
        self.write("src/other.py", "b = 3\nc = 4\n")
        report.record_reason(self.root, st_task, "src/other.py", "폭 조정으로 메뉴 항목 1개 숨김")
        rec = report.build(self.root, st.current(self.root, "S1"))
        lines = rec["text"].splitlines()
        self.assertEqual(lines[0], "범위 밖 변경 1건")
        self.assertEqual(lines[1], "- src/other.py: 폭 조정으로 메뉴 항목 1개 숨김")
        self.assertIn("증거: 없음", rec["text"])
        # claude 없이 — PATH에는 git 폴더만(유닉스 경로 고정은 Windows에서 git까지 지운다)
        env = dict(os.environ, PYTHONPATH=ENGINE, VMC_HARNESS_SESSION_ID="S1",
                   PATH=os.path.dirname(shutil.which("git")))
        p = subprocess.run([sys.executable, "-m", "vmc_harness", "report"], cwd=str(self.root), env=env,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith("범위 밖 변경 1건"))


class PmAsyncTest(HarnessTestCase):
    def test_detached_bundle_writes_result(self):
        """비동기 경로 실측: 분리 실행된 task-bg가 결과 파일을 쓴다(claude 없는 PATH — PM은 미호출로 남는다)."""
        self.write("a.py", "x = 1\n")
        self.commit()
        st.session_touch(self.root, "S1", "claude")
        empty = self._tmp / "emptybin"
        empty.mkdir()
        os.environ["PATH"] = str(empty)
        t0 = time.time()
        r = dec.declare(self.root, raw(scope=["src/"], predict={"tokens": [1, 2, 3], "agent_sec": [600, 900, 1200]}))
        declare_sec = time.time() - t0
        self.assertEqual(r["pm_mode"], "async")
        rec = pm.wait(self.root, r["task"]["task_id"], 15)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "done")
        self.assertFalse(rec["pm"]["called"])
        self.assertLess(declare_sec, 5)
