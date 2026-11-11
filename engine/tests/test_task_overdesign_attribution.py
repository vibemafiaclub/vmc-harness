"""과설계 점검 0.1.9 — 다른 세션 파일 제외, 선언 경로 대조 넓히기, LLM 호출 토큰 기록 (spec 03 §과설계 점검).

현장 결함(사용 데이터 6일): 과설계 발동 11건 중 실제로 줄인 건 1건. 6건은 선언에 경로를 문장·상대경로·파일 이름만·
선언 폴더 아래로 적어 못 알아본 것, 2건은 다른 세션 파일(늦게 읽힌 LLM 판정)이었다. 픽스처는 합성 데이터만.
"""

import json
import os
from unittest import mock

from tests.util import HarnessTestCase

from vmc_harness.core import events
from vmc_harness.core.jsonio import write_json
from vmc_harness.task import declare as dec, overdesign, pm, state as st


class _Base(HarnessTestCase):
    def setUp(self):
        super().setUp()
        for k in st.SESSION_ENV:
            os.environ.pop(k, None)
        self.write("app/billing.py", "def total(xs):\n    return sum(xs)\n")
        self.write("docs/index.md", "# docs\n")
        self.commit()
        st.session_touch(self.root, "S1", "claude")
        p = mock.patch("vmc_harness.task.pm.start", return_value="sync")  # 실제 claude -p를 띄우지 않는다
        p.start()
        self.addCleanup(p.stop)

    def _task(self, **kw):
        d = {"intent": "합성 작업", "scope": ["app/billing.py"],
             "predict": {"tokens": [1, 2, 3], "agent_sec": [60, 120, 170]}}
        d.update(kw)
        return dec.declare(self.root, d, session_id="S1")["task"]

    def _new_signals(self, task):
        task = st.by_id(self.root, task["task_id"]) or task
        return {(s["kind"], s.get("path")) for s in overdesign.machine_signals(self.root, task)
                if s["kind"].startswith("new_")}


class DeclaredPathTest(_Base):
    def test_path_inside_sentence(self):
        t = self._task(new=["새 페이지 projects/site/page.md 를 만든다"])
        self.agent_write("projects/site/page.md", "x\n")
        self.assertEqual(self._new_signals(t), set())

    def test_filename_only_and_number_range(self):
        # --new "page.md, fig1~5.html" 로 선언하고 projects/.../ 아래에 만든 경우
        t = self._task(new="page.md, fig1~5.html")
        for rel in ("projects/site/s4/page.md", "projects/site/s4/fig1.html", "projects/site/s4/fig5.html"):
            self.agent_write(rel, "x\n")
        self.assertEqual(self._new_signals(t), set())
        self.agent_write("projects/site/s4/fig6.html", "x\n")
        self.assertIn(("new_file_out_of_scope", "projects/site/s4/fig6.html"), self._new_signals(t))

    def test_relative_to_declared_dir(self):
        t = self._task(scope=["projects/site/s4/notes.md"], new=["logs/261110_report.md"])
        self.agent_write("projects/site/s4/logs/261110_report.md", "x\n")
        self.assertEqual(self._new_signals(t), set())

    def test_under_declared_scope_glob_or_dir(self):
        t = self._task(scope=["projects/site/**", "web/pages/"])
        self.agent_write("projects/site/a/b.md", "x\n")
        self.agent_write("web/pages/new.tsx", "x\n")
        self.assertEqual(self._new_signals(t), set())
        self.agent_write("projects/other/c.md", "x\n")
        self.assertIn(("new_file_out_of_scope", "projects/other/c.md"), self._new_signals(t))

    def test_absolute_path_and_dir_in_sentence(self):
        t = self._task(new=[str(self.root / "notes" / "a.md"),
                            "projects/news/item/ (index.md + 해시 이름 이미지 34개", "publish.py 산출물)"])
        self.agent_write("notes/a.md", "x\n")
        self.agent_write("projects/news/item/ab12cd.jpg", "x\n")
        self.agent_write("projects/news/item/index.md", "x\n")
        self.assertEqual(self._new_signals(t), set())

    def test_bare_words_and_repo_wide_scope_do_not_declare_everything(self):
        t = self._task(scope=["app/billing.py", "."], new=["문서", "새 페이지", "**"])
        self.agent_write("misc/stray.md", "x\n")
        k = self._new_signals(t)
        self.assertIn(("new_file_out_of_scope", "misc/stray.md"), k)
        self.assertIn(("new_module_out_of_scope", "misc"), k)

    def test_bare_word_that_is_a_root_dir_counts(self):
        t = self._task(scope=["docs"])
        self.agent_write("docs/guide.md", "x\n")
        self.assertEqual(self._new_signals(t), set())

    def test_path_tokens(self):
        toks = overdesign.path_tokens(self.root, "새 index.md를 projects/x/ 아래에, fig2~3.png")
        self.assertEqual(toks, ["index.md", "projects/x/", "fig2.png", "fig3.png"])
        self.assertEqual(overdesign.path_tokens(self.root, "문서 정리"), [])
        self.assertEqual(overdesign.path_tokens(self.root, "/elsewhere/tmp/a.md"), [])


