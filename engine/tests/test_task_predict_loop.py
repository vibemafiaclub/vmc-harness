"""예측 학습 루프(spec 13): 단계 나누기·캐시 재생성·이어받기·원인 판정·티켓·숫자 판정·solo 프로파일. 합성 데이터만."""

import datetime as _dt
import json
from pathlib import Path

from tests.util import HarnessTestCase
from vmc_harness.core import config, ledger
from vmc_harness.core.jsonio import write_json
from vmc_harness.sessions import first_uuid
from vmc_harness.sessions import claude as claude_parser
from vmc_harness.task import analysis, lifecycle, measure, phases
from vmc_harness.task import state as st

T0 = _dt.datetime(2026, 11, 5, 12, 0, 0, tzinfo=_dt.timezone.utc)


def _at(sec):
    return (T0 + _dt.timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")


def _user(sec, text, uid):
    return {"type": "user", "uuid": uid, "sessionId": "S", "timestamp": _at(sec), "turnOrigin": "human",
            "message": {"role": "user", "content": text}}


def _tool(sec, mid, tid, name, inp, usage=None):
    return {"type": "assistant", "uuid": "a-" + tid, "sessionId": "S", "timestamp": _at(sec),
            "message": {"id": mid, "role": "assistant", "usage": usage or {"input_tokens": 1, "output_tokens": 10},
                        "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def _result(sec, tid):
    return {"type": "user", "uuid": "r-" + tid, "sessionId": "S", "timestamp": _at(sec),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}


def _text(sec, mid, usage=None):
    return {"type": "assistant", "uuid": "t-" + mid, "sessionId": "S", "timestamp": _at(sec),
            "message": {"id": mid, "role": "assistant", "usage": usage or {"input_tokens": 1, "output_tokens": 5},
                        "content": [{"type": "text", "text": "끝"}]}}


class ClassifyTest(HarnessTestCase):
    def test_by_command_not_tool_name(self):
        c = lambda name, cmd=None: phases.classify({"name": name, "input": {"command": cmd} if cmd else {}})
        self.assertEqual(c("Bash", "cat > engine/a.py <<'EOF'\nx=1\nEOF"), "edit")
        self.assertEqual(c("Bash", "python3 - <<'PY'\npathlib.Path('a').write_text('x')\nPY"), "edit")
        self.assertEqual(c("Bash", "cd engine && python3 -m unittest discover -s tests"), "verify")
        self.assertEqual(c("Bash", "xcodebuild -scheme Void build"), "verify")
        self.assertEqual(c("Bash", "cd /x && git log --oneline -3"), "explore")
        self.assertEqual(c("Read"), "explore")
        self.assertEqual(c("Write"), "edit")
        self.assertEqual(c("Agent"), "delegate")
        self.assertEqual(c("TodoWrite"), "other")
        self.assertEqual(c("Bash", "cd /x && python3 -I - <<'PY'\nimport json\nprint(1)\nPY"), "explore")
        self.assertEqual(c("Bash", "git commit -m x && git push"), "other")
        self.assertEqual(c("mcp__docs__search"), "explore")
        long_write = {"name": "Bash", "input": {"command": "python3 - <<'PY'\n" + "x=1\n" * 1500 + "open('a','w').write('b')\nPY"}}
        self.assertEqual(phases.classify(long_write), "edit")


class ParseAndPhasesTest(HarnessTestCase):
    def _transcript(self, rows):
        p = self.root / "t.jsonl"
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        return p

    def test_phases_rework_and_cache_recreate(self):
        rows = [
            _user(0, "a.py 고쳐 줘", "u1"),
            _tool(10, "m1", "t1", "Read", {"file_path": str(self.root / "a.py")}),
            _result(20, "t1"),
            _tool(50, "m2", "t2", "Edit", {"file_path": str(self.root / "a.py"), "old_string": "a", "new_string": "b"}),
            _result(55, "t2"),
            _tool(60, "m3", "t3", "Bash", {"command": "python3 -m unittest"}),
            _result(160, "t3"),
            _text(170, "m4"),
            _user(5000, "아니 다르게", "u2"),
            # 1시간 넘게 지난 뒤 — 문맥 전체를 다시 쓴 응답
            _tool(5010, "m5", "t5", "Edit", {"file_path": str(self.root / "a.py"), "old_string": "b", "new_string": "c"},
                  usage={"input_tokens": 1, "output_tokens": 10, "cache_creation_input_tokens": 90000,
                         "cache_read_input_tokens": 1000, "cache_creation": {"ephemeral_1h_input_tokens": 90000}}),
            _result(5020, "t5"),
            _text(5030, "m6"),
        ]
        s = claude_parser.parse(self._transcript(rows))
        self.assertTrue(s.measured)
        self.assertEqual(s.token_parts.get("cache_recreate"), 90000)
        self.assertEqual(s.tool_calls[0]["end"][:19], _at(20)[:19])
        ph, det = phases.compute(s)
        self.assertEqual(ph["explore"], 20.0)   # 프롬프트~Read 결과
        self.assertEqual(ph["edit"], 35.0)      # Read 결과~Edit 결과
        self.assertEqual(ph["verify"], 105.0)   # Edit 결과~테스트 결과
        self.assertEqual(ph["rework"], 20.0)    # 두 번째 메시지 뒤 같은 파일 다시 고침
        self.assertEqual(ph["answer"], 20.0)    # 마지막 결과~턴 끝(두 턴 10초씩)
        self.assertAlmostEqual(sum(ph.values()), s.agent_sec, delta=0.5)
        self.assertEqual(det["calls"], 4)

    def test_first_uuid_matches_resumed_copy(self):
        a = self._transcript([_user(0, "x", "same-uuid"), _text(5, "m1")])
        b = self.root / "b.jsonl"
        b.write_text(a.read_text(encoding="utf-8") + "\n" + json.dumps(_user(100, "y", "u9")), encoding="utf-8")
        self.assertEqual(first_uuid(a), "same-uuid")
        self.assertEqual(first_uuid(b), "same-uuid")


class AdoptResumedTest(HarnessTestCase):
    def test_task_moves_to_new_session_and_measures_last_file(self):
        old_p, new_p = self.root / "old.jsonl", self.root / "new.jsonl"
        old_p.write_text(json.dumps(_user(0, "x", "U1")), encoding="utf-8")
        new_p.write_text(json.dumps(_user(0, "x", "U1")) + "\n" + json.dumps(_user(60, "y", "U2")), encoding="utf-8")
        st.session_touch(self.root, "OLD", "claude", str(old_p))
        st.session_touch(self.root, "NEW", "claude", str(new_p))
        st.save(self.root, {"task_id": "T-1", "session_id": "OLD", "status": "open", "started_at": _at(0)})
        t = lifecycle.adopt_resumed(self.root, "NEW", str(new_p))
        self.assertEqual(t["session_id"], "NEW")
        self.assertEqual(t["session_ids"], ["OLD", "NEW"])
        self.assertEqual(st.by_id(self.root, "T-1")["session_id"], "NEW")
        self.assertIsNone(lifecycle.adopt_resumed(self.root, "NEW", str(new_p)))  # 이미 옮겼으면 다시 하지 않는다


def _task(tid, verify=60.0, edit=100.0, sec=300.0, p80=400.0, in_scope=1.0, rev=1, tokens=1000, tok_p80=5000,
          recreate=0, files=("app/a.py",), ended=_at(0)):
    return {"task_id": tid, "ended_at": ended, "declare_rev": rev,
            "declare": {"intent": "app 화면 고치기", "scope": ["app/"],
                        "predict": {"agent_sec": {"p30": 100, "p50": 200, "p80": p80},
                                    "tokens": {"p30": 1, "p50": 2, "p80": tok_p80}}},
            "actual": {"measured": True, "agent_sec": sec, "tokens": tokens,
                       "tokens_detail": {"cache_recreate": recreate} if recreate else {}},
            "phases": {"explore": 50.0, "edit": edit, "verify": verify, "rework": 0.0, "delegate": 0.0, "other": 10.0},
            "phases_detail": {"explore_late": 0.0},
            "scope_cache": {"files": list(files), "touch": {"in_scope": in_scope}}}


class AnalysisTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.base = [_task("T-a"), _task("T-b"), _task("T-c")]

    def cause(self, t, metric="agent_sec"):
        return analysis.analyze(self.root, t, self.base + [t])["cause"][metric]

    def test_causes(self):
        self.assertIsNone(self.cause(_task("T-ok")))                              # P80 안 — 원인 없음
        self.assertEqual(self.cause(_task("T-v", verify=600, sec=900)), "verify")
        self.assertEqual(self.cause(_task("T-s", edit=700, sec=900, in_scope=0.2)), "structure")
        self.assertEqual(self.cause(_task("T-e", edit=700, sec=900)), "edit")
        self.assertEqual(self.cause(_task("T-p", sec=500)), "predict")             # 단계는 평소대로, 숫자만 낮았다
        self.assertEqual(self.cause(_task("T-r", sec=900, rev=2)), "requirement")
        self.assertEqual(self.cause(_task("T-k", tokens=90000, recreate=80000), "tokens"), "cache")
        lone = _task("T-u", verify=600, sec=900)
        self.assertEqual(analysis.analyze(self.root, lone, [lone])["cause"]["agent_sec"], "unknown")

    def test_tickets_after_two_same_misses_only_once(self):
        ts = [dict(_task("T-%d" % i, verify=600, sec=900), analysis=None) for i in range(2)]
        for t in ts:
            t["analysis"] = analysis.analyze(self.root, t, self.base + ts)
        self.assertEqual(analysis.make_tickets(self.root, ts[:1]), [])             # 한 번은 아무것도 안 바꾼다
        made = analysis.make_tickets(self.root, ts)
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0]["kind"], "predict")
        self.assertEqual(made[0]["cause"], "verify")
        self.assertEqual(made[0]["baseline_excess"], 540.0)
        self.assertEqual(analysis.make_tickets(self.root, ts), [])                 # 열린 티켓이 있으면 다시 안 만든다

    def test_run_disabled_unless_profile(self):
        self.assertEqual(analysis.run(self.root), {"enabled": False})

    def test_weekly_hint_one_line(self):
        t = _task("T-r", sec=900, rev=2, ended=_at(10))
        t["analysis"] = analysis.analyze(self.root, t, self.base + [t])
        hint = analysis.weekly_hint([t], T0, T0 + _dt.timedelta(days=7))
        self.assertIn("1건", hint)


class JudgeTest(HarnessTestCase):
    def _action(self, layer="structure", key="app|verify"):
        return ledger.create_action(
            self.root, {"type": "predict_miss", "ref": "C-1", "key": key}, layer, "검증 초과가 줄어든다",
            why_not_higher="x", target={"predict": key}, status="trial",
            extra={"merged_at": _at(0), "predict": {"key": "app|verify", "cause": "verify", "phase": "verify",
                                                    "area": "app", "baseline_excess": 500.0}})

    def _ledger_task(self, tid, excess, ended):
        ledger.write_task(self.root, {"task_id": tid, "status": "ended", "ended_at": ended,
                                      "analysis": {"excess": {"verify": excess}, "area": ["app"]}})

    def test_kept_reverted_extended(self):
        from vmc_harness.ledger import judge
        need = config.get("judge.exposures", self.root)
        a = self._action()
        for i in range(need):
            self._ledger_task("T-%d" % i, 100.0, _at(100 + i))
        res, why, nums = judge.evaluate(self.root, a, [], [], at=T0 + _dt.timedelta(days=1))
        self.assertEqual(res, "kept")
        self.assertEqual(nums["excess_after"], 100.0)
        for i in range(need):
            self._ledger_task("T-%d" % i, 900.0, _at(100 + i))
        self.assertEqual(judge.evaluate(self.root, a, [], [], at=T0 + _dt.timedelta(days=1))[0], "reverted")
        b = self._action(layer="code", key="other|verify")
        far = T0 + _dt.timedelta(days=config.get("judge.max_days", self.root) + 30)
        b["predict"]["area"] = "other"  # 노출 0인 다른 영역
        self.assertEqual(judge.evaluate(self.root, b, [], [], at=far)[0], "reverted")
        c = dict(a, predict=dict(a["predict"], area="other"))
        self.assertEqual(judge.evaluate(self.root, c, [], [], at=far)[0], "extended")

    def test_meta_gate_needs_ref_and_key(self):
        with self.assertRaises(ledger.MetaGateError):
            ledger.create_action(self.root, {"type": "predict_miss"}, "structure", "x")


class ProfileAndSurfaceTest(HarnessTestCase):
    def test_solo_profile(self):
        self.assertFalse(config.get("predict_loop.enabled", self.root))
        self.assertFalse(config.get("loop.structure_direct", self.root))
        write_json(self.root / ".harness" / "config.json", {"project_id": self.project_id, "profile": "solo",
                                                           "source_roots": ["."]})
        self.assertTrue(config.get("predict_loop.enabled", self.root))
        self.assertTrue(config.get("loop.structure_direct", self.root))
        self.assertTrue(config.get("loop.enabled", self.root))

    def test_estimate_line_shows_end(self):
        d = {"predict": {"agent_sec": {"p30": 300, "p50": 480, "p80": 900}}, "end": "커밋까지"}
        self.assertEqual(measure.estimate_line(d), "예상 8~15분 · 끝: 커밋까지")
        d.pop("end")
        self.assertEqual(measure.estimate_line(d), "예상 8~15분")
