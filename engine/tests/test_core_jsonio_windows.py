"""현장 결함 F1·F2 — Windows 파일 교체 충돌 재시도, UTF-8이 아닌 파일 읽기."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vmc_harness.core import jsonio


class TestReplaceRetry(unittest.TestCase):
    def test_retries_permission_error_on_windows(self):
        calls = []
        real = os.replace

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(5, "Access is denied")
            real(src, dst)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(jsonio, "_windows", lambda: True), \
                mock.patch.object(jsonio.os, "replace", flaky), mock.patch.object(jsonio.time, "sleep", lambda s: None):
            p = Path(d) / "cost.json"
            jsonio.write_json(p, {"a": 1})
            self.assertEqual(jsonio.read_json(p), {"a": 1})
        self.assertEqual(len(calls), 3)

    def test_gives_up_and_cleans_tmp(self):
        def always(src, dst):
            raise PermissionError(5, "Access is denied")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(jsonio, "_windows", lambda: True), \
                mock.patch.object(jsonio.os, "replace", always), mock.patch.object(jsonio.time, "sleep", lambda s: None):
            with self.assertRaises(PermissionError):
                jsonio.write_json(Path(d) / "x.json", {})
            self.assertEqual(list(Path(d).iterdir()), [])


class TestReadTextAny(unittest.TestCase):
    def test_encodings(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "q.sql"
            p.write_bytes("SELECT 1 -- 한글\r\n".encode("utf-16"))
            self.assertEqual(jsonio.read_text_any(p), "SELECT 1 -- 한글\r\n")
            p.write_bytes(b"\xef\xbb\xbfx")
            self.assertEqual(jsonio.read_text_any(p), "x")
            p.write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
            self.assertIsNone(jsonio.read_text_any(p))
            self.assertIsNone(jsonio.read_text_any(Path(d) / "none"))


class TestSimulateNonUtf8(unittest.TestCase):
    def test_edit_on_utf16_and_binary(self):
        from types import SimpleNamespace
        from vmc_harness.gates import meta
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.sql").write_bytes("DELETE FROM t\r\n".encode("utf-16"))
            (Path(d) / "b.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            ctx = SimpleNamespace(root=d, tool_name="Edit", tool_input={"old_string": "DELETE", "new_string": "SELECT"})
            self.assertEqual(meta.simulate(ctx, "a.sql"), "SELECT FROM t\r\n")
            self.assertIsNone(meta.simulate(ctx, "b.png"))


if __name__ == "__main__":
    unittest.main()
