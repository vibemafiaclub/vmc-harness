"""실행 뒤 대조의 "이 명령이 쓴 파일" 귀속 — 이 세션이 썼다는 근거가 시간 창뿐인 파일은 빼기 (spec 02·03 §이번 작업이 바꾼 것).

현장 실측(0.1.11, 한 레포 6일): 완료·과설계 차단 75건 중 20건이 다른 세션 파일 때문이었다. 긴 셸 명령(영상 렌더·전사·분석) 동안
- 다른 세션의 해석 못 한 셸 명령·렌더 작업이 쓴 파일(그 세션 작업의 선언 범위·편집 기록에 있음)
- 같은 명령 안 git 동기화(pull·rebase·stash pop)가 되살린 파일
이 실행 시간 창 안에 수정됐다는 이유만으로 이 작업 편집이 됐다. 픽스처는 합성이다.
"""

import subprocess

from vmc_harness.task import declare as dec, scope, state as st

from tests.test_gates_opaque import OpaqueCase

SCRIPT = "python3 - <<'EOF'\nopen('web/pages/a.tsx','w').write('x')\nEOF"


def _raw(scope_):
    return {"intent": "x", "scope": scope_, "predict": {"tokens": [1, 2, 3], "agent_sec": [60, 120, 170]}}


class OtherSessionClaimTest(OpaqueCase):
    def setUp(self):
        super().setUp()
        self.write("web/pages/a.tsx", "export const A = 1\n")
        self.write("lessons/l9/drops.txt", "0\n")
        self.write("tools/out.json", "{}\n")
        self.commit("base")
        st.session_touch(self.root, self.session, "claude")
        dec.declare(self.root, _raw(["web/pages/"]), session_id=self.session)

    def other(self, scope_):
        st.session_touch(self.root, "sess-other", "claude")
        dec.declare(self.root, _raw(scope_), session_id="sess-other")
        return st.current(self.root, "sess-other")

    def touched(self):
        return scope.touched(self.root, st.current(self.root, self.session))

    def test_live_other_task_scope_not_attributed(self):
        self.other(["lessons/l9/"])

        def during():
            self.write("web/pages/a.tsx", "export const A = 9\n")   # 이 명령
            self.write("lessons/l9/drops.txt", "1\n")                # 다른 세션 편집 작업(훅이 못 본 셸 쓰기)
            self.write("tools/out.json", "{\"a\": 1}\n")             # 이 명령 스크립트가 범위 밖에 쓴 파일 — 가진 작업이 없다

        self.run_cmd(SCRIPT, during)
        self.assertEqual(self.touched(), ["tools/out.json", "web/pages/a.tsx"])

    def test_other_task_edits_claim_and_own_scope_wins(self):
        t = self.other(["docs/"])
        t["edits"] = ["web/pages/a.tsx", "tools/out.json"]
        st.save(self.root, t)

        def during():
            self.write("web/pages/a.tsx", "export const A = 9\n")    # 다른 작업도 기록했지만 이 작업 선언 범위 안 → 이 작업
            self.write("tools/out.json", "{\"a\": 1}\n")              # 범위 밖이고 다른 작업이 기록 → 빼기

        self.run_cmd(SCRIPT, during)
        self.assertEqual(self.touched(), ["web/pages/a.tsx"])

    def test_finished_other_task_does_not_claim(self):
        t = self.other(["lessons/l9/"])
        old = "2000-01-01T00:00:00+00:00"
        t.update({"ended_at": old, "last_activity_at": old, "started_at": old, "declared_at": old})
        st.save(self.root, t)
        self.run_cmd(SCRIPT, lambda: self.write("lessons/l9/drops.txt", "1\n"))
        self.assertEqual(self.touched(), ["lessons/l9/drops.txt"])


class GitSyncTest(OpaqueCase):
    def setUp(self):
        super().setUp()
        self.write("web/pages/a.tsx", "export const A = 1\n")
        self.write("web/pages/z.tsx", "export const Z = 1\n")
        self.commit("base")
        st.session_touch(self.root, self.session, "claude")

    def git(self, *args):
        subprocess.run(["git"] + list(args), cwd=self.root, check=True, capture_output=True)

    def test_stash_pop_files_not_attributed(self):
        self.write("web/pages/z.tsx", "export const Z = 2\n")
        self.git("stash")
        dec.declare(self.root, _raw(["web/pages/"]), session_id=self.session)
        cmd = "git stash pop && " + SCRIPT

        def during():
            self.git("stash", "pop")
            self.write("web/pages/a.tsx", "export const A = 9\n")

        self.run_cmd(cmd, during)
        self.assertEqual(scope.touched(self.root, st.current(self.root, self.session)), ["web/pages/a.tsx"])

    def test_pulled_change_not_attributed(self):
        dec.declare(self.root, _raw(["web/pages/"]), session_id=self.session)
        cmd = "git pull --rebase --autostash; " + SCRIPT

        def during():
            # 이력에서 온 변경(HEAD 이동) 뒤 작업 트리에 남은 같은 파일 변경 — 이 명령 스크립트가 쓴 게 아니다
            self.write("web/pages/z.tsx", "export const Z = 3\n")
            self.commit("from elsewhere")
            self.write("web/pages/z.tsx", "export const Z = 4\n")
            self.write("web/pages/a.tsx", "export const A = 9\n")

        self.run_cmd(cmd, during)
        self.assertEqual(scope.touched(self.root, st.current(self.root, self.session)), ["web/pages/a.tsx"])
