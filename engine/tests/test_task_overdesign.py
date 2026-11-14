import json
import sys
import types
from unittest import mock

from tests.util import HarnessTestCase

from vmc_harness.task import declare as dec, overdesign, scope, state as st

DUP = '''def total_price(items, rate):
    total = 0
    for it in items:
        if it.qty > 0:
            total += it.price * it.qty
    if rate:
        total = total * (1 + rate)
    return round(total, 2)
'''


class OverdesignTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.write("app/billing.py", DUP + "\n\ndef use():\n    return total_price([], 0)\n")
        self.write("package.json", json.dumps({"dependencies": {"react": "1"}}))
        self.write("requirements.txt", "requests==2\n")
        self.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["flask>=2", "pkg[extra]>=1"]\n')
        self.commit()
        st.session_touch(self.root, "S1", "claude")
        patcher = mock.patch("vmc_harness.task.pm.start", return_value="sync")  # 실제 claude -p를 띄우지 않는다
        patcher.start()
        self.addCleanup(patcher.stop)

    def _task(self, **kw):
        d = {"intent": "청구 금액 계산 수정", "scope": ["app/billing.py"],
             "predict": {"tokens": [1, 2, 3], "agent_sec": [60, 120, 170]}}
        d.update(kw)
        return dec.declare(self.root, d)["task"]

    def _kinds(self, task):
        task = st.by_id(self.root, task["task_id"]) or task
        return {(s["kind"], s.get("name") or s.get("path")) for s in overdesign.machine_signals(self.root, task)}

    def test_new_file_and_module(self):
        t = self._task()
        self.agent_write("app/newpkg/helpers.py", "def _x():\n    return 1\n")
        k = self._kinds(t)
        self.assertIn(("new_file_out_of_scope", "app/newpkg/helpers.py"), k)
        self.assertIn(("new_module_out_of_scope", "app/newpkg"), k)
        t2 = self._task(new=["app/newpkg/"])
        self.assertEqual({x for x in self._kinds(t2) if x[0].startswith("new_")}, set())

    def test_new_dependency(self):
        t = self._task()
        self.agent_write("package.json", json.dumps({"dependencies": {"react": "1", "lodash": "4"}}))
        self.agent_write("requirements.txt", "requests==2\nhttpx>=0.1  # comment\n")
        self.agent_write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["flask>=2", "pkg[extra]>=1", "pydantic"]\n')
        k = self._kinds(t)
        for name in ("lodash", "httpx", "pydantic"):
            self.assertIn(("new_dependency", name), k)
        self.assertNotIn(("new_dependency", "react"), k)
        t2 = self._task(new=["lodash", "httpx", "pydantic"])
        self.assertFalse([x for x in self._kinds(t2) if x[0] == "new_dependency"])

    def test_unreferenced_public(self):
        t = self._task()
        self.agent_write("app/billing.py", DUP + "\n\ndef use():\n    return total_price([], 0)\n\n"
                                         "def export_csv(rows):\n    return rows\n\n"
                                         "def used_one():\n    return 1\n\nX = used_one()\n")
        self.agent_write("web/a.ts", "export function formatWon(n: number) { return n }\nexport const GET = 1\n")
        k = self._kinds(t)
        self.assertIn(("unreferenced_public", "export_csv"), k)
        self.assertNotIn(("unreferenced_public", "used_one"), k)
        self.assertIn(("unreferenced_public", "formatWon"), k)
        self.assertNotIn(("unreferenced_public", "GET"), k)

    def test_single_impl_abstraction_and_convention(self):
        t = self._task()
        self.agent_write("app/billing.py", DUP + "\n\ndef use():\n    return total_price([], 0)\n\n"
                                         "from typing import Protocol\n\n"
                                         "class _PriceRepo(Protocol):\n    def get(self): ...\n\n"
                                         "class MssqlPriceRepo:\n    def get(self):\n        return 1\n\n"
                                         "R = MssqlPriceRepo()\n")
        self.assertIn(("single_impl_abstraction", "_PriceRepo"), self._kinds(t))
        fake = types.ModuleType("vmc_harness.bootstrap.facts")
        fake.load = lambda root: {"conventions": ["도메인마다 Protocol + InMemory + Mssql 3종 세트"]}
        pkg = types.ModuleType("vmc_harness.bootstrap")
        saved = {k: sys.modules.get(k) for k in ("vmc_harness.bootstrap", "vmc_harness.bootstrap.facts")}
        sys.modules["vmc_harness.bootstrap"] = sys.modules.get("vmc_harness.bootstrap") or pkg
        sys.modules["vmc_harness.bootstrap.facts"] = fake
        try:
            self.assertNotIn(("single_impl_abstraction", "_PriceRepo"), self._kinds(t))
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        self.agent_write("web/b.ts", "export interface Store { get(): number }\nclass A implements Store { get() { return 1 } }\n"
                               "export const s = new A()\nconsole.log(s)\n")
        self.assertIn(("single_impl_abstraction", "Store"), self._kinds(t))

    def test_near_duplicate(self):
        t = self._task()
        copy = DUP.replace("total_price", "sum_cost").replace("items", "lines").replace("total", "acc")
        self.agent_write("app/billing.py", DUP + "\n\ndef use():\n    return total_price([], 0) + sum_cost([], 0)\n\n" + copy)
        k = self._kinds(t)
        self.assertIn(("near_duplicate_function", "sum_cost"), k)

    def test_describe_has_no_numbers_pressure(self):
        text = overdesign.describe([{"kind": "new_dependency", "path": "package.json", "name": "lodash"}])
        self.assertEqual(text, "과설계 신호: 새 의존성 lodash(package.json)")

    def test_scope_matching(self):
        d = {"scope": ["src/", "web/*.tsx", "app/billing.py"], "new": ["lib/new_mod"], "test_change": None}
        self.assertTrue(scope.in_scope(self.root, d, "src/a/b.py"))
        self.assertTrue(scope.in_scope(self.root, d, "web/x.tsx"))
        self.assertTrue(scope.in_scope(self.root, d, "lib/new_mod/a.py"))
        self.assertFalse(scope.in_scope(self.root, d, "srcx/a.py"))
        self.assertFalse(scope.in_scope(self.root, d, "app/other.py"))


