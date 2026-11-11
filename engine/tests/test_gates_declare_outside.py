"""착수 선언 — 쓰는 대상이 모두 프로젝트 밖이면 선언을 요구하지 않는다 (spec 02 §훅 디스패처·03).

현장 결함: 선언 차단 29건 중 12건이 /tmp만 건드리는 명령이었다(`cd /tmp/x && sed -i '' … a.py`, `cat > /tmp/f.ts <<EOF`).
셸 분석기가 같은 명령 안의 cd를 따라 상대 경로를 풀고, BSD `sed -i ''`의 빈 인자를 파일로 읽지 않는다.
"""

from tests.gates_util import GatesCase
from vmc_harness.gates import shell

WITH_TASK = [("vmc_harness.gates.handlers", True), ("vmc_harness.task.handlers", False)]


class TestDeclareOutside(GatesCase):
    def bash(self, cmd):
        return self.hook("PreToolUse", "Bash", {"command": cmd}, modules=WITH_TASK)

    def test_outside_only_needs_no_declare(self):
        for cmd in ("cd /tmp/vh-x && sed -i '' 's/a/b/' a.py",
                    "cat > /tmp/vh-f.ts <<EOF\nconst a = 1 > 0\nEOF",
                    "mkdir -p /tmp/vh-y && cd /tmp/vh-y && echo hi > out.txt && rm -f old.txt",
                    "pushd /tmp && cp a b && popd"):
            self.assertAllowed(self.bash(cmd))

    def test_inside_still_needs_declare(self):
        for cmd in ("sed -i '' 's/a/b/' a.py",
                    "cd /tmp && echo x > /tmp/a && cd - && echo y > b.txt",     # cd - : 어디인지 모름 → 원래 경로로 본다
                    "(cd /tmp && touch a) && touch b.txt",                     # 괄호 밖은 원래 폴더
                    "cd /tmp/vh-nope; rm -f a.py",                             # ; 뒤는 cd 실패 시 원래 폴더에서 돈다
                    "cd /tmp/vh-x && echo y > %s/b.txt" % self.root):
            self.assertDenied(self.bash(cmd), "착수 선언 없음")

    def test_safety_gate_not_weakened_by_unsure_cd(self):
        out = self.bash("cd /tmp/vh-nope; rm -rf .harness/ledger")
        self.assertDenied(out)
        self.assertNotIn("착수 선언", (out.get("hookSpecificOutput") or {}).get("permissionDecisionReason", ""))

    def test_bsd_sed_empty_suffix(self):
        self.assertEqual(shell.analyze("sed -i '' 's/a/b/' a.py")["writes"], ["a.py"])
        self.assertEqual(shell.analyze("cd sub && sed -i '' 's/a/b/' a.py")["writes"], ["sub/a.py"])
