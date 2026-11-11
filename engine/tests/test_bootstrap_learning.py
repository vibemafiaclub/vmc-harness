"""bootstrap: init의 학습 후보 추출은 분리된 백그라운드(learn-bg)로 돈다 — init은 기다리지 않고, 끝나면 완료 기록·알림(합성 데이터)."""

import io
import os
import sys
import time
from contextlib import redirect_stdout
from unittest import mock

from tests.util import HarnessTestCase
from vmc_harness.bootstrap import baseline, init, report
from vmc_harness.core import events, notices
from vmc_harness.core.jsonio import read_json, write_json

SESSIONS = 8
SLEEP = 1.5  # 가짜 claude가 세션 하나 해석에 쓰는 초 — 동기라면 init이 SESSIONS × SLEEP 이상 걸린다

FAKE = r'''
import json, os, re, sys, time
p = sys.stdin.read()
res = "{}"
if p.startswith("사람 발화:"):
    time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
    m = re.search(r"^0\. (.+)$", p, re.M)
    c = re.search(r"^(\S+): ", p.split("기존 학습 후보:", 1)[1], re.M)
    res = json.dumps({"corrections": 1, "learnings": [{
        "same_as": c.group(1) if c else None, "gist": "합성 취지", "title": "합성 카드", "situation": "s", "judgment": "j",
        "signals": ["x"], "not_when": "n", "cross_project": True, "why_not_higher": "상황마다 다르다",
        "prompt_index": 0, "quote": (m.group(1) if m else "x")[:6]}], "explicit_requests": []}, ensure_ascii=False)
sys.stdout.write(json.dumps({"result": res, "usage": {"input_tokens": 1}}))
'''


def _export(i):
    msgs = [{"role": "user", "timestamp": "2026-10-%02dT10:0%d:00Z" % (i + 1, j), "content": "합성 발화 %d번 세션 %d번 정정" % (i, j)}
            for j in range(3)]
    return {"id": "S%02d" % i, "agent": "claude", "_source": {"cwd": "/w"}, "title": "t", "messages": msgs,
            "startedAt": "2026-10-%02dT10:00:00Z" % (i + 1), "endedAt": "2026-10-%02dT11:00:00Z" % (i + 1)}


class InitLearningBackgroundTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.write("app/main.py", "def main():\n    pass\n")
        self.commit("base")
        bindir = self._tmp / "bin"
        bindir.mkdir()
        script = bindir / "fake_claude.py"
        script.write_text(FAKE, encoding="utf-8")
        if sys.platform == "win32":
            (bindir / "claude.cmd").write_text('@"%s" "%s" %%*\r\n' % (sys.executable, script), encoding="utf-8")
        else:
            sh = bindir / "claude"
            sh.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script), encoding="utf-8")
            sh.chmod(0o755)
        os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")
        os.environ["FAKE_CLAUDE_SLEEP"] = str(SLEEP)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self._tmp / "claude")
        self.sessions_dir = self._tmp / "sessions"
        for i in range(SESSIONS):
            write_json(self.sessions_dir / ("S%02d.json" % i), _export(i))

    def _init(self, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(report, "to_pdf", return_value=(None, "x")):
            t0 = time.time()
            s = init.run(self.root, dry_system=True, run_tests=False, do_diagnose=False, open_report=False,
                         sessions_dir=str(self.sessions_dir), out=print, **kw)
        return s, time.time() - t0, buf.getvalue()

    def _wait_done(self, limit=90):
        end = time.time() + limit
        while time.time() < end:
            st = read_json(init.learning_path(self.root), {}) or {}
            if st.get("status") in ("done", "error"):
                return st
            time.sleep(0.2)
        self.fail("백그라운드 학습이 %d초 안에 끝나지 않음: %s" % (limit, read_json(init.learning_path(self.root), {})))

    def test_init_does_not_wait_and_background_records_completion(self):
        s, sec, out = self._init()
        lr = s["steps"]["learning"]
        self.assertEqual(lr["status"], "async", out)
        self.assertEqual(lr["total"], SESSIONS)
        self.assertLess(sec, SESSIONS * SLEEP)  # 동기였다면 최소 SESSIONS × SLEEP초
        self.assertIn("백그라운드로 시작", out)
        self.assertTrue(s["report"]["html"])  # 학습을 기다리지 않고 리포트가 나온다
        st = read_json(init.learning_path(self.root))
        self.assertEqual(st["status"], "running")
        self.assertEqual([e["sessions"] for e in events.read(self.root, "learning_started")], [SESSIONS])

        st = self._wait_done()
        self.assertEqual(st["status"], "done", st)
        r = st["result"]
        self.assertEqual((r["sessions"], r["failed"], st["done"]), (SESSIONS, 0, SESSIONS))
        self.assertGreaterEqual(r["cards"], 1)  # 같은 취지 2회 → 카드
        done = events.read(self.root, "learning_done")
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["cards"], r["cards"])
        self.assertEqual(done[0]["tokens"], SESSIONS)  # 가짜 claude가 호출마다 usage 1토큰 — 학습 LLM 호출 합계
        n = [x for x in notices.pending(self.root) if x["kind"] == "learning_done"]
        self.assertEqual(len(n), 1)
        self.assertIn("배운 판단 %d개" % r["cards"], n[0]["text"])
        self.assertIn("세션 %d개" % SESSIONS, n[0]["text"])

        # 다시 init — 이미 해석한 세션은 다시 세지 않는다
        s2, _, out2 = self._init()
        self.assertEqual(s2["steps"]["learning"]["status"], "nothing", out2)

    def test_no_llm_and_running_job_are_respected(self):
        s, _, out = self._init(use_llm=False)
        self.assertEqual(s["steps"]["learning"], {"status": "skipped", "reason": "--no-llm"})
        self.assertFalse(init.learning_path(self.root).exists())
        # 살아 있는 작업이 있으면 하나 더 띄우지 않는다
        from vmc_harness.core.clock import now_iso
        write_json(init.learning_path(self.root), {"status": "running", "updated_at": now_iso(), "total": 3, "done": 1})
        with mock.patch("vmc_harness.core.proc.spawn_detached") as sp:
            s, _, out = self._init()
        sp.assert_not_called()
        self.assertEqual(s["steps"]["learning"]["status"], "running")
        # 오래 멈춘 기록은 죽은 것으로 보고 다시 띄운다
        write_json(init.learning_path(self.root), {"status": "running", "updated_at": "2020-01-01T00:00:00+00:00"})
        with mock.patch("vmc_harness.core.proc.spawn_detached") as sp:
            s, _, _ = self._init()
        sp.assert_called_once()
        self.assertIn("learn-bg", sp.call_args[0][0])
        self.assertEqual(s["steps"]["learning"]["status"], "async")


class BaselineRepeatedTest(HarnessTestCase):
    def test_prefix_filter_matches_pairwise(self):
        """접두 필터 결과 = 모든 앞선 프롬프트와 짝지은 전수 비교 결과."""
        import random

        def pairwise(sessions):
            items = []
            for s in sessions:
                for p in s.get("human_prompts") or []:
                    t = p.get("text") or ""
                    if len(t.strip()) >= baseline.MIN_PROMPT_CHARS:
                        items.append((p.get("at") or "", s.get("session_id"), baseline._shingles(t)))
            items.sort(key=lambda x: x[0])
            c = 0
            for i, (_, sid, sh) in enumerate(items):
                for _, sid2, sh2 in items[:i] if sh else []:
                    if sid2 != sid and sh2 and len(sh & sh2) / float(len(sh | sh2)) >= baseline.JACCARD_MIN:
                        c += 1
                        break
            return c

        words = "정렬 고쳐줘 테스트 다시 확인 버튼 화면 오류 저장 a b abc".split()
        for seed in range(150):
            r = random.Random(seed)
            base = [" ".join(r.choice(words) for _ in range(r.randint(2, 8))) for _ in range(5)]
            sessions = []
            for _ in range(r.randint(1, 8)):
                hp = [{"at": "2026-10-%02d" % r.randint(1, 3),
                       "text": r.choice(base) + (" " + r.choice(words) if r.random() < 0.5 else "")}
                      for _ in range(r.randint(0, 6))]
                sessions.append({"session_id": "S%d" % r.randint(0, 3), "human_prompts": hp})
            self.assertEqual(baseline.repeated_instructions(sessions), pairwise(sessions), seed)