class OverdesignPromptTest(HarnessTestCase):
    """LLM 판정 입력 — 이번 작업 지시만 요청으로, 착수 선언 포함, 기존 관례 새 파일 제외 (2026-11-14 현장)."""

    def test_split_prompts_by_task_start(self):
        ps = [{"at": "2026-11-14T20:00:00+09:00", "text": "A님 브리핑해줘"},
              {"at": "2026-11-14T20:40:00+09:00", "text": "미팅 링크 첨부"},
              {"at": "2026-11-14T20:50:00+09:00", "text": "ㄱㄱ"}]
        prior, start, during = overdesign._split_prompts(ps, "2026-11-14T20:42:00+09:00")
        self.assertEqual(prior, ["A님 브리핑해줘"])
        self.assertEqual(start, "미팅 링크 첨부")
        self.assertEqual(during, ["ㄱㄱ"])
        prior, start, during = overdesign._split_prompts(ps, None)
        self.assertEqual((len(prior), start, during), (2, "ㄱㄱ", []))
        self.assertEqual(overdesign._split_prompts([], "2026-11-14T20:42:00+09:00"), ([], "", []))

    def test_prompts_text_labels(self):
        fake = types.SimpleNamespace(human_prompts=[
            {"at": "2026-11-14T20:00:00+09:00", "text": "브리핑해줘"},
            {"at": "2026-11-14T20:40:00+09:00", "text": "미팅 링크"}])
        with mock.patch("vmc_harness.sessions.parse", return_value=fake):
            out = overdesign._prompts_text("x.jsonl", "claude", 4000, since="2026-11-14T20:42:00+09:00",
                                           prior_max_chars=800)
        self.assertTrue(out.startswith("[이전 대화"))
        self.assertIn("[이번 작업을 시작한 사람 지시]\n미팅 링크", out)
        self.assertLess(out.index("브리핑해줘"), out.index("[이번 작업을 시작한 사람 지시]"))

    def test_declare_text(self):
        self.assertEqual(overdesign._declare_text({"declare": {"intent": "회의록 적재", "end": "커밋까지"}}),
                         "[착수 선언]\n회의록 적재 (커밋까지)")
        self.assertEqual(overdesign._declare_text({}), "")

    def test_convention_files(self):
        for n in ("261112_가_인터뷰.md", "261112_가_인터뷰_transcript.md",
                  "261113_나_인터뷰.md", "261113_나_인터뷰_transcript.md"):
            self.write("logs/" + n, "x\n")
        self.write("logs/README.md", "x\n")
        self.commit()
        st.session_touch(self.root, "S1", "claude")
        with mock.patch("vmc_harness.task.pm.start", return_value="sync"):
            task = dec.declare(self.root, {"intent": "회의록", "scope": ["logs/"],
                                           "predict": {"tokens": [1, 2, 3], "agent_sec": [60, 120, 170]}})["task"]
        files = ["logs/261114_다_인터뷰.md", "logs/261114_다_인터뷰_transcript.md", "logs/new_plan.md",
                 "logs/README.md"]
        conv = overdesign.convention_files(self.root, task, files)
        self.assertEqual(conv, {"logs/261114_다_인터뷰.md", "logs/261114_다_인터뷰_transcript.md"})


