"""학습 오분류 수정(0.1.14) — 직전 AI 응답 맥락·인용 라벨·기계 검사·서로 다른 세션 2건·병합 기계 확인·옛 후보 정리.
픽스처는 모두 합성이다."""

import json
from unittest import mock

from tests.util import HarnessTestCase

from vmc_harness.core import ledger
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.project import local_dir
from vmc_harness.ledger import selfcheck
from vmc_harness.sessions import Session


def _l(**kw):
    base = {"label": "correction", "same_as": None, "gist": "배포 뒤 운영 화면을 직접 확인한다",
            "title": "배포 뒤 운영 화면 확인", "situation": "배포를 마쳤다고 보고할 때",
            "judgment": "배포 뒤 운영 화면을 직접 열어 바뀐 부분을 확인하고 보고한다", "signals": ["배포 완료 보고"],
            "not_when": "배포가 없는 작업", "cross_project": True, "why_not_higher": "상황마다 다르다",
            "prompt_index": 0, "quote": "운영 화면은 안 봤잖아"}
    base.update(kw)
    return base


def _session(sid, prompts, says=()):
    return Session(session_id=sid, agent="claude",
                   human_prompts=[{"at": at, "text": t} for at, t in prompts],
                   assistant_texts=[{"at": at, "text": t} for at, t in says],
                   tool_calls=[{"name": "Edit"}] * 3, human_turns=len(prompts), measured=True,
                   ended_at=max(at for at, _ in prompts))


class ContextTest(HarnessTestCase):
    def test_prompt_contexts_takes_ai_text_between_prompts_and_strips_meta(self):
        raw = [{"at": "2026-11-18T10:00:00+09:00", "text": "a"}, {"at": "2026-11-18T10:10:00+09:00", "text": "b"},
               {"at": "2026-11-18T10:20:00+09:00", "text": "c"}]
        says = [{"at": "2026-11-18T09:59:00+09:00", "text": "첫 응답"},
                {"at": "2026-11-18T10:05:00+09:00", "text": "시안 A·B 중 고를까요?\n__SIMPLECLAW_ARTIFACT__ {\"kind\":\"file\"}"},
                {"at": "2026-11-18T10:06:00+09:00", "text": "-# 메타 줄\n기본값은 A입니다."}]
        ctx = selfcheck.prompt_contexts(raw, says, 600)
        self.assertEqual(ctx[0], "첫 응답")
        self.assertIn("고를까요?", ctx[1])
        self.assertIn("기본값은 A입니다.", ctx[1])
        self.assertNotIn("ARTIFACT", ctx[1])
        self.assertNotIn("메타 줄", ctx[1])
        self.assertEqual(ctx[2], "")  # 사이에 AI 글이 없으면 맥락 없음
        self.assertEqual(len(selfcheck.prompt_contexts(raw, says, 5)[1]), 5)  # 끝 n자

    def test_extract_body_carries_context_before_prompt(self):
        seen = {}

        def fake(body, **kw):
            seen["body"] = body
            return {"text": "{}", "tokens": 1}
        with mock.patch("vmc_harness.core.llm.available", return_value=True), \
                mock.patch("vmc_harness.core.llm.claude_p", side_effect=fake):
            selfcheck.extract(self.root, ["그래 B로 가자", "안 됐네"], contexts=["A·B 중 고를까요?", ""])
        self.assertIn("0. [직전 AI] …A·B 중 고를까요?\n   [사람] 그래 B로 가자", seen["body"])
        self.assertIn("1. [사람] 안 됐네", seen["body"])


class ValidateTest(HarnessTestCase):
    def test_only_corrections_pass_machine_checks(self):
        prompts = ["운영 화면은 안 봤잖아", "그래 H2가 좋다", "들어가도돼", "이건 별로"]
        out = {"corrections": 3, "learnings": [
            _l(),
            _l(label="answer", prompt_index=0),
            _l(label=None, prompt_index=0),
            _l(prompt_index=1, quote="그래 H2가 좋다"),            # 짧은 선택어
            _l(prompt_index=2, quote="들어가도돼"),                # 짧은 승인어
            _l(prompt_index=3, quote="이건 별로", judgment="이 발화만으로는 원인을 알 수 없다"),
            _l(prompt_index=3, quote="이건 별로", judgment="빼기 전에 사용자에게 먼저 확인한다"),
        ]}
        rej = []
        _, learnings, _ = selfcheck.validate(out, prompts, rej)
        self.assertEqual(len(learnings), 1)
        self.assertEqual(sorted(r[0] for r in rej),
                         ["approval_quote", "approval_quote", "ask_user", "label:answer", "label:none", "unsure"])


