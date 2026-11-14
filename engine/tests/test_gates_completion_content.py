"""완료 게이트 — 수정 시각만 늦어진 파일로 멀쩡한 증거를 무효로 보지 않는다(2026-11-15 현장).

다른 세션이 같은 작업 트리에서 git pull --rebase --autostash를 돌리면, 커밋 전 파일이 stash됐다가 같은 내용으로
다시 써져 수정 시각만 늦어진다. 증거가 이 세션의 마지막 도구 쓰기 뒤에 만들어졌고 내용이 그대로면 인정한다.
옛 스크린샷 재사용 방지와 증거 뒤 진짜 편집 차단은 그대로다.
"""

import os
import time

from vmc_harness.gates import evidence, state

from tests.test_gates_safety import GatesCase, TaskMixin


class TestCompletionContent(TaskMixin, GatesCase):
    def setUp(self):
        super().setUp()
        self.write("fe/pages/settle.tsx", "export const A = 1\n")
        self.commit()
        self.hook("SessionStart", source="startup")

    def stop(self):
        return self.hook("Stop", stop_hook_active=False)

    def _agent_edit(self, text):
        self.write("fe/pages/settle.tsx", text)
        state.note_writes(self.root, self.session, ["fe/pages/settle.tsx"])   # PostToolUse가 남기는 쓰기 시각

    def _shot(self, t, name="shot.png"):
        png = self._tmp / name
        png.write_bytes(b"\x89PNG" + name.encode())
        return evidence.screenshot(self.root, t["task_id"], str(png), "정산 페이지")

    def _touch_later(self, sec=5):
        p = self.root / "fe/pages/settle.tsx"
        t = time.time() + sec
        os.utime(str(p), (t, t))

    def test_autostash_rewrite_keeps_evidence(self):
        t = self.set_task()
        self._agent_edit("export const A = 2\n")
        time.sleep(0.05)
        m = self._shot(t)
        self.assertIn("fe/pages/settle.tsx", m.get("tree_sha") or {})
        self._touch_later()                     # 다른 세션의 autostash: 같은 내용, 시각만 늦게
        self.assertEqual(self.stop(), {})

    def test_real_edit_after_evidence_blocks(self):
        t = self.set_task()
        self._agent_edit("export const A = 2\n")
        time.sleep(0.05)
        self._shot(t)
        time.sleep(0.05)
        self.write("fe/pages/settle.tsx", "export const A = 3\n")   # 증거 뒤 내용이 바뀜(누가 썼든)
        self._touch_later()
        self.assertEqual(self.stop().get("decision"), "block")

    def test_evidence_before_own_write_blocks(self):
        t = self.set_task()
        self._agent_edit("export const A = 2\n")
        png = self._tmp / "old.png"
        png.write_bytes(b"\x89PNG-old")
        os.utime(str(png), (time.time() - 3600, time.time() - 3600))   # 편집 전에 찍어 둔 스크린샷
        evidence.screenshot(self.root, t["task_id"], str(png), "정산")
        self._touch_later()
        self.assertEqual(self.stop().get("decision"), "block")

    def test_no_own_write_record_falls_back_to_time(self):
        t = self.set_task()
        self.write("fe/pages/settle.tsx", "export const A = 2\n")   # 도구 쓰기 기록 없음(스크립트 편집 등)
        time.sleep(0.05)
        self._shot(t)
        self._touch_later()
        self.assertEqual(self.stop().get("decision"), "block")

    def test_old_marker_without_snapshot_uses_time(self):
        from vmc_harness.gates import completion
        self.assertFalse(completion.same_content({}, {"a": "x"}))
        self.assertFalse(completion.same_content({"tree_sha": {"a": "x"}}, {"a": None}))
        self.assertTrue(completion.same_content({"tree_sha": {"a": "x", "b": "y"}}, {"a": "x"}))
