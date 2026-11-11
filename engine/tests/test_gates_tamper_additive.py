"""검증물 변조 — 기존 테스트 파일에 줄을 더하기만 하는 변경은 통과, 기존 줄 변경·삭제·건너뛰기 표시 추가는 계속 막음 (spec 02).

현장 결함: 검증물 변조 차단 6건 중 5건이 기존 테스트 파일에 새 테스트를 더하려던 것이었고, 에이전트는 새 파일로 쪼개 우회했다.
"""


from tests.gates_util import GatesCase
from tests.test_gates_safety import TaskMixin

PY = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 1)\n"
JS = "describe('calc', () => {\n  it('adds', () => {\n    expect(1 + 1).toBe(2)\n  })\n})\n"


class TestTamperAdditive(TaskMixin, GatesCase):
    def setUp(self):
        super().setUp()
        self.write("tests/test_calc.py", PY)
        self.write("web/calc.spec.ts", JS)
        self.commit()
        self.set_task()

    def edit(self, rel, old, new, tool="Edit"):
        return self.hook("PreToolUse", tool, {"file_path": str(self.root / rel), "old_string": old, "new_string": new})

    def write_all(self, rel, content):
        return self.hook("PreToolUse", "Write", {"file_path": str(self.root / rel), "content": content})

    def test_edit_adding_test_allowed(self):
        last = "        self.assertEqual(1, 1)\n"
        self.assertAllowed(self.edit("tests/test_calc.py", last, last + "\n    def test_b(self):\n        self.assertEqual(2, 2)\n"))
        # 중간에 끼워 넣기도 기존 줄을 다 남기면 더하기
        self.assertAllowed(self.edit("tests/test_calc.py", "    def test_a(self):",
                                     "    def test_z(self):\n        self.assertTrue(True)\n\n    def test_a(self):"))

    def test_edit_changing_or_skipping_blocked(self):
        self.assertDenied(self.edit("tests/test_calc.py", "assertEqual(1, 1)", "assertEqual(1, 2)"), "검증물 변조")
        self.assertDenied(self.edit("tests/test_calc.py", "    def test_a(self):",
                                    "    @unittest.skip('later')\n    def test_a(self):"), "검증물 변조")
        self.assertDenied(self.edit("web/calc.spec.ts", "  it('adds'", "  it.only('mul', () => {})\n  it('adds'"))

    def test_write_keeps_all_old_lines(self):
        self.assertAllowed(self.write_all("tests/test_calc.py", PY + "\n    def test_b(self):\n        model.fit(x)\n        sys.exit(0)\n"))
        self.assertDenied(self.write_all("tests/test_calc.py", PY.replace("        self.assertEqual(1, 1)\n", "")))
        self.assertDenied(self.write_all("web/calc.spec.ts", JS + "xit('later', () => {})\n"))
        self.assertAllowed(self.write_all("web/calc.spec.ts", JS + "describe('mul', () => {\n  it('x', () => {})\n})\n"))

    def test_multiedit_and_codex_patch(self):
        out = self.hook("PreToolUse", "MultiEdit", {"file_path": str(self.root / "web/calc.spec.ts"), "edits": [
            {"old_string": "describe('calc', () => {\n", "new_string": "describe('calc', () => {\n  it('subs', () => {})\n"}]})
        self.assertAllowed(out)
        add = "*** Begin Patch\n*** Update File: web/calc.spec.ts\n@@\n })\n+describe('mul', () => {})\n*** End Patch"
        self.assertAllowed(self.hook("PreToolUse", "apply_patch", {"command": add}, agent="codex"))
        change = "*** Begin Patch\n*** Update File: web/calc.spec.ts\n@@\n-    expect(1 + 1).toBe(2)\n+    expect(1 + 1).toBe(3)\n*** End Patch"
        self.assertDenied(self.hook("PreToolUse", "apply_patch", {"command": change}, agent="codex"))
        skip = "*** Begin Patch\n*** Update File: web/calc.spec.ts\n@@\n })\n+describe.skip('mul', () => {})\n*** End Patch"
        self.assertDenied(self.hook("PreToolUse", "apply_patch", {"command": skip}, agent="codex"))

    def test_parsed_shell_write_still_blocked(self):
        self.assertDenied(self.hook("PreToolUse", "Bash", {"command": "cat >> tests/test_calc.py <<EOF\n    def test_b(self): pass\nEOF"}))

    def test_opaque_shell_append_allowed_rewrite_reverted(self):
        p = self.root / "tests/test_calc.py"
        cmd = "python3 add_test.py"
        self.assertAllowed(self.hook("PreToolUse", "Bash", {"command": cmd}, tool_use_id="t1"))
        p.write_text(PY + "\n    def test_b(self):\n        self.assertEqual(2, 2)\n", encoding="utf-8")
        self.assertAllowed(self.hook("PostToolUse", "Bash", {"command": cmd}, tool_use_id="t1"))
        self.assertIn("test_b", p.read_text(encoding="utf-8"))
        before = p.read_bytes()
        self.assertAllowed(self.hook("PreToolUse", "Bash", {"command": cmd}, tool_use_id="t2"))
        p.write_text(PY.replace("(1, 1)", "(1, 2)"), encoding="utf-8")
        post = self.hook("PostToolUse", "Bash", {"command": cmd}, tool_use_id="t2")
        self.assertEqual(post.get("decision"), "block", post)
        self.assertEqual(p.read_bytes(), before)

    def test_expected_files_not_additive_exception(self):
        self.write("tests/__snapshots__/calc.snap", "a\n")
        self.commit("snap")
        out = self.hook("PreToolUse", "Write", {"file_path": str(self.root / "tests/__snapshots__/calc.snap"), "content": "a\nb\n"})
        self.assertDenied(out)


class TestTamperAdditiveCRLF(TestTamperAdditive):
    """Windows 작업 트리(CRLF)에 LF로 된 old_string을 보내도 더하기 판정이 같아야 한다(CI windows 실패 재현)."""

    def setUp(self):
        super().setUp()
        for rel, body in (("tests/test_calc.py", PY), ("web/calc.spec.ts", JS)):
            (self.root / rel).write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
        self.commit("crlf")
