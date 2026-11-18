"""mod 로컬 경로 설치: marketplace add <mod 폴더> → install --scope local (spec 08·10)."""

from unittest import mock

from tests.util import HarnessTestCase

from vmc_harness.render import mod_install


class ModInstallTest(HarnessTestCase):
    def test_register_unregister(self):
        calls = []

        def fake_run(args, cwd=None, timeout=None, **kw):
            calls.append(args)
            return 0, "{}", ""
        with mock.patch.object(mod_install, "_claude", return_value="/bin/claude"), \
                mock.patch.object(mod_install, "run", side_effect=fake_run):
            r = mod_install.register(self.root)
            self.assertTrue(r["ok"], r)
            self.assertTrue(mod_install.is_registered(self.root))
            self.assertEqual(calls[0][1:4], ["plugin", "marketplace", "add"])
            self.assertTrue(calls[0][4].endswith("mod"))
            self.assertIn("vmc-harness@vmc-harness-local", calls[1])
            self.assertIn("local", calls[1])
            mod_install.unregister(self.root)
            self.assertFalse(mod_install.is_registered(self.root))
            self.assertIn("uninstall", calls[-1])

    def test_no_claude(self):
        with mock.patch.object(mod_install, "_claude", return_value=None):
            self.assertFalse(mod_install.register(self.root)["ok"])

    def test_registered_record_with_deleted_mod_dir_is_not_registered(self):
        from vmc_harness.core.jsonio import write_json
        write_json(mod_install._state_path(self.root), {"registered": True, "mod_dir": str(self.root / "gone" / "mod")})
        self.assertFalse(mod_install.is_registered(self.root))   # 옛 커널 버전 폴더가 지워진 경우(2026-11-18 실측)
