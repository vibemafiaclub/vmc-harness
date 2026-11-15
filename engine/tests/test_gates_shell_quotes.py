"""셸 분석기 — 따옴표·명령 치환·변수·cd 오인 (spec 02 엔진 쓰기 보호·검증물 변조, 03 착수 선언).

현장 실측(0.1.11, 한 레포 6일): 착수 선언 차단 57건 중 15건과 검증물 변조 차단 1건이 레포에 쓰지 않는 명령이었다.
원인은 명령을 줄 단위로 자른 뒤 토큰으로 나눠서 —
- 따옴표 안 여러 줄 글자(커밋 메시지의 `> 인용`, `python3 -c "… '>=…' …"`)의 `>`를 리다이렉트로 읽음
- `d=$(mktemp -d)`·`D=~/x`처럼 같은 명령 안에서 정한 변수 경로(`$d/msg.txt`)를 레포 상대 경로로 읽음
- `cd 폴더;` 뒤(`;`)는 cd 실패를 가정해 원래 폴더 경로도 대상으로 봄 — 폴더가 지금 있으면 cd는 성공한다
- `rm -f *.png`의 `*`를 git pathspec이 하위 폴더까지 넘겨 다른 폴더의 테스트 그림을 변조 대상으로 봄
픽스처는 합성이다.
"""

import os
import subprocess
import tempfile

from tests.gates_util import GatesCase
from tests.test_gates_safety import TaskMixin
from vmc_harness.gates import shell

WITH_TASK = [("vmc_harness.gates.handlers", True), ("vmc_harness.task.handlers", False)]


class TestShellQuotes(GatesCase):
    def setUp(self):
        super().setUp()
        self.out = tempfile.mkdtemp(prefix="vmh-out-")   # 레포 밖 작업 폴더(영상·임시 파일 자리)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.out, ignore_errors=True)
        super().tearDown()

    def a(self, cmd):
        return shell.analyze(cmd, cwd=str(self.root))

    def bash(self, cmd):
        return self.hook("PreToolUse", "Bash", {"command": cmd}, modules=WITH_TASK)

    def test_quoted_multiline_gt_is_text(self):
        commit = 'git commit -q -m "feat: x\n\nuser prompt:\n> 이거 해줘\n>> 둘째 줄\n" -- a.py && git push -q'
        self.assertEqual(self.a(commit)["writes"], [])
        py = "python3 -c \"\nimport json\nev=[e for e in ev if e['at']>='2026-11-11T16:14']\nprint(len(ev) > 3)\n\""
        self.assertEqual(self.a(py)["writes"], [])
        ts = "npx tsx -e '\nimport { x } from \"./src\";\n(async()=>{const db = 1;\nconsole.log(s => [s.c, 2]);})()' 2>&1 | head -60"
        self.assertEqual(self.a(ts)["writes"], [])
        sub = "git commit -m \"$(cat <<'EOF'\nfix: y\n\n> 인용 > 이어서\nEOF\n)\""
        self.assertEqual(self.a(sub)["writes"], [])
        for cmd in (commit, py, ts, sub):
            self.assertAllowed(self.bash(cmd))

    def test_sed_script_with_spaces_and_hangul(self):
        cmd = "f=docs/README.md && sed -i '' 's/같은 대화에서 11\\/19(목) 13~15시 회의실/다른 문장(확정)/' \"$f\" && git add \"$f\""
        self.assertEqual(self.a(cmd)["writes"], ["docs/README.md"])
        self.assertDenied(self.bash(cmd), "착수 선언 없음")

    def test_same_command_variables(self):
        # 레포 밖(mktemp·홈 아래 폴더)
        self.assertEqual(self.a("d=$(mktemp -d); cat > $d/msg.txt <<'E'\n안녕 > 하세요\nE\npython3 send.py --text-file $d/msg.txt")["writes"], [])
        self.assertEqual(self.a("D=%s/v; mkdir -p $D/_work; python3 x.py > $D/_work/c.json" % self.out)["writes"],
                         ["%s/v/_work" % self.out, "%s/v/_work/c.json" % self.out])
        # 레포 안(변수로 정한 상대 경로·for 목록)
        self.assertEqual(self.a("B=data/issued; cp /tmp/s.pdf $B/out.pdf")["writes"], ["data/issued/out.pdf"])
        self.assertEqual(self.a("for f in a.txt b.txt; do echo x > $f; done")["writes"], ["a.txt", "b.txt"])
        self.assertEqual(self.a('T=note.md\n{ cat <<\'EOF\'\n# 제목\nEOF\n} > "$T"')["writes"], ["note.md"])
        self.assertDenied(self.bash("B=data/issued; cp /tmp/s.pdf $B/out.pdf"), "착수 선언 없음")
        self.assertAllowed(self.bash("d=$(mktemp -d); cat > $d/msg.txt <<'E'\nhi\nE\nwc -l $d/msg.txt"))

    def test_unknown_variable_is_opaque_not_repo_write(self):
        a = self.a("echo hi > $VMH_UNSET_X/a.txt; cp a b \"$VMH_UNSET_Y\"")
        self.assertEqual(a["writes"], [])
        self.assertIn("(var)", a["opaque_cmds"])
        self.assertAllowed(self.bash("mkdir -p $VMH_UNSET_W && ffmpeg -i in.mov $VMH_UNSET_W/a.wav > $VMH_UNSET_W/log.txt"))
        # 폴더를 아는 상대 경로(`out_$(date).txt`)는 레포 쓰기로 남는다
        self.assertEqual(self.a("echo x > out_$(date +%s).txt")["writes"], ["out_$(date +%s).txt"])

    def test_cd_followed_after_assignment_and_loop(self):
        cmd = "PY=$(head -1 $(which tool) | sed 's/^#!//'); echo $PY; cd %s; mkdir -p _work; " \
              "for t in 1 2; do ffmpeg -ss $t -i 0.mov _work/p$t.png; done; ls _work > _work/list.txt" % self.out
        self.assertEqual(self.a(cmd)["writes"], ["%s/_work" % self.out, "%s/_work/list.txt" % self.out])
        self.assertAllowed(self.bash(cmd))
        # 지금 없는 폴더로의 `;` cd는 실패할 수 있다 — 원래 폴더 경로도 대상(기존 동작)
        self.assertIn("_work", self.a("cd %s/none-yet; mkdir -p _work" % self.out)["writes"])

    def test_safety_gate_still_sees_variable_ledger_write(self):
        out = self.bash("L=.harness/ledger; echo x > $L/x.json")
        self.assertDenied(out)
        self.assertNotIn("착수 선언", (out.get("hookSpecificOutput") or {}).get("permissionDecisionReason", ""))


class TestTamperGlobDelete(TaskMixin, GatesCase):
    def test_glob_delete_stays_in_its_folder(self):
        self.write("assets/tests/sample.png", "png")
        self.write("app/x.ts", "export const a = 1\n")
        self.commit()
        self.set_task()
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        # 레포 밖 작업 폴더의 그림을 지우는 명령(cd 대상이 변수) — 다른 폴더 테스트 그림은 변조 대상이 아니다
        out = self.hook("PreToolUse", "Bash", {"command": "W=%s; cd $W/slides && rm -f *.png" % tempfile.gettempdir()})
        self.assertAllowed(out)
        out = self.hook("PreToolUse", "Bash", {"command": "cd $VMH_UNSET_Q && rm -f *.png"})
        self.assertAllowed(out)
        self.assertDenied(self.hook("PreToolUse", "Bash", {"command": "rm -f assets/tests/*.png"}), "검증물 변조")
