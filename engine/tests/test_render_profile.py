"""독자 프로필: 같은 개념 중 가장 많이 쓴 말·실제 표기·업무 대상 이름·문장 견본 (spec 08 실측 예시를 기대값으로)."""

from tests.util import HarnessTestCase

from vmc_harness.render import lexicon, profile


def _prompts():
    p = []
    p += ["이 페이지 확인해줘"] * 81 + ["화면 깨졌어"] * 31
    p += ["운영에 적용해줘"] * 58 + ["반영됐어?"] * 17
    p += ["원복해"] * 5 + ["롤백 해줘"] * 3
    p += ["QA pass"] * 10 + ["이건 hold"] * 9
    p += ["sp 고쳐줘"] * 64 + ["SP 확인"] * 2
    p += ["플래그 확인"] * 1 + ["초기화해"] * 2
    p += ["usp_WorkRequest_Reject 반려 재상신 확인해줘"] * 3
    return p


class ProfileTest(HarnessTestCase):
    def test_spec08_measured_example(self):
        prof = profile.build(_prompts(), root=self.root)
        pref = prof["preferred"]
        self.assertEqual(pref["페이지"]["term"], "페이지")
        self.assertEqual(pref["페이지"]["counts"]["페이지"], 81)
        self.assertEqual(pref["페이지"]["counts"]["화면"], 31)
        self.assertEqual(pref["적용"]["term"], "적용")
        self.assertEqual((pref["적용"]["counts"]["적용"], pref["적용"]["counts"]["반영"]), (58, 17))
        self.assertEqual(pref["원복"]["term"], "원복")
        self.assertEqual((pref["원복"]["counts"]["원복"], pref["원복"]["counts"]["롤백"]), (5, 3))
        self.assertEqual(pref["pass"]["counts"]["pass"], 10)
        self.assertEqual(pref["hold"]["term"], "hold")
        self.assertEqual(pref["sp"]["term"], "sp")  # 소문자 표기
        self.assertEqual(pref["플래그"]["counts"]["플래그"], 1)
        self.assertEqual(pref["초기화"]["counts"]["초기화"], 2)
        self.assertIn("usp_WorkRequest_Reject", prof["objects"])
        self.assertTrue(prof["samples"])
        self.assertTrue(all(len(s) <= 60 for s in prof["samples"]))
        # 독자가 안 쓴 동의어는 그 사람 선호어로
        disc = profile.discouraged(prof)
        self.assertEqual(disc.get("보류"), "hold")
        self.assertNotIn("화면", disc)  # 31번 썼다 — 아는 말

    def test_compute_saves_local_and_merges_objects(self):
        prof = profile.compute(self.root, prompts=_prompts())
        self.assertEqual(profile.load(self.root)["preferred"]["원복"]["term"], "원복")
        self.assertIn("usp_WorkRequest_Reject", lexicon.load(self.root)["terms"])
        self.assertEqual(prof["source"]["prompts"], len(_prompts()))

    def test_lexicon_group_extension(self):
        lx = lexicon.load(self.root)
        lx["groups"] = [["결재함", "결재 목록"]]
        lexicon.save(self.root, lx)
        prof = profile.compute(self.root, prompts=["결재함 봐줘"] * 3 + ["결재 목록 봐"])
        self.assertEqual(prof["preferred"]["결재함"]["term"], "결재함")

    def test_unclear_word_learning(self):
        self.assertEqual(lexicon.detect_unclear("'멱등성'이 뭔 말이야?"), "멱등성")
        self.assertEqual(lexicon.detect_unclear("트랜잭션 무슨 뜻이야"), "트랜잭션")
        self.assertIsNone(lexicon.detect_unclear("이게 뭔 말이야"))
        r = lexicon.unclear(self.root, "멱등성")
        self.assertFalse(r["team_avoid"])
        self.assertIn("멱등성", profile.load(self.root)["avoid"])
        r = lexicon.unclear(self.root, "멱등성")
        self.assertTrue(r["team_avoid"])  # render.lexicon_unclear_min = 2
        v = lexicon.vocab_for(self.root, profile.load(self.root))
        self.assertIn("멱등성", v.avoid)
