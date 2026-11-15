"""작업별 사람 턴 귀속 (0.1.12 현장 결함 회귀) — 작업 장부 actual.human_turns가 늘 0이던 것.

사람 메시지는 선언·재개(첫 도구 호출)보다 먼저 와서 작업 구간 밖에 놓인다. 합성 시각만 쓴다.
"""

import datetime as _dt

from tests.util import HarnessTestCase

from vmc_harness.task import measure

UTC = _dt.timezone.utc


def t(mm, ss=0):
    return _dt.datetime(2026, 11, 1, 10, mm, ss, tzinfo=UTC)


def z(d):
    return d.isoformat().replace("+00:00", "Z")


class TaskHumanTurnsTest(HarnessTestCase):
    """작업별 사람 턴 — 사람 메시지는 선언·재개(첫 도구 호출)보다 먼저 와서 구간 밖에 놓인다(0.1.12 전엔 늘 0)."""

    @staticmethod
    def task(tid, *segs):
        return {"task_id": tid, "segments": [{"start": s.isoformat(), "end": e.isoformat() if e else None}
                                              for s, e in segs]}

    def test_resume_each_turn(self):
        # 턴마다 SessionEnd로 마감 → 다음 메시지 뒤 첫 도구 호출에서 다시 연다
        prompts = [z(t(0)), z(t(10)), z(t(20))]
        a = self.task("A", (t(0, 20), t(5)), (t(10, 15), t(15)))
        b = self.task("B", (t(20, 10), t(25)))
        self.assertEqual(measure.owned_prompts(a, prompts, [a, b]), 2)
        self.assertEqual(measure.owned_prompts(b, prompts, [a, b]), 1)

    def test_redeclare_stub_does_not_double_count(self):
        # 같은 턴에서 옛 작업이 3초 다시 열렸다가 재선언으로 새 작업이 시작 — 메시지는 새 작업 하나에만
        prompts = [z(t(0)), z(t(10))]
        old = self.task("OLD", (t(0, 5), t(0, 8)))
        new = self.task("NEW", (t(0, 8), t(8)))
        self.assertEqual(measure.owned_prompts(old, prompts, [old, new]), 0)
        self.assertEqual(measure.owned_prompts(new, prompts, [old, new]), 1)

    def test_chat_turn_without_task_not_counted(self):
        prompts = [z(t(0)), z(t(10))]
        a = self.task("A", (t(10, 5), t(12)))
        self.assertEqual(measure.owned_prompts(a, prompts, [a]), 1)  # 10:00 메시지만(10:00 이전 대화 턴은 작업 없음)
