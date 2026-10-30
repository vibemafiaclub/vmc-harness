"""dist — activate·update·lock·server.call 흐름을 가짜 API 서버(같은 프로세스, apps/api와 같은 응답 모양)로 확인한다."""

import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tests.test_dist_shell import DistTestCase, sha_file, synthetic_zip, test_key

from vmc_harness.dist import lock, server, shell, updater


class FakeApi:
    """apps/api /harness/* 의 최소 흉내. 키 하나, 릴리스 목록, 잠금 표."""

    def __init__(self, key):
        self.key_hash = hashlib.sha256(key.encode()).hexdigest()
        self.revoked = False
        self.releases = []  # [(version, path)]
        self.locks = {}
        self.calls = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _auth(self):
                h = self.headers.get("Authorization", "")
                k = h[7:] if h.startswith("Bearer ") else ""
                if hashlib.sha256(k.encode()).hexdigest() != fake.key_hash:
                    return "unknown_key"
                return "revoked" if fake.revoked else None

            def _send(self, code, data=None, raw=None, ctype="application/json"):
                body = raw if raw is not None else json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def _handle(self):
                fake.calls.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}))
                bad = self._auth()
                if bad:
                    return self._send(401, {"error": "unauthorized", "reason": bad})
                if self.command == "POST" and self.path == "/harness/activate":
                    b = self._body()
                    if not b.get("pc_id"):
                        return self._send(400, {"error": "pc_id required"})
                    return self._send(200, {"license_id": "lic-1", "org": "테스트사", "expires_at": None})
                if self.command == "GET" and self.path == "/harness/releases/latest":
                    if not fake.releases:
                        return self._send(404, {"error": "no release"})
                    v, p = fake.releases[-1]
                    return self._send(200, {"version": v, "sha256": sha_file(p), "size": p.stat().st_size, "notes": None})
                if self.command == "GET" and self.path.startswith("/harness/releases/") and self.path.endswith("/download"):
                    v = self.path.split("/")[3]
                    for rv, p in fake.releases:
                        if rv == v:
                            return self._send(200, raw=p.read_bytes(), ctype="application/zip")
                    return self._send(404, {"error": "no such version"})
                if self.path == "/harness/lock":
                    b = self._body()
                    cur = fake.locks.get(b["project_hash"])
                    if self.command == "POST":
                        if cur and cur != b["pc_id"]:
                            return self._send(200, {"granted": False, "holder": cur, "expires_at": "2099-01-01T00:00:00Z"})
                        fake.locks[b["project_hash"]] = b["pc_id"]
                        return self._send(200, {"granted": True, "holder": b["pc_id"], "expires_at": "2099-01-01T00:00:00Z"})
                    if self.command == "DELETE":
                        ok = cur == b["pc_id"]
                        if ok:
                            del fake.locks[b["project_hash"]]
                        return self._send(200, {"released": ok})
                return self._send(404, {"error": "not found"})

            do_GET = do_POST = do_DELETE = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FlowTest(DistTestCase):
    def setUp(self):
        super().setUp()
        self.key = test_key()
        self.api = FakeApi(self.key)
        os.environ["VMC_HARNESS_SERVER"] = self.api.url
        self.api.releases.append(("0.1.0", synthetic_zip(self.out / "r010.zip", "0.1.0")))

    def tearDown(self):
        self.api.close()
        super().tearDown()

    def test_activate_online_installs_kernel_and_stores_key(self):
        msgs = []
        self.assertEqual(shell.activate(self.key, out=msgs.append), 0, msgs)
        self.assertEqual(shell.current_version(), "0.1.0")
        lic = shell.read_license()
        self.assertEqual((lic["license_id"], lic["org"], lic["mode"]), ("lic-1", "테스트사", "online"))
        self.assertEqual(shell.load_key(), self.key)
        self.assertNotIn(self.key, json.dumps(lic))
        dl = [c for c in self.api.calls if c[1].endswith("/download")][0]
        self.assertEqual(dl[2].get("x-vmc-pc-id"), shell.pc_id())

    def test_activate_rejected_key(self):
        msgs = []
        self.assertEqual(shell.activate(test_key(), out=msgs.append), 2)
        self.assertIsNone(shell.read_license())
        self.assertIsNone(shell.current_version())

    def test_activate_unreachable_suggests_offline(self):
        os.environ["VMC_HARNESS_SERVER"] = "http://127.0.0.1:9"
        msgs = []
        self.assertEqual(shell.activate(self.key, out=msgs.append), 3)
        self.assertIn("--offline", msgs[0])

    def test_update_new_version_then_contract_end_keeps_working(self):
        self.assertEqual(shell.activate(self.key, out=lambda m: None), 0)
        self.assertEqual(updater.run()["status"], "current")
        self.api.releases.append(("0.1.1", synthetic_zip(self.out / "r011.zip", "0.1.1")))
        r = updater.run()
        self.assertEqual((r["status"], r["version"], r["from"]), ("updated", "0.1.1", "0.1.0"))
        self.assertEqual(shell.current_version(), "0.1.1")
        # 계약 종료(회수) → 업데이트만 끊기고 지금 버전 그대로
        self.api.revoked = True
        self.api.releases.append(("0.1.2", synthetic_zip(self.out / "r012.zip", "0.1.2")))
        self.assertEqual(updater.run()["status"], "unauthorized")
        self.assertEqual(shell.current_version(), "0.1.1")
        # 서버에 못 닿아도 그대로
        os.environ["VMC_HARNESS_SERVER"] = "http://127.0.0.1:9"
        self.assertEqual(updater.run()["status"], "unreachable")
        self.assertEqual(shell.current_version(), "0.1.1")
        self.assertFalse(updater.due())  # 방금 확인했다

    def test_update_rejects_corrupted_download(self):
        self.assertEqual(shell.activate(self.key, out=lambda m: None), 0)
        p = synthetic_zip(self.out / "r020.zip", "0.2.0")
        self.api.releases.append(("0.2.0", p))
        real_api = shell.api  # 내려받은 파일이 latest가 알려준 sha256과 다른 경우

        def corrupt(method, path, **kw):
            st, data = real_api(method, path, **kw)
            if path.endswith("/latest"):
                data = dict(data, sha256="0" * 64)
            return st, data
        with mock.patch.object(shell, "api", side_effect=corrupt):
            r = shell.update_now(key=self.key)
        self.assertEqual(r["status"], "error")
        self.assertEqual(shell.current_version(), "0.1.0")

    def test_server_call_without_license(self):
        self.assertEqual(server.call("GET", "/harness/releases/latest"), (0, {"error": "no_license"}))
        self.assertEqual(shell.activate(self.key, out=lambda m: None), 0)
        st, data = server.call("GET", "/harness/releases/latest")
        self.assertEqual((st, data["version"]), (200, "0.1.0"))

    def test_lock_server_then_other_pc_blocked_then_release(self):
        self.assertEqual(shell.activate(self.key, out=lambda m: None), 0)
        self.assertTrue(lock.acquire(self.root, 600))
        ph = lock.project_hash(self.root)
        self.assertEqual(self.api.locks[ph], shell.pc_id())
        self.assertFalse(lock.acquire(self.root, 600))  # 같은 PC의 두 번째 루프 — 로컬 파일이 막는다
        lock.release(self.root)
        self.assertNotIn(ph, self.api.locks)
        self.api.locks[ph] = "other-pc"
        self.assertFalse(lock.acquire(self.root, 600))
        self.assertFalse((Path(lock._file(self.root))).exists())

    def test_lock_falls_back_to_local_when_unreachable(self):
        os.environ["VMC_HARNESS_SERVER"] = "http://127.0.0.1:9"
        self.assertTrue(lock.acquire(self.root, 600))
        self.assertEqual(json.loads(lock._file(self.root).read_text(encoding="utf-8"))["mode"], "local")
        self.assertFalse(lock.acquire(self.root, 600))
        lock.release(self.root)
        self.assertTrue(lock.acquire(self.root, 600))
        lock.release(self.root)

    def test_auto_off_skips_session_start_but_manual_update_works(self):
        import io
        from contextlib import redirect_stdout
        from vmc_harness import cli
        from vmc_harness.dist import handlers
        self.assertEqual(shell.activate(self.key, out=lambda m: None), 0)
        os.environ.pop("VMC_HARNESS_NESTED", None)
        self.assertTrue(updater.auto_enabled())  # 기본 update.auto = true
        n_calls = len(self.api.calls)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["update", "--auto", "off"]), 0)
        self.assertIn("자동 업데이트 꺼짐", buf.getvalue())
        self.assertFalse(updater.auto_enabled())
        self.assertEqual(len(self.api.calls), n_calls)  # --auto는 업데이트하지 않는다
        updater.state_file().write_text('{"auto": false}', encoding="utf-8")  # 확인 기록 없음 = due
        self.assertTrue(updater.due())
        with mock.patch("vmc_harness.core.proc.spawn_detached") as sp:
            handlers.on_session_start(None)
        self.assertEqual(sp.call_count, 0)
        # 수동 update는 그대로
        self.api.releases.append(("0.1.1", synthetic_zip(self.out / "r011.zip", "0.1.1")))
        self.assertEqual(updater.run()["status"], "updated")
        self.assertFalse(updater.auto_enabled())  # run()이 덮어쓰기를 지우지 않는다
        # 다시 켜면 띄운다
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["update", "--auto", "on"]), 0)
        st = json.loads(updater.state_file().read_text(encoding="utf-8"))
        st.pop("checked_at")
        updater.state_file().write_text(json.dumps(st), encoding="utf-8")
        with mock.patch("vmc_harness.core.proc.spawn_detached") as sp:
            handlers.on_session_start(None)
        self.assertEqual(sp.call_count, 1)
        os.environ["VMC_HARNESS_NESTED"] = "1"

    def test_auto_default_comes_from_config(self):
        off = lambda k: False if k == "update.auto" else None
        with mock.patch("vmc_harness.dist.settings.get", side_effect=off):
            self.assertFalse(updater.auto_enabled())
        updater.set_auto(True)  # PC 덮어쓰기가 설정보다 우선
        with mock.patch("vmc_harness.dist.settings.get", side_effect=off):
            self.assertTrue(updater.auto_enabled())

    def test_session_start_hook_spawns_once_a_day(self):
        from vmc_harness.dist import handlers
        self.assertEqual(shell.activate(self.key, out=lambda m: None), 0)
        os.environ.pop("VMC_HARNESS_NESTED", None)
        updater.state_file().unlink() if updater.state_file().exists() else None
        with mock.patch("vmc_harness.core.proc.spawn_detached") as sp:
            self.assertIsNone(handlers.on_session_start(None))
            self.assertIsNone(handlers.on_session_start(None))
        self.assertEqual(sp.call_count, 1)
        args = sp.call_args[0][0]
        self.assertEqual(args[1:], ["-m", "vmc_harness", "update", "--quiet"])
        os.environ["VMC_HARNESS_NESTED"] = "1"
        updater.state_file().unlink()
        with mock.patch("vmc_harness.core.proc.spawn_detached") as sp:
            handlers.on_session_start(None)
        self.assertEqual(sp.call_count, 0)
