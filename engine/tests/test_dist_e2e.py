"""dist E2E — 실제 서버(context-hub apps/api를 `next dev` 테스트 모드로)에 install → activate → download → update → lock → 오프라인 번들.

느려서(서버 첫 컴파일) 기본으로는 건너뛴다. 돌리려면:
  VMC_HARNESS_E2E_API_DIR=/Users/sumin/repos/vibemafiaclub/context-hub/apps/api python3 -m unittest tests.test_dist_e2e
apps/api에 node_modules가 있어야 한다(pnpm install). 서버는 HARNESS_API_TEST_MODE=1 — DB·R2 대신 메모리·로컬 폴더.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from tests.test_dist_shell import DistTestCase, load_release, test_key

from vmc_harness.dist import lock, shell

API_DIR = os.environ.get("VMC_HARNESS_E2E_API_DIR")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@unittest.skipUnless(API_DIR and (Path(API_DIR) / "node_modules" / ".bin" / "next").exists(), "VMC_HARNESS_E2E_API_DIR 미설정")
class ApiE2ETest(DistTestCase):
    def setUp(self):
        super().setUp()
        rel = load_release()
        self.build = rel.build(self.out)
        self.v = self.build["version"]
        srv = self._tmp / "srv"
        (srv / "install").mkdir(parents=True)
        (srv / "releases").mkdir()
        (srv / "install" / "install.py").write_text(rel.render_install(), encoding="ascii")
        r2_key = "releases/" + self.build["path"].name
        shutil.copy(str(self.build["path"]), str(srv / r2_key))
        (srv / "releases.json").write_text(json.dumps([{"version": self.v, "sha256": self.build["sha256"], "size": self.build["size"], "r2_key": r2_key}]))
        self.key = test_key()
        port = free_port()
        self.url = "http://127.0.0.1:%d" % port
        env = dict(os.environ, HARNESS_API_TEST_MODE="1", HARNESS_API_TEST_DIR=str(srv), HARNESS_API_TEST_KEYS=self.key, NEXT_TELEMETRY_DISABLED="1")
        env.pop("VERCEL_ENV", None)
        self.log = open(str(self._tmp / "next.log"), "w")
        self.proc = subprocess.Popen([str(Path(API_DIR) / "node_modules" / ".bin" / "next"), "dev", "-p", str(port), "-H", "127.0.0.1"],
                                     cwd=API_DIR, env=env, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.url + "/harness/install.py", timeout=30) as r:
                    if r.status == 200:
                        self.install_text = r.read().decode("ascii")
                        break
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.5)
        else:
            self.fail("next dev가 뜨지 않음: %s" % (self._tmp / "next.log").read_text()[-2000:])
        os.environ["VMC_HARNESS_SERVER"] = self.url

    def tearDown(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=20)
        except Exception:
            pass
        self.log.close()
        super().tearDown()

    def run_cli(self, *args, home=None):
        env = dict(os.environ)
        if home:
            env["VMC_HARNESS_HOME"] = str(home)
        launcher = Path(env["VMC_HARNESS_HOME"]) / "bin" / "vmc-harness"
        return subprocess.run([str(launcher)] + list(args), env=env, capture_output=True, text=True, cwd=str(self._tmp), timeout=300)

    def test_full_flow(self):
        # ① 설치: 서버가 서빙한 install.py를 표준 입력으로(curl | python3 - 와 같다)
        r = subprocess.run([sys.executable, "-", "--no-path"], input=self.install_text, capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.run_cli("brief").returncode, 2)  # 커널 없음 → activate 먼저
        # ② 인증 + ③ 받기
        r = self.run_cli("activate", self.key)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(shell.current_version(), self.v)
        kdir = shell.kernel_root() / self.v
        self.assertFalse(os.access(str(kdir / "engine" / "vmc_harness" / "__init__.py"), os.W_OK))
        self.assertEqual(shell.read_license()["org"], "test-org-0")
        # 껍데기 → 커널 엔진으로 넘긴다
        r = self.run_cli("--version")
        self.assertEqual(r.stdout.strip(), self.v, r.stderr)
        r = self.run_cli("update")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(self.v, r.stdout)
        # 틀린 키는 401
        req = urllib.request.Request(self.url + "/harness/releases/%s/download" % self.v, headers={"Authorization": "Bearer " + test_key()})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(cm.exception.code, 401)
        cm.exception.close()
        # 잠금: 이 PC가 가져가면 다른 PC는 못 받는다
        self.assertTrue(lock.acquire(self.root, 120))
        self.assertEqual(json.loads(lock._file(self.root).read_text(encoding="utf-8"))["mode"], "server")
        other = {"project_hash": lock.project_hash(self.root), "pc_id": "other-pc", "ttl_s": 60}
        st, data = shell.api("POST", "/harness/lock", body=other, key=self.key)
        self.assertEqual((st, data["granted"], data["holder"]), (200, False, shell.pc_id()))
        lock.release(self.root)
        st, data = shell.api("POST", "/harness/lock", body=other, key=self.key)
        self.assertTrue(data["granted"])
        st, data = shell.api("DELETE", "/harness/lock", body={"project_hash": other["project_hash"], "pc_id": "other-pc"}, key=self.key)
        self.assertTrue(data["released"])

    def test_offline_bundle_flow(self):
        rel = load_release()
        info = {"license_id": "lic-offline", "org": "오프라인사", "expires_at": None, "key_hash": shell.key_hash(self.key)}
        bundle = rel.make_bundle(self.build["path"], info, self._tmp / "usb")
        unpacked = self._tmp / "usb-open"
        with zipfile.ZipFile(str(bundle)) as zf:
            zf.extract("install.py", str(unpacked))
        os.environ["VMC_HARNESS_SERVER"] = "http://127.0.0.1:9"  # 사내망에서 막힌 상황
        r = subprocess.run([sys.executable, str(unpacked / "install.py"), "--no-path"], capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_cli("activate", self.key)
        self.assertEqual(r.returncode, 3)
        self.assertIn("--offline", r.stdout)
        r = self.run_cli("activate", "--offline", self.key, "--bundle", str(bundle))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(shell.current_version(), self.v)
        self.assertEqual(shell.read_license()["mode"], "offline")
        r = self.run_cli("--version")
        self.assertEqual(r.stdout.strip(), self.v)
