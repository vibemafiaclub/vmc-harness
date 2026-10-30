"""dist — 껍데기(shell.py)·릴리스 zip·커널 설치·오프라인 번들·설치 스크립트·홈 정리. 네트워크 없음."""

import hashlib
import importlib.util
import io
import json
import os
import secrets as pysecrets
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

from tests.util import HarnessTestCase

from vmc_harness.dist import shell

REPO = Path(__file__).resolve().parents[2]


def load_release():
    spec = importlib.util.spec_from_file_location("vmh_release", str(REPO / "scripts" / "release.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_key():
    return "vmh_" + pysecrets.token_urlsafe(32)


def synthetic_zip(path, version, extra=None):
    """작은 가짜 릴리스(engine/vmc_harness/__init__.py만)."""
    files = {"engine/vmc_harness/__init__.py": ('__version__ = "%s"\n' % version).encode()}
    files.update(extra or {})
    man = {"name": "vmc-harness", "version": version, "files": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}}
    with zipfile.ZipFile(str(path), "w") as zf:
        zf.writestr("manifest.json", json.dumps(man))
        for k, v in files.items():
            zf.writestr(k, v)
    return path


def sha_file(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class DistTestCase(HarnessTestCase):
    def setUp(self):
        super().setUp()
        os.environ["VMC_HARNESS_SECRETS_BACKEND"] = "file"
        os.environ["VMC_HARNESS_SERVER"] = "http://127.0.0.1:9"  # 닿지 않는 주소 — 실수로 운영 서버를 부르지 않게
        self.out = self._tmp / "out"
        self.out.mkdir()


class ReleaseZipTest(DistTestCase):
    def test_build_verify_and_install_real_engine(self):
        rel = load_release()
        b = rel.build(self.out)
        man = shell.verify_release_zip(b["path"], b["version"])
        self.assertIn("engine/vmc_harness/dist/shell.py", man["files"])
        self.assertIn("install/install.py", man["files"])
        self.assertFalse(any("__pycache__" in k or k.endswith(".pyc") for k in man["files"]))
        self.assertFalse(man["eval_passed"])
        v = shell.install_release(b["path"], b["version"], b["sha256"])
        self.assertEqual(v, b["version"])
        self.assertEqual(shell.current_version(), v)
        kdir = shell.kernel_root() / v
        self.assertTrue((kdir / ".installed.json").is_file())
        init = kdir / "engine" / "vmc_harness" / "__init__.py"
        if os.name != "nt":
            self.assertFalse(os.access(str(init), os.W_OK))
            self.assertFalse(os.access(str(kdir / "engine" / "vmc_harness"), os.W_OK))
        # 미리 컴파일돼 있다(읽기 전용이라 실행 중에 못 쓴다)
        if not getattr(sys, "pycache_prefix", None):  # 맥 Xcode 파이썬은 .pyc를 사용자 캐시 폴더에 둔다
            self.assertTrue(list((kdir / "engine" / "vmc_harness" / "__pycache__").glob("*.pyc")))
        env = dict(os.environ, PYTHONPATH=str(kdir / "engine"))
        r = subprocess.run([sys.executable, "-c", "import vmc_harness;print(vmc_harness.__file__)"],
                           env=env, capture_output=True, text=True, cwd=str(self._tmp))
        self.assertIn(str(kdir), r.stdout)

    def test_tamper_and_bad_paths_rejected(self):
        z = synthetic_zip(self.out / "a.zip", "0.0.1")
        # 파일 내용 바꾸기
        bad = self.out / "bad.zip"
        with zipfile.ZipFile(str(z)) as src, zipfile.ZipFile(str(bad), "w") as dst:
            for n in src.namelist():
                data = src.read(n)
                dst.writestr(n, data + b"# x\n" if n.endswith(".py") else data)
        with self.assertRaises(shell.HarnessError):
            shell.verify_release_zip(bad)
        slip = synthetic_zip(self.out / "slip.zip", "0.0.1", {"../evil.py": b"x"})
        with self.assertRaises(shell.HarnessError):
            shell.verify_release_zip(slip)
        with self.assertRaises(shell.HarnessError):
            shell.install_release(z, "0.0.1", "0" * 64)  # 서버 sha256과 다름
        self.assertIsNone(shell.current_version())

    def test_prune_keeps_current_and_previous(self):
        for v in ("0.0.1", "0.0.2", "0.0.3"):
            z = synthetic_zip(self.out / ("%s.zip" % v), v)
            shell.install_release(z, v, sha_file(z), keep=2)
        self.assertEqual(shell.current_version(), "0.0.3")
        self.assertEqual(sorted(v for _, v in shell.installed_versions()), ["0.0.2", "0.0.3"])
        self.assertFalse((shell.kernel_root() / "0.0.1").exists())
        # 같은 버전 재설치는 그대로
        z = self.out / "0.0.3.zip"
        self.assertEqual(shell.install_release(z, "0.0.3", sha_file(z)), "0.0.3")


class OfflineBundleTest(DistTestCase):
    def _bundle(self, key, expires_at=None, tamper=False):
        rel = load_release()
        z = synthetic_zip(self.out / "vmc-harness-0.0.5.zip", "0.0.5")
        if tamper:
            # 번들 안 릴리스를 바꾸면 bundle.json의 sha256과 어긋난다
            pass
        info = {"license_id": "lic-1", "org": "테스트사", "expires_at": expires_at, "key_hash": shell.key_hash(key)}
        out = rel.make_bundle(z, info, self.out / "bundles")
        if tamper:
            t = out.with_name("tampered.zip")
            with zipfile.ZipFile(str(out)) as src, zipfile.ZipFile(str(t), "w") as dst:
                for n in src.namelist():
                    data = src.read(n)
                    if n == "vmc-harness-0.0.5.zip":
                        data = synthetic_zip(self.out / "other.zip", "0.0.5", {"engine/x.py": b"evil"}).read_bytes()
                    dst.writestr(n, data)
            return t
        return out

    def test_offline_activate_ok(self):
        key = test_key()
        b = self._bundle(key)
        msgs = []
        self.assertEqual(shell.activate_offline(key, b, out=msgs.append), 0, msgs)
        self.assertEqual(shell.current_version(), "0.0.5")
        lic = shell.read_license()
        self.assertEqual((lic["license_id"], lic["org"], lic["mode"]), ("lic-1", "테스트사", "offline"))
        self.assertEqual(shell.load_key(), key)
        # 키 원문은 license.json에 없다
        self.assertNotIn(key, shell.license_file().read_text(encoding="utf-8"))

    def test_offline_wrong_key_expired_tampered(self):
        key = test_key()
        msgs = []
        self.assertEqual(shell.activate_offline(test_key(), self._bundle(key), out=msgs.append), 2)
        self.assertEqual(shell.activate_offline(key, self._bundle(key, expires_at="2020-01-01T00:00:00Z"), out=msgs.append), 2)
        self.assertEqual(shell.activate_offline(key, self._bundle(key, tamper=True), out=msgs.append), 2)
        self.assertEqual(shell.activate_offline("not-a-key", self._bundle(key), out=msgs.append), 2)
        self.assertIsNone(shell.current_version())
        self.assertIsNone(shell.read_license())


class InstallScriptTest(DistTestCase):
    def test_render_ascii_and_install_from_stdin(self):
        rel = load_release()
        text = rel.render_install()
        text.encode("ascii")  # PowerShell 5.1 파이프(ASCII)로 깨지지 않는다
        r = subprocess.run([sys.executable, "-", "--no-path"], input=text, capture_output=True, text=True,
                           env=dict(os.environ), cwd=str(self._tmp))
        self.assertEqual(r.returncode, 0, r.stderr)
        installed = shell.shell_path().read_text(encoding="utf-8")
        self.assertEqual(installed, (REPO / "engine" / "vmc_harness" / "dist" / "shell.py").read_text(encoding="utf-8"))
        lp = shell.launcher_path()
        self.assertTrue(lp.is_file())
        if os.name != "nt":
            self.assertTrue(os.stat(str(lp)).st_mode & stat.S_IXUSR)
            r = subprocess.run([str(lp), "--shell-version"], capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), shell.SHELL_VERSION)
            r = subprocess.run([str(lp), "brief"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)
            self.assertIn("activate", r.stderr)

    def test_path_register_idempotent_and_unregister(self):
        if os.name == "nt":
            self.skipTest("POSIX rc 파일")
        fake_home = self._tmp / "userhome"
        fake_home.mkdir()
        os.environ["HOME"] = str(fake_home)
        os.environ["SHELL"] = "/bin/zsh"
        (fake_home / ".zshrc").write_text("alias x=y", encoding="utf-8")
        self.assertEqual(shell.register_path(), [str(fake_home / ".zshrc")])
        self.assertEqual(shell.register_path(), [])
        text = (fake_home / ".zshrc").read_text(encoding="utf-8")
        self.assertEqual(text.count(shell.PATH_MARKER), 1)
        self.assertTrue(text.startswith("alias x=y\n"))
        self.assertEqual(shell.unregister_path(), [str(fake_home / ".zshrc")])
        self.assertEqual((fake_home / ".zshrc").read_text(encoding="utf-8"), "alias x=y\n")


class ConfigAndSigningTest(DistTestCase):
    def test_shell_constants_match_config(self):
        from vmc_harness.dist import settings
        self.assertEqual(shell.DEFAULT_BASE_URL, settings.get("server.base_url"))
        self.assertEqual(shell.CALL_TIMEOUT_S, settings.get("server.call_timeout_s"))
        self.assertEqual(shell.DOWNLOAD_TIMEOUT_S, settings.get("update.download_timeout_s"))
        self.assertEqual(shell.KEEP_VERSIONS, settings.get("update.keep_versions"))

    def test_sigv4_matches_aws_example(self):
        rel = load_release()
        e = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        _, sig = rel.sigv4("GET", "examplebucket.s3.amazonaws.com", "/test.txt",
                           {"host": "examplebucket.s3.amazonaws.com", "range": "bytes=0-9", "x-amz-content-sha256": e, "x-amz-date": "20130524T000000Z"},
                           e, "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1", "s3", "20130524T000000Z")
        self.assertEqual(sig, "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")

    def test_publish_requires_eval_passed(self):
        rel = load_release()
        with self.assertRaises(SystemExit) as cm:
            rel.main(["publish", "--dry-run", "--out", str(self.out)])
        self.assertIn("eval-passed", str(cm.exception))


class UninstallTest(DistTestCase):
    def test_remove_home_keeps_brain(self):
        from vmc_harness.dist import uninstall
        z = synthetic_zip(self.out / "z.zip", "0.0.7")
        shell.install_release(z, "0.0.7", sha_file(z))
        shell.store_key(test_key())
        (shell.home() / "brain").mkdir(parents=True)
        (shell.home() / "brain" / "card.md").write_text("x", encoding="utf-8")
        os.environ["HOME"] = str(self._tmp / "userhome")
        r = uninstall.remove_home(keep_brain=True)
        self.assertTrue((shell.home() / "brain" / "card.md").is_file())
        self.assertFalse((shell.home() / "kernel").exists())
        self.assertIsNone(shell.load_key())
        self.assertTrue(r["removed"])
        uninstall.remove_home(keep_brain=False)
        self.assertFalse(shell.home().exists())


class KeyInputTest(DistTestCase):
    def test_pasted_key_cleaned(self):
        k = test_key()
        mid = len(k) // 2
        for raw in (" %s\r\n" % k, k[:mid] + "​" + k[mid:], k[:mid] + "­" + k[mid:], "<%s>" % k, '"%s"' % k,
                    k[:mid] + "\n" + k[mid:], k.replace("vmh_", "vmh_﻿")):
            self.assertEqual(shell.clean_key(raw), k, repr(raw))
        self.assertIsNone(shell.key_problem(k))
        dashed = "vmh_" + "a" * 20 + "-" + "b" * 22
        self.assertEqual(shell.clean_key(dashed.replace("-", "–")), dashed)

    def test_problem_says_why_without_key(self):
        k = test_key()
        self.assertIn("44자", shell.key_problem(k[:44]))
        self.assertIn("vmh_로 시작", shell.key_problem("vmc_" + k[4:]))
        msg = shell.key_problem(k[:10] + "é" + k[11:])
        self.assertIn("11번째", msg)
        self.assertNotIn(k[5:], msg)
        self.assertIn("비어", shell.key_problem(""))

    def test_activate_main_cleans_arg(self):
        lines = []
        rc = shell.activate_main(["​" + test_key()], out=lines.append)
        self.assertNotIn("형식", " ".join(lines))  # 형식은 통과하고 서버 연결(닿지 않는 주소)에서 멈춘다
        self.assertEqual(rc, 3)