class ApplyTest(HarnessTestCase):
    def _run(self, session, out):
        with mock.patch("vmc_harness.sessions.parse", return_value=session), \
                mock.patch("vmc_harness.core.llm.available", return_value=True), \
                mock.patch("vmc_harness.core.llm.claude_p",
                           return_value={"text": json.dumps(out, ensure_ascii=False), "tokens": 1}):
            return selfcheck.run_session(self.root, "/x/%s.jsonl" % session.session_id, "claude", session.session_id)

    def test_card_needs_two_different_sessions(self):
        r1 = self._run(_session("S1", [("2026-11-18T10:00:00+09:00", "운영 화면은 안 봤잖아")]),
                       {"corrections": 1, "learnings": [_l()]})
        cid = r1["learnings"][0]["candidate_id"]
        # 같은 세션의 다음 턴 — 개수는 늘어도 세션은 하나라 카드가 아니다
        r2 = self._run(_session("S1", [("2026-11-18T10:00:00+09:00", "운영 화면은 안 봤잖아"),
                                       ("2026-11-18T11:00:00+09:00", "또 운영 화면은 안 봤잖아")]),
                       {"corrections": 1, "learnings": [_l(same_as=cid)]})
        self.assertNotIn("card", r2["learnings"][0])
        self.assertEqual(r2["learnings"][0]["candidate_id"], cid)
        r3 = self._run(_session("S2", [("2026-11-18T12:00:00+09:00", "운영 화면은 안 봤잖아")]),
                       {"corrections": 1, "learnings": [_l(same_as=cid)]})
        self.assertIn("card", r3["learnings"][0])
        d = read_json(local_dir(self.root) / "quotes" / ("%s.draft.json" % cid))
        self.assertEqual(d["sessions"], ["S1", "S2"])

    def test_unrelated_same_as_starts_new_candidate_and_keeps_first_draft(self):
        r1 = self._run(_session("S1", [("2026-11-18T10:00:00+09:00", "운영 화면은 안 봤잖아")]),
                       {"corrections": 1, "learnings": [_l()]})
        cid = r1["learnings"][0]["candidate_id"]
        other = _l(same_as=cid, gist="회의 일정은 캘린더에서 찾는다", title="일정 출처",
                   judgment="회의 날짜는 캘린더 원본에서 읽는다", quote="11/8 미팅은 안했음")
        r2 = self._run(_session("S2", [("2026-11-18T12:00:00+09:00", "11/8 미팅은 안했음")]),
                       {"corrections": 1, "learnings": [other]})
        self.assertNotEqual(r2["learnings"][0]["candidate_id"], cid)
        self.assertNotIn("card", r2["learnings"][0])
        d = read_json(local_dir(self.root) / "quotes" / ("%s.draft.json" % cid))
        self.assertEqual(d["title"], "배포 뒤 운영 화면 확인")

    def test_answer_label_is_not_learned(self):
        r = self._run(_session("S1", [("2026-11-18T10:00:00+09:00", "커뮤니티 일화는 들어가도 됩니다")],
                               [("2026-11-18T09:59:00+09:00", "일화를 넣을까요? 기본값은 넣음입니다.")]),
                      {"corrections": 0, "learnings": [_l(label="answer", quote="커뮤니티 일화는 들어가도 됩니다")]})
        self.assertEqual(r["learnings"], [])
        self.assertEqual(ledger.list_candidates(self.root), [])


class MigrationTest(HarnessTestCase):
    def _legacy(self, summary, quotes, judgment="배포 뒤 화면을 확인한다"):
        c = ledger.create_candidate(self.root, "learning", summary, quote=quotes[0])
        q = local_dir(self.root) / "quotes" / ("%s.txt" % c["candidate_id"])
        q.write_text("\n---\n".join(quotes), encoding="utf-8")
        write_json(local_dir(self.root) / "quotes" / ("%s.draft.json" % c["candidate_id"]),
                   {"title": summary, "judgment": judgment, "cross_project": True})
        return c["candidate_id"]

    def test_archives_failing_legacy_and_resets_sessions_once(self):
        bad = self._legacy("고른 버전 기준", ["그래 H29가 좋다"])
        unsure = self._legacy("얼굴 각도", ["각도가 어색한데"], judgment="원인은 더 봐야 확정할 수 있다")
        good = self._legacy("배포 뒤 확인", ["운영 화면은 안 봤잖아", "이거 지금 작동해?"])
        res = selfcheck.migrate_candidates(self.root)
        self.assertEqual(res, {"archived": 2, "kept": 1})
        st = {c["candidate_id"]: c["status"] for c in ledger.list_candidates(self.root)}
        self.assertEqual((st[bad], st[unsure], st[good]), ("archived", "archived", "open"))
        d = read_json(local_dir(self.root) / "quotes" / ("%s.draft.json" % good))
        self.assertEqual(d["sessions"], [])
        self.assertIsNone(selfcheck.migrate_candidates(self.root))  # 1회만
