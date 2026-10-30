"""해석 못 한 셸 명령(opaque)의 실행 뒤 대조·되돌림 (spec 02 엔진 쓰기 보호·보호 영역·경로형 생성 게이트).

고객 재현(합성): 읽기만 하는 python 명령에 `.harness/ledger` 문자열이 있어 막힘(오탐) /
heredoc python으로 보호 영역 파일을 고쳐도 통과(놓침). 명령 실행은 훅 사이에 파이썬으로 같은 효과를 내서 흉내 낸다.
"""

import subprocess
from unittest import mock

from tests.gates_util import GatesCase
from vmc_harness.core import events, ledger
from vmc_harness.core.jsonio import write_json
from vmc_harness.gates import generated, opaque_watch

READ_LEDGER = ("python3 - <<'EOF'\nimport json, glob\nfor f in glob.glob('.harness/ledger/**/*.json', recursive=True):\n"
               "    d = json.load(open(f))\n    print(f, d.get('kind'), '=>', len(d))\nEOF")
EDIT_AUTH = "python3 - <<'EOF'\np = 'auth/login.py'\ns = open(p).read()\nopen(p, 'w').write(s.replace('True', 'False'))\nEOF"


class OpaqueCase(GatesCase):
    def setUp(self):
        super().setUp()
        write_json(self.root / ".harness/protected.json", {"paths": [{"glob": "auth/**", "kind": "auth"}]})
        self.write("auth/login.py", "ALLOW = True\n")
        self.write("src/app.py", "x = 1\n")
        write_json(self.root / ".harness/ledger/findings/F-1.json", {"finding_id": "F-1", "kind": "false_block"})
        self.commit()
        self.n = 0

    def run_cmd(self, cmd, effect=None, failed=False):
        """PreToolUse → (명령 효과) → PostToolUse(실패면 PostToolUseFailure). (pre 출력, post 출력)"""
        self.n += 1
        extra = {"tool_use_id": "toolu_%d" % self.n}
        pre = self.hook("PreToolUse", "Bash", {"command": cmd}, **extra)
        if (pre.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny":
            return pre, None
        if effect:
            effect()
        post = self.hook("PostToolUseFailure" if failed else "PostToolUse", "Bash", {"command": cmd}, **extra)
        return pre, post

    def read(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")


class TestOpaqueLedger(OpaqueCase):
    def test_read_only_python_with_ledger_string_passes(self):
        pre, post = self.run_cmd(READ_LEDGER)
        self.assertAllowed(pre)
        self.assertEqual(post, {})
        # 대조 기록은 PostToolUse가 가져가고 지운다
        self.assertEqual(list((self.root / ".harness/cache/gates/opaque").glob("*.json")), [])

    def test_python_heredoc_ledger_write_reverted(self):
        p = self.root / ".harness/ledger/findings/F-1.json"
        before = p.read_bytes()

        def effect():
            p.write_text('{"finding_id": "F-1", "kind": "forged"}\n', encoding="utf-8")
            (self.root / ".harness/ledger/findings/F-2.json").write_text("{}", encoding="utf-8")
        pre, post = self.run_cmd("python3 - <<'EOF'\nimport tool\ntool.fix()\nEOF", effect)
        self.assertAllowed(pre)
        self.assertEqual(post.get("decision"), "block", post)
        self.assertIn("엔진 쓰기 보호", post["reason"])
        self.assertIn("되돌림", post["reason"])
        self.assertEqual(p.read_bytes(), before)
        self.assertFalse((self.root / ".harness/ledger/findings/F-2.json").exists())
        ev = events.read(self.root, "opaque_reverted")
        self.assertEqual(ev[-1]["gate"], "engine_write_guard")
        blocks = events.read(self.root, "gate_block")
        self.assertEqual(blocks[-1]["event"], "PostToolUse")

    def test_uncommitted_ledger_file_restored_from_copy(self):
        p = self.root / ".harness/ledger/tasks/2026-10/pc.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"task_id":"T-1"}\n', encoding="utf-8")   # 미추적(커밋 전) 장부
        pre, post = self.run_cmd("node scripts/fix.js", lambda: p.write_text("", encoding="utf-8"))
        self.assertEqual(post.get("decision"), "block")
        self.assertEqual(p.read_text(encoding="utf-8"), '{"task_id":"T-1"}\n')

    def test_engine_write_during_command_kept(self):
        def effect():
            ledger.create_finding(self.root, "false_block", {"gate_id": "kernel:tamper"})
        pre, post = self.run_cmd("python3 run_checks.py", effect)
        self.assertEqual(post, {})
        self.assertEqual(len(ledger.list_findings(self.root)), 2)

    def test_failed_command_still_reverted(self):
        p = self.root / ".harness/ledger/findings/F-1.json"
        before = p.read_bytes()
        pre, post = self.run_cmd("python3 broken.py", lambda: p.write_text("{}", encoding="utf-8"), failed=True)
        self.assertEqual(post, {})   # PostToolUseFailure는 출력하지 않는다(형식 미실측) — 되돌리고 기록만
        self.assertEqual(p.read_bytes(), before)
        self.assertEqual(events.read(self.root, "opaque_reverted")[-1]["gate"], "engine_write_guard")

    def test_forged_evidence_removed(self):
        ev = self.root / ".harness/cache/evidence/T-1/x.marker.json"

        def effect():
            ev.parent.mkdir(parents=True, exist_ok=True)
            ev.write_text('{"kind": "test_run"}', encoding="utf-8")
        pre, post = self.run_cmd("python3 -c \"import mk; mk.marker()\"", effect)
        self.assertEqual(post.get("decision"), "block")
        self.assertFalse(ev.exists())

    def test_windows_path_string_not_preblocked(self):
        cmd = "py -c \"print(open('.harness\\\\ledger\\\\findings\\\\F-1.json').read())\""
        pre, post = self.run_cmd(cmd)
        self.assertAllowed(pre)
        self.assertEqual(post, {})

    def test_git_only_opaque_not_watched(self):
        pre, post = self.run_cmd("git stash")
        self.assertAllowed(pre)
        self.assertEqual(list((self.root / ".harness/cache/gates/opaque").glob("*.json")), [])

    def test_snapshot_failure_falls_back_to_preblock(self):
        with mock.patch.object(opaque_watch, "_git_list", lambda root, specs: None):
            pre, post = self.run_cmd(READ_LEDGER)
        self.assertDenied(pre, "엔진 쓰기 보호")

    def test_kernel_path_still_preblocked(self):
        pre, _ = self.run_cmd("python3 -c \"open('~/.vmc-harness/kernel/0.1.4/x.py','w')\"")
        self.assertDenied(pre, "엔진 쓰기 보호")


class TestOpaqueProtected(OpaqueCase):
    def set_task(self, **kw):
        t = {"task_id": "T-261030-aaaaaa", "session_id": self.session, "size": "mid",
             "started_at": "2000-01-01T00:00:00+00:00", "declare": {"scope": [], "protected_touch": False, "test_change": None}}
        t["declare"].update(kw.pop("declare", {}))
        self.task = t
        p = mock.patch("vmc_harness.gates.state.current_task", lambda root, sid: self.task)
        p.start()
        self.addCleanup(p.stop)
        return t

    def edit_auth(self):
        p = self.root / "auth/login.py"
        p.write_text(p.read_text(encoding="utf-8").replace("True", "False") + "# x\n", encoding="utf-8")

    def test_heredoc_python_edit_of_protected_reverted_and_card(self):
        self.set_task()
        pre, post = self.run_cmd(EDIT_AUTH, self.edit_auth)
        self.assertAllowed(pre)
        self.assertEqual(post.get("decision"), "block", post)
        self.assertIn("보호 영역", post["reason"])
        self.assertEqual(self.read("auth/login.py"), "ALLOW = True\n")
        aps = ledger.list_approvals(self.root)
        self.assertEqual(len(aps), 1)
        self.assertIn("auth/login.py", aps[0]["detail"]["paths"])
        # 승인되면 같은 명령이 통과한다
        ledger.answer_approval(self.root, aps[0]["approval_id"], "approve")
        pre, post = self.run_cmd(EDIT_AUTH, self.edit_auth)
        self.assertEqual(post, {})
        self.assertIn("False", self.read("auth/login.py"))

    def test_dirty_protected_restored_to_pre_command_content(self):
        self.set_task()
        (self.root / "auth/login.py").write_text("ALLOW = True  # 사람이 고치던 중\n", encoding="utf-8")
        pre, post = self.run_cmd("python3 tools/codemod.py", self.edit_auth)
        self.assertEqual(post.get("decision"), "block")
        self.assertEqual(self.read("auth/login.py"), "ALLOW = True  # 사람이 고치던 중\n")

    def test_new_file_in_protected_removed_and_other_files_untouched(self):
        self.set_task()

        def effect():
            self.write("auth/backdoor.py", "x\n")
            self.write("src/app.py", "x = 2\n")
        pre, post = self.run_cmd("make gen", effect)
        self.assertEqual(post.get("decision"), "block")
        self.assertFalse((self.root / "auth/backdoor.py").exists())
        self.assertEqual(self.read("src/app.py"), "x = 2\n")   # 보호 영역 밖은 대조하지 않는다

    def test_touch_without_content_change_passes(self):
        import os
        import time
        self.set_task()
        p = self.root / "auth/login.py"
        pre, post = self.run_cmd("python3 lint.py", lambda: os.utime(str(p), (time.time() + 5, time.time() + 5)))
        self.assertEqual(post, {})

    def test_crlf_protected_file_restored_bytes(self):
        self.set_task()
        p = self.root / "auth/win.py"
        p.write_bytes(b"A = 1\r\nB = 2\r\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "crlf"], cwd=self.root, check=True)
        before = p.read_bytes()
        pre, post = self.run_cmd("powershell -File fix.ps1", lambda: p.write_bytes(b"A = 9\n"))
        self.assertEqual(post.get("decision"), "block")
        self.assertEqual(p.read_bytes(), before)


GATE_BODY = '''def check(api):
    for rel in api.paths:
        return "%s 자동 생성 파일 직접 수정" % rel
'''
BASIS = {"type": "explicit_request", "quote": "생성 파일은 손으로 고치지 마",
         "source": {"session": "s-1", "date": "2026-10-23", "area": "gen/"}}


class TestOpaqueGenerated(OpaqueCase):
    def test_path_gate_fires_on_opaque_change(self):
        self.write("gen/api.ts", "export const a = 1\n")
        self.commit()
        generated.create(self.root, title="생성 파일 보호", basis=BASIS,
                         trigger={"paths": ["gen/**"], "events": ["PreToolUse"]}, script_text=GATE_BODY, targets=["gen/api.ts"])
        mods = [("vmc_harness.gates.handlers", True), ("vmc_harness.gates.generated", False)]
        extra = {"tool_use_id": "toolu_gen"}
        cmd = "node scripts/patch.js"
        self.assertAllowed(self.hook("PreToolUse", "Bash", {"command": cmd}, modules=mods, **extra))
        self.write("gen/api.ts", "export const a = 2\n")
        post = self.hook("PostToolUse", "Bash", {"command": cmd}, modules=mods, **extra)
        self.assertEqual(post.get("decision"), "block", post)
        self.assertIn("생성 파일 보호", post["reason"])
        self.assertEqual(self.read("gen/api.ts"), "export const a = 1\n")


class TestOpaqueTamper(OpaqueCase):
    """검증물 변조 금지도 문자열 짐작 대신 실행 뒤 대조(현장 결함 C3·C4와 같은 부류)."""

    def setUp(self):
        super().setUp()
        self.write("tests/test_app.py", "def test_x():\n    assert 1\n")
        self.commit()

    def test_heredoc_edit_of_existing_test_reverted(self):
        p = self.root / "tests/test_app.py"
        before = p.read_bytes()
        pre, post = self.run_cmd("python3 - <<'EOF'\nimport tool\ntool.patch()\nEOF",
                                 lambda: p.write_text("def test_x():\n    pass\n", encoding="utf-8"))
        self.assertAllowed(pre)
        self.assertEqual(post.get("decision"), "block", post)
        self.assertIn("검증물 변조", post["reason"])
        self.assertEqual(p.read_bytes(), before)

    def test_mentioning_test_path_without_change_passes(self):
        pre, post = self.run_cmd("python3 -m pytest tests/test_app.py -q")
        self.assertAllowed(pre)
        self.assertEqual(post, {})

    def test_new_test_file_by_script_passes(self):
        q = self.root / "tests/test_new.py"
        pre, post = self.run_cmd("node scripts/gen.js", lambda: q.write_text("def test_y():\n    pass\n", encoding="utf-8"))
        self.assertEqual(post, {})
        self.assertTrue(q.exists())
