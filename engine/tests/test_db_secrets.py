import os
import stat
import sys
import unittest

from tests.util import HarnessTestCase
from vmc_harness.core import secrets


class FileSecretsTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        os.environ["VMC_HARNESS_SECRETS_BACKEND"] = "file"

    def test_roundtrip_and_permissions(self):
        v = 'p@ss "q" 한글;\\x\n둘째 줄'
        self.assertEqual(secrets.set_secret("db:p1:query", v), "file")
        self.assertEqual(secrets.get_secret("db:p1:query"), v)
        p = secrets._file_path("db:p1:query")
        self.assertNotIn(v, p.read_text(encoding="utf-8"))  # 평문 아님(base64) — 권한이 실제 보호
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)
        secrets.delete_secret("db:p1:query")
        self.assertIsNone(secrets.get_secret("db:p1:query"))

    def test_bad_name(self):
        with self.assertRaises(ValueError):
            secrets.set_secret('a"b', "x")


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("VMC_HARNESS_TEST_KEYCHAIN") == "1",
                     "실제 macOS 키체인 시험은 VMC_HARNESS_TEST_KEYCHAIN=1일 때만(사용자 키체인에 항목을 만들었다 지운다)")
class KeychainTest(HarnessTestCase):
    def test_keychain_roundtrip_not_in_argv(self):
        os.environ["VMC_HARNESS_SECRETS_BACKEND"] = "keychain"
        name = "selftest:%s" % self.project_id
        v = 'kc-p@ss "q" 한글'
        try:
            self.assertEqual(secrets.set_secret(name, v), "keychain")
            self.assertEqual(secrets.get_secret(name), v)
            self.assertFalse(secrets._file_path(name).exists())
        finally:
            secrets.delete_secret(name)
        self.assertIsNone(secrets.get_secret(name))


@unittest.skipUnless(sys.platform == "win32", "Windows 자격 증명 관리자")
class WinCredTest(HarnessTestCase):
    def test_wincred_roundtrip(self):
        os.environ["VMC_HARNESS_SECRETS_BACKEND"] = "wincred"
        name = "selftest:%s" % self.project_id
        try:
            self.assertEqual(secrets.set_secret(name, "wc-p@ss 한글"), "wincred")
            self.assertEqual(secrets.get_secret(name), "wc-p@ss 한글")
        finally:
            secrets.delete_secret(name)
        self.assertIsNone(secrets.get_secret(name))
