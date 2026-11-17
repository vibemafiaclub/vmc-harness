""""그냥 진행"의 단위 — (게이트, 파일, 작업) (spec 02 §"그냥 진행", 현장 결함 C9).

현장: 같은 기존 테스트 파일에 수정이 둘(래칫 값·주석 한 줄)이면 두 번 승인해야 했고, 게이트 단위 1회 허가를 다른 파일의
차단이 가져갔으며, 셸로 고친 테스트는 허가 뒤에도 실행 뒤 대조가 되돌렸다.
"""

from tests.gates_util import GatesCase
from tests.test_gates_opaque import OpaqueCase
from tests.test_gates_safety import TaskMixin

T = "tests/test_calc.py"
U = "tests/test_other.py"


def _edit(root, rel, old, new):
    return {"file_path": str(root / rel), "old_string": old, "new_string": new}


class TestFileBypass(TaskMixin, GatesCase):
    def setUp(self):
        super().setUp()
        self.write(T, "def test_a():\n    assert f() < 127  # docs/old.md\n")
        self.write(U, "def test_b():\n    assert 1\n")
        self.commit()
        self.hook("SessionStart", source="startup")
        self.set_task()

    def test_second_edit_same_file_same_task_passes(self):
        self.assertDenied(self.hook("PreToolUse", "Edit", _edit(self.root, T, "< 127", "< 107")), "검증물 변조")
        self.hook("UserPromptSubmit", prompt="그냥 진행")
        self.assertAllowed(self.hook("PreToolUse", "Edit", _edit(self.root, T, "< 127", "< 107")))
        # 같은 파일 두 번째 수정(주석)도 같은 작업 안이면 다시 묻지 않는다
        self.assertAllowed(self.hook("PreToolUse", "Edit", _edit(self.root, T, "docs/old.md", "docs/new.md")))

    def test_other_file_not_covered(self):
        self.assertDenied(self.hook("PreToolUse", "Edit", _edit(self.root, T, "< 127", "< 107")))
        self.hook("UserPromptSubmit", prompt="그냥 진행")
        self.assertDenied(self.hook("PreToolUse", "Edit", _edit(self.root, U, "assert 1", "assert 2")), "검증물 변조")
        # 다른 파일 차단이 허가를 가져가지 않았다
        self.assertAllowed(self.hook("PreToolUse", "Edit", _edit(self.root, T, "< 127", "< 107")))

    def test_other_task_not_covered(self):
        self.assertDenied(self.hook("PreToolUse", "Edit", _edit(self.root, T, "< 127", "< 107")))
        self.hook("UserPromptSubmit", prompt="그냥 진행")
        self.set_task(task_id="T-261023-bbbbbb")
        self.assertDenied(self.hook("PreToolUse", "Edit", _edit(self.root, T, "docs/old.md", "docs/new.md")))


class TestFileBypassOpaque(TaskMixin, OpaqueCase):
    def test_shell_edit_after_proceed_not_reverted(self):
        self.write(T, "def test_a():\n    assert f() < 127\n")
        self.commit()
        self.hook("SessionStart", source="startup")
        self.set_task()
        cmd = "python3 - <<'EOF'\np='%s'\nopen(p,'w').write(open(p).read().replace('127','107'))\nEOF" % T

        def effect():
            p = self.root / T
            p.write_text(p.read_text(encoding="utf-8").replace("127", "107"), encoding="utf-8")
        _, post = self.run_cmd(cmd, effect)
        self.assertIn("127", self.read(T))   # 허가 전 — 되돌림
        self.hook("UserPromptSubmit", prompt="그냥 진행")
        _, post = self.run_cmd(cmd, effect)
        self.assertIn("107", self.read(T))   # 허가 뒤 — 이 작업 동안 그 파일은 되돌리지 않는다
