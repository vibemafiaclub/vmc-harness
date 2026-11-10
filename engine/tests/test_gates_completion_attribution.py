"""완료 게이트·과설계가 "이 작업이 쓴 파일"만 세는가 (현장 결함 — 동시 세션 오탐).

같은 작업 트리를 여러 세션이 함께 쓰면, 다른 세션이 쓴 페이지 파일(미커밋·커밋 모두)이나 끝난 작업의 파일 때문에
상관없는 세션의 종료가 막혔다. 해석 못 한 셸 명령(스크립트)이 쓴 파일은 실행 뒤 대조가 이 작업의 편집으로 넣는다.
"""

from vmc_harness.gates import completion
from vmc_harness.task import declare as dec, scope, state as st

from tests.test_gates_opaque import OpaqueCase

SCRIPT = "python3 - <<'EOF'\nopen('web/pages/a.tsx','w').write('x')\nEOF"


def _raw(scope_):
    return {"intent": "x", "scope": scope_, "predict": {"tokens": [1, 2, 3], "agent_sec": [60, 120, 170]}}


class AttributionTest(OpaqueCase):
    def setUp(self):
        super().setUp()
        self.write("web/pages/a.tsx", "export const A = 1\n")
        self.write("docs/topic.md", "t\n")
        self.commit("pages")
        st.session_touch(self.root, self.session, "claude")

    def mine(self):
        return st.current(self.root, self.session)

    def stop(self):
        return self.hook("Stop", stop_hook_active=False)

    def test_other_session_page_edit_does_not_block(self):
        dec.declare(self.root, _raw(["docs/topic.md"]), session_id=self.session)
        self.agent_write("docs/topic.md", "t2\n", sid=self.session)
        # 다른 세션(끝난 작업·선언 없음)이 페이지를 고치고 일부는 커밋까지 함
        self.write("web/pages/a.tsx", "export const A = 2\n")
        self.write("web/pages/b.tsx", "export const B = 1\n")
        self.commit("other session")
        self.write("web/pages/c.tsx", "export const C = 1\n")
        t = self.mine()
        self.assertEqual(scope.touched(self.root, t), ["docs/topic.md"])
        self.assertIsNone(completion.evaluate(str(self.root), self.session, t))
        self.assertNotEqual(self.stop().get("decision"), "block")

    def test_script_written_page_is_this_task(self):
        dec.declare(self.root, _raw(["web/pages/"]), session_id=self.session)
        self.write("web/pages/other.tsx", "export const O = 1\n")   # 명령 전에 다른 세션이 쓴 파일
        self.run_cmd(SCRIPT, lambda: self.write("web/pages/a.tsx", "export const A = 9\n"))
        self.assertEqual(scope.touched(self.root, self.mine()), ["web/pages/a.tsx"])
        out = self.stop()
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("web/pages/a.tsx", out["reason"])
        self.assertNotIn("other.tsx", out["reason"])

    def test_no_task_session_only_counts_its_script_writes(self):
        self.write("web/pages/other.tsx", "export const O = 1\n")   # 다른 세션
        self.assertNotEqual(self.stop().get("decision"), "block")
        self.run_cmd(SCRIPT, lambda: self.write("web/pages/a.tsx", "export const A = 9\n"))
        out = self.stop()
        self.assertEqual(out.get("decision"), "block")
        self.assertNotIn("other.tsx", out["reason"])


class MonorepoBehaviorGlobsTest(OpaqueCase):
    def test_apps_subfolders_detected(self):
        from vmc_harness.bootstrap import facts
        self.write("apps/web/package.json", "{}")
        self.write("apps/web/app/page.tsx", "x")
        self.write("apps/web/app/api/r/route.ts", "x")
        self.write("projects/notes/n.html", "<p>")
        g = facts.detect_behavior_globs(self.root, ["apps", "projects"], [], None)
        self.assertIn("apps/web/app/**", g["page"])
        self.assertIn("apps/web/app/api/**", g["api"])