class OverdesignApprovedPlanTest(HarnessTestCase):
    """사람이 "ㄱㄱ"만 하고 계획은 직전 어시스턴트 제안에 있던 흐름 — 그 계획을 요청으로 넣는다(2026-11-15 현장)."""

    def _fake(self, start_text):
        return types.SimpleNamespace(
            human_prompts=[{"at": "2026-11-15T01:00:00+09:00", "text": "모션으로 고도화할 부분 점검해줘"},
                           {"at": "2026-11-15T01:20:00+09:00", "text": start_text}],
            assistant_texts=[{"at": "2026-11-15T00:50:00+09:00", "text": "옛 답(이전 요청 전)"},
                             {"at": "2026-11-15T01:10:00+09:00", "text": "A 크로스페이드 · B 강조 · C 그림 확대"},
                             {"at": "2026-11-15T01:30:00+09:00", "text": "작업 뒤 보고"}])

    def test_short_ack_includes_previous_proposal(self):
        with mock.patch("vmc_harness.sessions.parse", return_value=self._fake("ㄱㄱ")):
            out = overdesign._prompts_text("x.jsonl", "claude", 4000, since="2026-11-15T01:21:00+09:00",
                                           plan_max_chars=3000, short_max_chars=12)
        self.assertIn("[사람이 승인한 계획 — 직전 어시스턴트 제안]\nA 크로스페이드 · B 강조 · C 그림 확대", out)
        self.assertNotIn("옛 답", out)
        self.assertNotIn("작업 뒤 보고", out)
        self.assertLess(out.index("[사람이 승인한 계획"), out.index("[이번 작업을 시작한 사람 지시]\nㄱㄱ"))

    def test_long_instruction_does_not_add_plan(self):
        with mock.patch("vmc_harness.sessions.parse", return_value=self._fake("장표를 시안 1-A로 다시 만들어서 올려줘")):
            out = overdesign._prompts_text("x.jsonl", "claude", 4000, since="2026-11-15T01:21:00+09:00",
                                           plan_max_chars=3000, short_max_chars=12)
        self.assertNotIn("승인한 계획", out)

    def test_plan_off_when_zero(self):
        with mock.patch("vmc_harness.sessions.parse", return_value=self._fake("ㄱㄱ")):
            out = overdesign._prompts_text("x.jsonl", "claude", 4000, since="2026-11-15T01:21:00+09:00")
        self.assertNotIn("승인한 계획", out)

    def test_system_prompt_excludes_fixes(self):
        self.assertIn("결함을 바로잡은 것", overdesign.OD_SYSTEM)
        self.assertIn("승인한 계획", overdesign.OD_SYSTEM)


class OverdesignExistingUsageTest(HarnessTestCase):
    """'쓰이지 않는 장치'라고 지목한 이름이 저장소 다른 파일에서 이미 쓰이면 기존 관례 — 신호에서 뺀다(2026-11-15 현장)."""

    def test_unused_claim_dropped_when_used_elsewhere(self):
        self.write("app/deck/shell.tsx", "const isHero = el.type.isHero;\n")
        self.write("app/deck/cs.css", ".yp-body:has(.cs-top) { top: 0 }\n")
        self.write("app/new/page.tsx", "<div className=\"cs-top\" />\nFrame.isHero = true;\n")
        self.commit()
        sigs = [{"kind": "unrequested_option", "path": "app/new/page.tsx",
                 "name": "쓰이지 않는 것으로 보이는 cs-top div와 isHero 플래그"},
                {"kind": "unrequested_option", "path": "app/new/page.tsx", "name": "새 --verbose 옵션 추가"},
                {"kind": "unrequested_option", "path": "app/new/page.tsx", "name": "쓰이지 않는 newKnobXyz 설정"}]
        keep, dropped = overdesign.existing_usage(self.root, sigs, ["app/new/page.tsx"])
        self.assertEqual([s["name"] for s in dropped], ["쓰이지 않는 것으로 보이는 cs-top div와 isHero 플래그"])
        self.assertEqual(len(keep), 2)
