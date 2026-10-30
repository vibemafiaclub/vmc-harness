"""테스트 공용: 임시 홈·임시 프로젝트(git 레포 + .harness/config.json)."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from vmc_harness.core.ids import new_uuid
from vmc_harness.core.jsonio import write_json


class HarnessTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="vmh-test-"))
        self.home = self._tmp / "home"
        self.root = self._tmp / "proj"
        self.root.mkdir()
        self._old_env = dict(os.environ)
        self._old_cwd = os.getcwd()
        os.environ["VMC_HARNESS_HOME"] = str(self.home)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.root, check=True)
        self.project_id = new_uuid()
        write_json(self.root / ".harness" / "config.json",
                   {"project_id": self.project_id, "profile": "standard", "source_roots": ["."]})

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_env)
        # 테스트가 임시 프로젝트로 chdir한 채 끝나면 지우기 전에 돌아온다 — 지워진 폴더가 현재 폴더로 남으면
        # 뒤 테스트의 os.getcwd()가 실패한다(전체 실행 순서 의존 실패)
        try:
            here = os.getcwd()
        except OSError:
            here = None
        if here is None or Path(here).resolve().is_relative_to(self._tmp.resolve()):
            os.chdir(self._old_cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def commit(self, msg="c"):
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", msg, "--allow-empty"], cwd=self.root, check=True)