class OtherSessionTest(_Base):
    def test_file_written_by_another_session_is_not_judged(self):
        t = self._task()
        self.agent_write("app/billing.py", "def total(xs):\n    return sum(xs) or 0\n")
        # 같은 작업 트리의 다른 세션이 만든 파일 — 이 작업의 편집 기록에 없다
        self.write("playbooks/other-session.md", "x\n")
        self.assertEqual(self._new_signals(t), set())

    def test_late_llm_result_about_other_files_is_dropped(self):
        t = self._task()
        self.agent_write("app/billing.py", "def total(xs):\n    return sum(xs) or 0\n")
        # 예전 귀속으로 만든 판정이 몇 시간 뒤에 읽힌 경우 — 남의 파일에 대한 신호
        write_json(overdesign.od_path(self.root, t["task_id"]), {"status": "done", "tokens": 5, "signals": [
            {"kind": "unrequested_option", "path": "playbooks/other-session.md", "name": "남의 옵션"},
            {"kind": "unrequested_option", "path": "app/billing.py", "name": "요청에 없는 옵션"},
            {"kind": "unrequested_option", "path": None, "name": "경로 없는 신호"}]})
        rec = overdesign.read_llm(self.root, t["task_id"], ["app/billing.py"])
        self.assertEqual([s["name"] for s in rec["signals"]], ["요청에 없는 옵션", "경로 없는 신호"])
        # files를 안 주면 그대로(옛 호출)
        self.assertEqual(len(overdesign.read_llm(self.root, t["task_id"])["signals"]), 3)


class TokenEventTest(_Base):
    def test_overdesign_llm_logs_tokens_and_drops_foreign_paths(self):
        t = self._task()
        self.agent_write("app/billing.py", "def total(xs, opt=None):\n    return sum(xs)\n")
        out = json.dumps({"signals": [{"what": "opt 인자", "path": "app/billing.py"},
                                      {"what": "남의 것", "path": "playbooks/other-session.md"}]})
        with mock.patch("vmc_harness.core.llm.claude_p", return_value={"text": out, "tokens": 321, "sec": 2.5}):
            rec = overdesign.run_llm(self.root, t["task_id"])
        self.assertEqual([s["path"] for s in rec["signals"]], ["app/billing.py"])
        ev = events.read(self.root, "overdesign_llm")
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["task_id"], ev[0]["tokens"], ev[0]["sec"], ev[0]["signals"]),
                         (t["task_id"], 321, 2.5, 1))

    def test_pm_event_has_tokens(self):
        t = self._task()
        with mock.patch("vmc_harness.task.pm.run_pm",
                        return_value={"opinion": "approve", "wish": None, "tokens": 777, "sec": 1.0}), \
                mock.patch("vmc_harness.task.pm.run_cards", return_value=[]):
            pm.run_bundle(self.root, t["task_id"])
        ev = events.read(self.root, "pm")
        self.assertEqual(ev[-1]["tokens"], 777)
        self.assertIn("sec", ev[-1])
