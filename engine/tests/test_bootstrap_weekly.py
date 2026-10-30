"""bootstrap: 주간 재진단·설치 이후 실측·사용자 단위 설정 점검 (합성 데이터, LLM은 모킹)."""

import datetime as _dt
import json
import os
from unittest import mock

from tests.util import HarnessTestCase
from vmc_harness.bootstrap import diagnose, facts, handlers, remeasure, report, sessions_input, user_setup, weekly
from vmc_harness.core import config, ledger, notices
from vmc_harness.core.clock import now
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.hooks.contract import HANDLER_MODULES, HookContext

SECRET_LINE = "배포 전에는 반드시 전체 E2E를 세 번 돌린다 token=abcd1234efgh"
HOOK_CMD = "python3 /Users/kim/bin/slow-verify.py --all"

GOOD = {"summary": {"headline": "검증 고리가 문제", "observations": ["재확인 12%"], "recommendation": "SQL부터",
                    "since_last": "설치 뒤 개입이 6에서 3으로 줄었습니다"},
        "proposals": [{"title": "SQL 파일은 SQL/ 아래에만", "kind": "gate", "evidence": "세션 12건", "blocks": "엉뚱한 곳 SQL",
                       "scope": "*.sql 쓰기", "cost": "0.1초",
                       "condition": {"type": "write_outside", "ext": [".sql"], "allowed": ["SQL/**"]}}],
        "user_level": [{"title": "모든 Stop마다 도는 전체 검증 훅을 줄이기", "setting": "slow-verify.py (Stop 훅)",
                        "issue": "느린 게이트", "evidence": "지난 대화 2개에서 3회, 합 27초", "suggestion": "변경 파일만 검사"},
                       {"title": "빈 근거", "setting": "x", "evidence": "", "suggestion": "y"}]}


def _session(sid, at, prompts=3, skill=None, hooks=()):
    tools = [{"at": at, "name": "Read", "write": False, "paths": ["a.py"], "commands": []}]
    if skill:
        tools.append({"at": at, "name": "Skill", "write": False, "paths": [], "commands": [], "skill": skill})
    return {"session_id": sid, "agent": "claude", "kind": "main", "started_at": at, "ended_at": at, "measured": True,
            "human_prompts": [{"at": at, "text": "이거 고쳐줘 %d번" % i} for i in range(prompts)],
            "tool_calls": tools, "human_turns": prompts, "asks": 0, "tokens": 10, "hook_runs": list(hooks)}


class UserSetupTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.ch = self._tmp / "claude-home"
        self.xh = self._tmp / "codex-home"
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.ch)
        os.environ["CODEX_HOME"] = str(self.xh)
        self._write(self.ch / "skills" / "deep-review" / "SKILL.md",
                    "---\nname: deep-review\ndescription: |\n  모든 변경을 세 번 검토하고 멀티에이전트로 교차 검증한다\n---\n본문 비밀 절차\n")
        self._write(self.ch / "CLAUDE.md", "# 내 규칙\n\n" + SECRET_LINE + "\n")
        write_json(self.ch / "settings.json", {
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": HOOK_CMD}]}],
                      "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": HOOK_CMD}]}]},
            "enabledPlugins": {"guard@market": True, "off@market": False}})
        plug = self._tmp / "plugcache" / "guard"
        self._write(plug / "skills" / "tdd" / "SKILL.md", "---\nname: tdd\ndescription: 테스트 먼저\n---\n")
        write_json(plug / "hooks" / "hooks.json", {"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "node \"${CLAUDE_PLUGIN_ROOT}/hooks/inject.mjs\""}]}]}})
        write_json(self.ch / "plugins" / "installed_plugins.json", {"version": 2, "plugins": {
            "guard@market": [{"scope": "user", "installPath": str(plug), "version": "1.0.0"}],
            "other@market": [{"scope": "project", "projectPath": "/somewhere/else", "installPath": str(plug)}]}})
        self._write(self.xh / "AGENTS.md", "# 코덱스 규칙\n항상 영어로 답한다\n")
        self._write(self.xh / "config.toml", 'model = "gpt-5"\napi_key = "sk-secret"\n[projects."/Users/kim/p"]\ntrust_level = "trusted"\n'
                                              '[mcp_servers.db]\ncommand = "x"\n')

    def _write(self, p, text):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def test_hook_runs_from_records(self):
        recs = [{"type": "attachment", "attachment": {"type": "hook_success", "hookEvent": "SessionStart", "hookName": "SessionStart:startup",
                                                      "command": "node /x/hooks/inject.mjs", "durationMs": 286, "stdout": "긴 출력"}},
                {"type": "attachment", "attachment": {"type": "hook_non_blocking_error", "hookEvent": "PostToolUse",
                                                      "command": "bash .claude/hooks/q.sh", "durationMs": 5}},
                {"type": "system", "subtype": "stop_hook_summary", "preventedContinuation": True,
                 "hookInfos": [{"command": HOOK_CMD, "durationMs": 9000}]},
                {"type": "assistant", "message": {}}]
        runs = user_setup.hook_runs_from_records(recs)
        self.assertEqual([(r["event"], r["label"], r["ms"]) for r in runs],
                         [("SessionStart", "inject.mjs", 286), ("PostToolUse", "q.sh", 5), ("Stop", "slow-verify.py", 9000)])
        self.assertTrue(runs[1]["error"])
        self.assertTrue(runs[2]["blocked"])
        self.assertNotIn("kim", runs[2]["key"])  # 색인에 남는 열쇠는 홈 경로를 가린다
        self.assertEqual(user_setup.hook_label('[ -n "$X" ] && "$X/hooks/notify.sh" || true'), "notify.sh")
        self.assertEqual(user_setup.hook_label("argos hook"), "argos")

    def test_skill_of_and_session_load(self):
        self.assertEqual(user_setup.skill_of("Skill", {"skill": "commit", "args": "x"}), "commit")
        self.assertEqual(user_setup.skill_of("exec_command", {"cmd": "cat ~/.codex/skills/deep-review/SKILL.md"}), "deep-review")
        self.assertIsNone(user_setup.skill_of("Read", {"file_path": "a.py"}))
        tc = sessions_input.normalize_tool_call("Skill", {"skill": "deep-review"})
        self.assertEqual(tc["skill"], "deep-review")
        # 원본 jsonl 묶음 → 세션에 훅 실행 기록이 붙고, 세션 색인에도 남는다
        d = self._tmp / "export"
        d.mkdir()
        lines = [
            {"type": "user", "sessionId": "S1", "cwd": str(self.root), "timestamp": "2026-10-20T01:00:00Z",
             "message": {"role": "user", "content": "리뷰해줘"}},
            {"type": "assistant", "sessionId": "S1", "timestamp": "2026-10-20T01:00:05Z",
             "message": {"id": "m1", "usage": {"input_tokens": 1}, "content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "deep-review"}}]}},
            {"type": "system", "subtype": "stop_hook_summary", "timestamp": "2026-10-20T01:00:09Z",
             "hookInfos": [{"command": HOOK_CMD, "durationMs": 9000}]},
        ]
        (d / "s1.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")
        sess, _ = sessions_input.load(self.root, str(d))
        self.assertEqual(len(sess), 1)
        self.assertEqual(sess[0]["hook_runs"][0]["label"], "slow-verify.py")
        self.assertEqual([t.get("skill") for t in sess[0]["tool_calls"]], ["deep-review"])
        diagnose.write_sessions_index(self.root, sess)
        idx = diagnose.read_sessions_index(self.root)
        self.assertEqual(idx[0]["hook_runs"][0]["ms"], 9000)
        self.assertEqual(idx[0]["tool_calls"][0]["skill"], "deep-review")

    def test_collect_names_and_usage_only(self):
        key = user_setup._cmd_key(HOOK_CMD)
        sess = [_session("S1", "2026-10-20T01:00:00+00:00", skill="deep-review",
                         hooks=[{"event": "Stop", "label": "slow-verify.py", "key": key, "ms": 9000},
                                {"event": "Stop", "label": "slow-verify.py", "key": key, "ms": 9000}]),
                _session("S2", "2026-10-21T01:00:00+00:00", skill="guard:tdd",
                         hooks=[{"event": "Stop", "label": "slow-verify.py", "key": key, "ms": 9000},
                                {"event": "SessionStart", "label": "inject.mjs", "key": "node /real/path/hooks/inject.mjs", "ms": 300}])]
        r = user_setup.collect(self.root, sess)
        s = r["summary"]
        c = s["claude"]
        self.assertEqual([k["name"] for k in c["skills"]], ["deep-review"])
        self.assertEqual(c["skills"][0]["used"], {"calls": 1, "sessions": 1})
        self.assertIn("세 번 검토", c["skills"][0]["description"])
        stop = [h for h in c["hooks"] if h["event"] == "Stop"][0]
        self.assertEqual(stop["used"]["runs"], 3)
        self.assertEqual(stop["used"]["total_sec"], 27.0)
        pre = [h for h in c["hooks"] if h["event"] == "PreToolUse"][0]
        self.assertIsNone(pre["used"])  # 같은 명령이라도 이벤트가 다르면 따로 센다
        self.assertEqual(pre["matcher"], "Bash")
        plugs = {p["name"]: p for p in c["plugins"]}
        self.assertEqual(set(plugs), {"guard@market", "off@market"})  # 다른 프로젝트 범위 설치는 뺀다
        self.assertTrue(plugs["guard@market"]["enabled"])
        self.assertEqual(plugs["guard@market"]["skills"][0]["used"]["calls"], 1)  # 플러그인:스킬 이름으로도 잇는다
        self.assertEqual(plugs["guard@market"]["hooks"][0]["used"]["runs"], 1)  # 변수가 펼쳐진 명령은 (이벤트, 이름)으로
        self.assertEqual(c["claude_md"]["headings"], ["내 규칙"])
        cx = s["codex"]
        self.assertTrue(cx["agents_md"]["exists"])
        self.assertEqual(cx["config"]["top_keys"], ["model", "api_key"])
        self.assertEqual(cx["config"]["mcp_servers"], ["db"])
        dump = json.dumps(s, ensure_ascii=False)
        for leak in (SECRET_LINE, "sk-secret", "/Users/kim", "slow-verify.py --all", "본문 비밀 절차", "영어로 답한다"):
            self.assertNotIn(leak, dump)
        # 모델 참고용 원문은 로컬 자료로만 — 마스킹을 거친다
        self.assertIn("E2E를 세 번", r["docs_text"])
        self.assertNotIn("abcd1234efgh", r["docs_text"])


class WeeklyTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        os.environ["CLAUDE_CONFIG_DIR"] = str(self._tmp / "claude-home")
        os.environ["CODEX_HOME"] = str(self._tmp / "codex-home")
        ch = self._tmp / "claude-home"
        ch.mkdir()
        (ch / "CLAUDE.md").write_text("# 규칙\n" + SECRET_LINE + "\n", encoding="utf-8")
        write_json(ch / "settings.json", {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": HOOK_CMD}]}]}})
        self.write("SQL/a.sql", "select 1\n")
        self.commit("base")
        facts.collect(self.root, run_tests=False, use_llm=False)
        cfg = read_json(self.root / ".harness" / "config.json")
        cfg["created_at"] = "2026-10-01T00:00:00+09:00"
        write_json(self.root / ".harness" / "config.json", cfg)
        ledger.write_baseline(self.root, {"before": {"sessions": {"main_with_prompts": 4}, "human_prompts": 24,
                                                     "interventions_per_session": {"median": 6, "mean": 6.5}}})

    def _run(self, payload, sessions=None, trigger="manual"):
        fake = {"text": json.dumps(payload, ensure_ascii=False), "tokens": 100, "sec": 1.0}
        with mock.patch("vmc_harness.core.llm.available", return_value=True), \
                mock.patch("vmc_harness.core.llm.nested", return_value=False), \
                mock.patch("vmc_harness.core.llm.claude_agent", return_value=fake) as m, \
                mock.patch.object(diagnose, "render_proposal", return_value={}):
            d = diagnose.run(self.root, sessions=sessions, trigger=trigger)
        return d, m.call_args[0][0]

    def test_second_diagnosis_adds_measurements(self):
        d1, p1 = self._run(GOOD, sessions=[])
        self.assertNotIn("since_init", d1)
        self.assertIn("user_setup.json", p1)
        self.assertIn("사용자 단위", p1)
        self.assertNotIn("measured.json", p1)
        self.assertEqual([u["title"] for u in d1["user_level"]], ["모든 Stop마다 도는 전체 검증 훅을 줄이기"])  # 형식 틀린 건 뺀다
        self.assertEqual(len(d1["checks"]["user_level_dropped"]), 1)
        # 설치 이후 실측: 조치·판정·게이트 발동·작업 예측
        a = ledger.create_action(self.root, {"type": "explicit_request", "quote": "SQL 막아", "ref": "diagnose"}, "gate",
                                 "엉뚱한 곳 SQL", human_requested=True, target={"gate": "G1"})
        ledger.write_verdict(self.root, {"verdict_id": "V-1", "action_id": a["action_id"], "result": "kept", "at": now().isoformat()})
        day = now().strftime("%Y-%m-%d")
        write_json(ledger.metrics_path(self.root, day), {"date": day, "pc_id": "pc", "sessions": 2, "human_turns": 6,
                                                         "gates": {"G1": {"fired": 5, "blocked": 2, "false_block": 1, "sec": 0.5}},
                                                         "exposures": {a["action_id"]: 4}})
        ledger.append_task(self.root, {"task_id": "T-1", "started_at": now().isoformat(),
                                       "declare": {"predict": {"agent_sec": {"p30": 60, "p50": 120, "p80": 300}}},
                                       "actual": {"measured": True, "agent_sec": 200}})
        sess = [_session("S%d" % i, now().isoformat(), prompts=3) for i in range(3)]
        d2, p2 = self._run(GOOD, sessions=sess, trigger="weekly")
        m = d2["since_init"]
        self.assertIn("measured.json", p2)
        self.assertIn("다시 하는 진단", p2)
        self.assertTrue((diagnose.input_dir(self.root) / "measured.json").is_file())
        self.assertEqual(m["gates"]["blocked"], 2)
        self.assertEqual(m["gates"]["false_block"], 1)
        self.assertEqual(m["exposures"], 4)
        self.assertEqual(m["actions"]["total"], 1)
        self.assertEqual(m["verdicts"]["by_result"], {"kept": 1})
        self.assertEqual(m["tasks"]["in_range"], 1)
        self.assertEqual(m["metrics"]["before"]["interventions_per_session"]["median"], 6)
        self.assertEqual(m["metrics"]["now"]["interventions_per_session"]["median"], 3)
        self.assertEqual(m["metrics"]["now"]["sessions"], 3)
        self.assertEqual(d2["trigger"], "weekly")
        # 이미 보인 제안·사용자 단위 제안은 다시 내지 않는다
        self.assertEqual(d2["proposals"], [])
        self.assertEqual(d2["user_level"], [])
        self.assertIn("SQL 파일은 SQL/ 아래에만", p2)
        self.assertIn("모든 Stop마다 도는 전체 검증 훅을 줄이기", p2)
        # 세 번째: "지난 진단" 칸 = 직전 진단의 "지금"
        d3, _ = self._run(GOOD, sessions=sess)
        self.assertEqual(d3["since_init"]["metrics"]["previous"]["sessions"], 3)
        # [제외]한 제안도 다시 내지 않는다(history 그대로)
        hist = read_json(diagnose.history_path(self.root))
        self.assertIn("SQL 파일은 SQL/ 아래에만", [h["title"] for h in hist["shown"]])
        self.assertEqual(len(hist["user_shown"]), 1)

    def test_report_sections_names_only(self):
        self._run(GOOD, sessions=[])
        sess = [_session("S1", now().isoformat(), hooks=[{"event": "Stop", "label": "slow-verify.py",
                                                          "key": user_setup._cmd_key(HOOK_CMD), "ms": 9000}])]
        g2 = dict(GOOD, user_level=[dict(GOOD["user_level"][0], title="새 사용자 제안")])
        self._run(g2, sessions=sess)
        with mock.patch.object(report, "to_pdf", return_value=(None, "x")):
            r = report.build(self.root, open_browser=False)
        with open(r["html"], encoding="utf-8") as fh:
            h = fh.read()
        for s in ("지난 진단 이후", "하네스 전", "지난 진단", "설치 뒤 개입이 6에서 3으로", "사용자 단위 지침·훅", "새 사용자 제안",
                  "slow-verify.py", "하네스는 이 설정을 고치지 않습니다", "~/.claude/CLAUDE.md"):
            self.assertIn(s, h)
        for leak in (SECRET_LINE, "/Users/kim", "--all"):
            self.assertNotIn(leak, h)

    def test_due_ensure_job_notice(self):
        ref = now()
        ws = ref - _dt.timedelta(days=ref.weekday())
        ws = ws.replace(hour=0, minute=0, second=0, microsecond=0)
        with mock.patch("vmc_harness.core.llm.available", return_value=True), \
                mock.patch("vmc_harness.core.llm.nested", return_value=False):
            self.assertEqual(weekly.due(self.root), (False, "직전 진단 없음"))
            write_json(diagnose.diagnosis_path(self.root), {"status": "ok", "proposals": [],
                                                            "generated_at": (ws - _dt.timedelta(days=2)).isoformat()})
            self.assertEqual(weekly.due(self.root), (True, None))
            with mock.patch("vmc_harness.core.proc.spawn_detached") as sp:
                self.assertTrue(weekly.ensure(self.root))
                self.assertFalse(weekly.ensure(self.root))  # 실행 중이면 두 번 띄우지 않는다
            self.assertEqual(sp.call_count, 1)
            self.assertEqual(sp.call_args[0][0][-2:], ["diagnose", "weekly"])
            self.assertEqual(read_json(weekly.state_path(self.root))["status"], "running")
            with mock.patch.object(config, "get", side_effect=lambda k, r=None, profile=None:
                                   False if k == "diagnose.auto" else config.defaults()[0][k]):
                os.remove(str(weekly.state_path(self.root)))
                self.assertEqual(weekly.due(self.root)[0], False)
        # 본체: 모델은 모킹, PDF 없음 → 알림 한 줄(flow)
        fake = {"text": json.dumps(GOOD, ensure_ascii=False), "tokens": 100, "sec": 1.0}
        with mock.patch("vmc_harness.core.llm.available", return_value=True), \
                mock.patch("vmc_harness.core.llm.nested", return_value=False), \
                mock.patch("vmc_harness.core.llm.claude_agent", return_value=fake), \
                mock.patch.object(diagnose, "render_proposal", return_value={}), \
                mock.patch.object(report, "to_pdf", return_value=(None, "x")), \
                mock.patch.object(sessions_input, "load", return_value=([], {"main": 0})):
            st = weekly.job(self.root)
        self.assertEqual(st["status"], "ok")
        self.assertTrue(st["html"].replace("\\", "/").split("/.harness/cache/reports/")[1].startswith("diagnosis-"))
        ns = [n for n in notices.pending(self.root) if n["kind"] == "diagnosis_weekly"]
        self.assertEqual(len(ns), 1)
        self.assertEqual(ns[0]["level"], "flow")
        self.assertIn("새 제안 1개", ns[0]["text"])
        self.assertEqual(diagnose.load(self.root)["trigger"], "weekly")
        # 이번 기간(하루) 진단이 있으면 그 기간엔 다시 안 돈다
        os.remove(str(weekly.state_path(self.root)))
        with mock.patch("vmc_harness.core.llm.available", return_value=True), \
                mock.patch("vmc_harness.core.llm.nested", return_value=False), \
                mock.patch("vmc_harness.core.proc.spawn_detached") as sp:
            self.assertFalse(weekly.ensure(self.root))
        self.assertEqual(sp.call_count, 0)
        self.assertEqual(read_json(weekly.state_path(self.root))["status"], "recent")

    def test_session_start_handler_registered_and_fail_open(self):
        self.assertIn(("vmc_harness.bootstrap.handlers", False), HANDLER_MODULES)
        ctx = HookContext(event="SessionStart", agent="claude", raw={}, root=str(self.root))
        with mock.patch.object(weekly, "ensure", side_effect=RuntimeError("x")):
            self.assertIsNone(handlers.on_session_start(ctx))
        with mock.patch.object(weekly, "ensure", return_value=True) as en:
            handlers.on_session_start(ctx)
        en.assert_called_once_with(str(self.root))

    def test_config_keys(self):
        self.assertIs(config.get("diagnose.auto"), True)
        self.assertGreater(config.get("diagnose.user_doc_chars"), 0)
        self.assertGreater(config.get("diagnose.user_desc_chars"), 0)

    def test_remeasure_pick_keyword_only(self):
        b = {"sessions": {"main_with_prompts": 2}, "recheck_after_done_ratio": {"method": "tagged", "value": 0.2},
             "recheck_after_done_ratio_keyword": {"method": "keyword", "value": 0.1}}
        self.assertEqual(remeasure.pick(b)["recheck_after_done_ratio_keyword"]["value"], 0.1)
        b2 = {"recheck_after_done_ratio": {"method": "tagged", "value": 0.2}}
        self.assertNotIn("recheck_after_done_ratio_keyword", remeasure.pick(b2))


class PeriodTest(HarnessTestCase):
    def test_default_period_is_one_day(self):
        import datetime as _dt
        from vmc_harness.bootstrap import weekly
        from vmc_harness.core import config
        self.assertEqual(config.get("diagnose.every"), "day")
        d1 = _dt.datetime(2026, 10, 23, 9, 0).astimezone()
        d2 = _dt.datetime(2026, 10, 23, 23, 0).astimezone()
        d3 = _dt.datetime(2026, 10, 24, 0, 30).astimezone()
        self.assertEqual(weekly._week(d1)[1], weekly._week(d2)[1])
        self.assertNotEqual(weekly._week(d2)[1], weekly._week(d3)[1])

    def test_week_period_still_available(self):
        import datetime as _dt
        from unittest import mock
        from vmc_harness.bootstrap import weekly
        from vmc_harness.core import config
        orig = config.get
        with mock.patch.object(config, "get", side_effect=lambda k, r=None: "week" if k == "diagnose.every" else orig(k, r)):
            a = weekly._week(_dt.datetime(2026, 10, 19, 9, 0).astimezone())[1]
            b = weekly._week(_dt.datetime(2026, 10, 23, 9, 0).astimezone())[1]
        self.assertEqual(a, b)
