"""진단 자동 반영 흐름 (spec 07 §자동 반영 · 04 §판정 — 합성 데이터, LLM 없음).

진단 결과 JSON → 기계 검사 → 자동 반영(trial) → 부작용 지표 → 자동 원복 / 충돌 칸 있는 제안 제외 / 같은 대상 묶기 /
느슨하게 하는 제안 제외 / 사람 미커밋 변경과 겹치면 그날 건너뜀 / gxp 끔.
"""

import datetime as _dt
import json
from unittest import mock

from tests.test_loop_util import CALC, LoopTestCase
from vmc_harness.bootstrap import autoapply, diagnose
from vmc_harness.core import ledger, notices
from vmc_harness.core.clock import now
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.project import local_dir

OK_FIELDS = {"human_rule_conflict": {"conflicts": False, "with": "", "quote": ""}, "direction": "tighten"}
SIG_TSC = {"type": "command", "regex": r"npx\s+tsc"}


def P(title, kind, **kw):
    p = {"title": title, "kind": kind, "evidence": "12번", "blocks": "b", "scope": "s", "cost": "c"}
    p.update(OK_FIELDS)
    p.update(kw)
    return p


def sess(sid, at, commands=(), hook_errors=0):
    return {"session_id": sid, "kind": "main", "started_at": at, "human_prompts": [],
            "tool_calls": [{"at": at, "name": "Bash", "write": False, "paths": [], "commands": [c]} for c in commands],
            "hook_runs": [{"event": "PreToolUse", "label": "check.sh", "key": "check.sh", "ms": 1, "error": True,
                           "blocked": False}] * hook_errors}


class AutoApplyCase(LoopTestCase):
    def setUp(self):
        super().setUp()
        self.write("calc.py", CALC)
        self.write("AGENTS.md", "# 사람 지침\n\n- 이 레포 세션에서 다른 레포도 고쳐 커밋까지 끝낸다\n")
        self.write(".gitignore", ".harness/cache/\n")
        cfg = read_json(self.root / ".harness/config.json") or {}
        write_json(self.root / ".harness/config.json", dict(cfg, profile="solo"))  # 자동 반영 기본 켜짐은 solo만
        self.base = self.commit_old("init")
        t0 = (now() - _dt.timedelta(days=3)).isoformat(timespec="seconds")
        self.sessions([sess("S%d" % i, t0, ["npx tsc --noEmit -p apps/www"] if i % 2 else ["ls"], hook_errors=1)
                       for i in range(6)])

    def sessions(self, rows, append=False):
        p = local_dir(self.root) / "sessions-index.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a" if append else "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def diagnosis(self, props):
        for i, p in enumerate(props, 1):
            p["no"] = i
        d = {"generated_at": "2026-11-16T01:00:00+09:00", "status": "ok", "proposals": props}
        write_json(diagnose.diagnosis_path(self.root), d)
        write_json(diagnose.history_path(self.root), {"shown": [{"title": p["title"], "at": d["generated_at"],
                                                                 "sig": autoapply.signature(p)} for p in props],
                                                      "excluded": [], "accepted": []})
        return d

    def rows(self, res):
        return {r["title"]: r for r in res["rows"]}


class ChecksTest(AutoApplyCase):
    def test_applies_only_proposals_passing_every_check(self):
        gate_bt = {"would_block": 3, "problem_changes": 1, "scenes": []}
        self.diagnosis([
            P("타입 검사는 정해 둔 명령으로만 돌린다", "rule", rule_text="타입 검사는 `npm run typecheck`로만 돌린다.", signal=SIG_TSC),
            P("다른 레포 커밋 금지", "rule", rule_text="다른 레포는 고치지 않는다.", signal=SIG_TSC,
              human_rule_conflict={"conflicts": True, "with": "AGENTS.md:3", "quote": "다른 레포도 고쳐 커밋까지 끝낸다"}),
            dict(P("충돌 칸 없는 규칙", "rule", rule_text="무엇을 한다.", signal=SIG_TSC), human_rule_conflict=None),
            P("되돌릴 수 있는 작업은 진행 허락을 묻지 않고 끝까지 하기", "rule", rule_text="진행 허락을 묻지 않는다.", signal=SIG_TSC),
            P("훅을 끈다", "rule", rule_text="훅을 쓴다.", signal=SIG_TSC, direction="loosen"),
            P("임시 스크립트 쓰기 막기", "gate", condition={"type": "path_write", "globs": ["**/*.tmp.*"]},
              backtest={"would_block": 4, "problem_changes": 0, "scenes": []}),
            P("`send.py`가 모든 발송 기록을 남기게 하기", "structure", targets=["scripts/send.py"], signal=SIG_TSC),
            P("레포 밖 경로 쓰기 막기", "gate", condition={"type": "path_write", "globs": ["**/*.secret"]}, backtest=gate_bt),
            P("신호가 없던 규칙", "rule", rule_text="무엇을 한다.", signal={"type": "command", "regex": "never-ran"}),
            P("DB 직접 접속 막기", "gate", condition={"type": "db_direct"}, backtest=gate_bt),
        ])
        res = autoapply.apply(str(self.root), structure=False)
        r = self.rows(res)
        self.assertEqual(r["타입 검사는 정해 둔 명령으로만 돌린다"]["result"], "applied", r["타입 검사는 정해 둔 명령으로만 돌린다"])
        self.assertEqual(r["레포 밖 경로 쓰기 막기"]["result"], "applied", r["레포 밖 경로 쓰기 막기"])
        why = {t: " ".join(x["reasons"]) for t, x in r.items()}
        self.assertIn("사람 규칙과 충돌(AGENTS.md:3)", why["다른 레포 커밋 금지"])
        self.assertIn("충돌 칸 없음", why["충돌 칸 없는 규칙"])
        self.assertIn("푸는 말", why["되돌릴 수 있는 작업은 진행 허락을 묻지 않고 끝까지 하기"])
        self.assertIn("direction=loosen", why["훅을 끈다"])
        self.assertIn("실제 문제를 잡은 변경 0건", why["임시 스크립트 쓰기 막기"])
        self.assertIn("외부 발송", why["`send.py`가 모든 발송 기록을 남기게 하기"])
        self.assertIn("지난 대화 기록에 없음", why["신호가 없던 규칙"])
        self.assertIn("커널 안전 게이트", why["DB 직접 접속 막기"])
        self.assertEqual(sum(1 for x in r.values() if x["result"] == "applied"), 2)

        acts = [a for a in ledger.list_actions(self.root) if (a.get("basis") or {}).get("type") == "diagnosis_auto"]
        self.assertEqual(len(acts), 2)
        for a in acts:
            self.assertEqual(a["status"], "trial")
            self.assertFalse(a["human_requested"])  # 노출 부족이면 원복된다(사람 요청 예외 아님)
            self.assertTrue(all(a["basis"]["checks"].values()))
            self.assertNotIn("quote", a["basis"])
        rule_a = [a for a in acts if a["layer"] == "rule"][0]
        self.assertEqual(rule_a["auto"]["signal_before"]["hits"], 3)
        agents = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("진단이 자동으로 넣은 규칙", agents)
        self.assertNotIn("사람이 정한 규칙", agents)  # 자동 반영 규칙을 사람 원문으로 표시하지 않는다
        gate_a = [a for a in acts if a["layer"] == "gate"][0]
        meta = read_json(self.root / (".harness/gates/%s.json" % gate_a["target"]["gate_id"]))
        self.assertFalse(meta["human_requested"])
        lines = [n["text"] for n in notices.pending(self.root)]
        self.assertTrue(any("vmc-harness revert %s" % gate_a["action_id"] in t for t in lines), lines)
        d = diagnose.load(self.root)
        self.assertTrue(all(p.get("auto_checks") for p in d["proposals"]))
        # 다시 돌려도 두 번 반영하지 않는다, 사람 [추가]도 막는다
        again = self.rows(autoapply.apply(str(self.root), structure=False))
        self.assertEqual(again["레포 밖 경로 쓰기 막기"]["result"], "skip")
        self.assertEqual(diagnose.accept(self.root, [8])[0]["skipped"], "이미 자동 반영됨")
        # 수용률 분모에서 뺀다
        hist = read_json(diagnose.history_path(self.root))
        self.assertEqual(sum(1 for e in hist["shown"] if e.get("auto")), 2)
        # 패널 [제외] = 되돌리기
        out = diagnose.exclude(self.root, [8])
        self.assertEqual(out[0]["action_status"], "reverted")
        self.assertFalse(read_json(self.root / (".harness/gates/%s.json" % gate_a["target"]["gate_id"]))["enabled"])

    def test_protected_auto_and_revert_drops_entry(self):
        from vmc_harness.ledger import revert
        self.diagnosis([P("마이그레이션 폴더 보호", "protected", protected_globs=["db/migrations/**"],
                          backtest={"would_block": 0, "problem_changes": 0, "scenes": []})])
        r = autoapply.apply(str(self.root), structure=False)["rows"][0]
        self.assertEqual(r["result"], "applied", r)
        e = read_json(self.root / ".harness/protected.json")["paths"][0]
        self.assertEqual((e["glob"], e["basis"]["type"]), ("db/migrations/**", "diagnosis_auto"))
        self.assertNotIn("checks", e["basis"])
        a = ledger.get_action(self.root, r["action_id"])
        self.assertFalse(a["human_requested"])
        revert.revert_action(str(self.root), r["action_id"], "시험", by_human=False)
        self.assertEqual(read_json(self.root / ".harness/protected.json")["paths"], [])

    def test_dry_run_writes_nothing(self):
        self.diagnosis([P("타입 검사는 정해 둔 명령으로만 돌린다", "rule", rule_text="타입 검사는 `npm run typecheck`로만 돌린다.",
                          signal=SIG_TSC)])
        before = (diagnose.diagnosis_path(self.root)).read_text(encoding="utf-8")
        res = autoapply.dry_run(str(self.root))
        self.assertTrue(res["current"][0]["eligible"])
        self.assertEqual(diagnose.diagnosis_path(self.root).read_text(encoding="utf-8"), before)
        self.assertEqual(ledger.list_actions(self.root), [])

    def test_standard_profile_is_off_by_default(self):
        cfg = read_json(self.root / ".harness/config.json")
        write_json(self.root / ".harness/config.json", dict(cfg, profile="standard"))
        self.diagnosis([P("x 쓰기 막기", "gate", condition={"type": "path_write", "globs": ["**/*.x"]},
                          backtest={"would_block": 1, "problem_changes": 1, "scenes": []})])
        self.assertEqual(autoapply.after_diagnosis(str(self.root), diagnose.load(self.root)), {"enabled": False})

    def test_gxp_profile_turns_it_off(self):
        cfg = read_json(self.root / ".harness/config.json")
        write_json(self.root / ".harness/config.json", dict(cfg, profile="gxp"))
        self.diagnosis([P("x 쓰기 막기", "gate", condition={"type": "path_write", "globs": ["**/*.x"]},
                          backtest={"would_block": 1, "problem_changes": 1, "scenes": []})])
        self.assertEqual(autoapply.after_diagnosis(str(self.root), diagnose.load(self.root)), {"enabled": False})
        self.assertEqual(ledger.list_actions(self.root), [])


class DedupeTest(AutoApplyCase):
    def test_same_target_is_merged_not_reproposed(self):
        old_struct = P("AGENTS.md에 착수 선언 예시 넣기", "structure", targets=["AGENTS.md"])
        old_gate = P("`_tmp_` 스크립트 막기", "gate", condition={"type": "path_write", "globs": ["packages/db/scripts/_tmp_*"]},
                     backtest={"scenes": [{"source": "session", "ref": "aa", "at": "2026-11-01T00:00:00+09:00"},
                                          {"source": "session", "ref": "bb", "at": "2026-11-02T00:00:00+09:00"}]})
        hist = {"shown": [{"title": old_struct["title"], "sig": autoapply.signature(old_struct)},
                          {"title": old_gate["title"], "sig": autoapply.signature(old_gate)}]}
        new = [P("AGENTS.md에 evidence·report 예시 넣기", "structure", targets=["./AGENTS.md"]),  # 이름만 바꾼 같은 대상
               P("`*.tmp.*` 임시 스크립트 막기", "gate", condition={"type": "path_write", "globs": ["**/*.tmp.*"]},
                 backtest={"scenes": [{"source": "session", "ref": "aa", "at": "2026-11-01T00:00:00+09:00"},
                                      {"source": "session", "ref": "bb", "at": "2026-11-02T00:00:00+09:00"}]}),
               P("`.gitignore`에 로그 넣기", "structure", targets=[".gitignore"]),
               P("같은 진단 안 중복", "structure", targets=[".gitignore"])]
        keep, merged = autoapply.dedupe(str(self.root), new, hist)
        self.assertEqual([p["title"] for p in keep], ["`.gitignore`에 로그 넣기"])
        by = {m["title"]: m for m in merged}
        self.assertEqual(by["AGENTS.md에 evidence·report 예시 넣기"]["by"], "대상 파일")
        self.assertEqual(by["`*.tmp.*` 임시 스크립트 막기"]["by"], "백테스트 장면")
        self.assertEqual(by["같은 진단 안 중복"]["into"], "`.gitignore`에 로그 넣기")
        self.assertEqual(hist["shown"][0]["repeats"], 1)

    def test_ticket_targets_count_as_known(self):
        from vmc_harness.loop import tickets
        c = tickets.register_ticket(str(self.root), {"title": "훅 경로 절대화", "kind": "structure",
                                                     "targets": [".claude/settings.json"]})
        keep, merged = autoapply.dedupe(str(self.root), [P("훅을 프로젝트 기준 경로로", "structure",
                                                           targets=[".claude/settings.json"])], {"shown": []})
        self.assertEqual(keep, [])
        self.assertEqual(merged[0]["from"], "루프 티켓")
        self.assertEqual(ledger.get_candidate(self.root, c["candidate_id"])["count"], 2)

    def test_diagnose_run_merges_and_applies(self):
        props = [P("x 쓰기 막기", "gate", condition={"type": "path_write", "globs": ["**/*.x"]}),
                 P("이름만 바꾼 x 쓰기 막기", "gate", condition={"type": "path_write", "globs": ["**/*.x"]})]
        fake = {"text": json.dumps({"summary": "s", "proposals": props}, ensure_ascii=False), "tokens": 1, "sec": 1.0}
        with mock.patch("vmc_harness.core.llm.available", return_value=True), \
                mock.patch("vmc_harness.core.llm.nested", return_value=False), \
                mock.patch("vmc_harness.core.llm.claude_agent", return_value=fake), \
                mock.patch.object(diagnose, "render_proposal", return_value={}), \
                mock.patch.object(autoapply, "_backtest_of",
                                  return_value=({"would_block": 1, "problem_changes": 1}, "테스트")):
            d = diagnose.run(self.root, sessions=None)
        self.assertEqual([p["title"] for p in d["proposals"]], ["x 쓰기 막기"])
        self.assertEqual(d["checks"]["merged"][0]["by"], "게이트 조건")
        p = diagnose.load(self.root)["proposals"][0]
        self.assertTrue(p["auto_applied"]["action_id"], p.get("auto_checks"))
        self.assertTrue(read_json(diagnose.history_path(self.root))["shown"][0]["sig"]["gate"])


HOOKS = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "a.sh"},
                                                                 {"type": "command", "command": "b.sh"}]}]}}


class FakeClaude:
    def __init__(self, edits):
        self.edits = edits

    def __call__(self, root, prompt, cwd, tools, deadline=None, add_dirs=None):
        from pathlib import Path
        for rel, text in self.edits.items():
            p = Path(cwd) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return {"text": '{"done": true}', "tokens": 500, "ok": True, "interrupted": None, "error": None}


class StructureFlowTest(AutoApplyCase):
    def structure(self, edits, title="계산 모듈에 검사 함수 넣기", targets=("calc.py",)):
        from vmc_harness.loop import tickets
        d = self.diagnosis([P(title, "structure", targets=list(targets), signal=SIG_TSC)])
        tickets.sync_diagnosis(str(self.root), d)
        with mock.patch("vmc_harness.loop.cleanse.claude", FakeClaude(edits)):
            return autoapply.apply(str(self.root), structure=True)

    def test_flow_apply_merge_then_judge_reverts_when_signal_does_not_drop(self):
        from vmc_harness.ledger import judge
        res = self.structure({"calc.py": CALC + "\n\ndef check(n):\n    return classify(n)\n"})
        self.assertEqual(res["rows"][0]["result"], "queued")
        self.assertEqual(res["structure"]["units"], 1, res)
        self.assertEqual(res["structure"]["merge"][0]["result"], "merged", res)
        a = [x for x in ledger.list_actions(self.root) if (x.get("basis") or {}).get("type") == "diagnosis_auto"][0]
        self.assertEqual(a["status"], "trial")
        self.assertIn("def check", (self.root / "calc.py").read_text(encoding="utf-8"))  # 사람 작업 트리(main)에 반영
        log = self.git("log", "-1", "--format=%B").stdout
        self.assertIn("Harness-Action: %s" % a["action_id"], log)
        texts = (local_dir(self.root) / "notices.jsonl").read_text(encoding="utf-8")
        self.assertIn("vmc-harness revert %s" % a["action_id"], texts)
        # 반영 뒤 대화 10개 — 근거 신호(npx tsc)가 대화당 그대로 → 원복(git revert + 장부)
        later = (now() + _dt.timedelta(hours=1)).isoformat(timespec="seconds")
        self.sessions([sess("L%d" % i, later, ["npx tsc -p ."] if i % 2 else ["ls"]) for i in range(10)], append=True)
        v = judge.judge_actions(str(self.root))
        self.assertEqual(v[0]["result"], "reverted", v)
        self.assertIn("근거 신호가 줄지 않음", v[0]["reason"])
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["status"], "reverted")
        self.assertNotIn("def check", (self.root / "calc.py").read_text(encoding="utf-8"))
        self.assertIn("하네스 조치 원복", (local_dir(self.root) / "notices.jsonl").read_text(encoding="utf-8"))

    def test_signal_dropped_keeps(self):
        from vmc_harness.ledger import judge
        self.structure({"calc.py": CALC + "\n\ndef check(n):\n    return classify(n)\n"})
        a = [x for x in ledger.list_actions(self.root) if (x.get("basis") or {}).get("type") == "diagnosis_auto"][0]
        later = (now() + _dt.timedelta(hours=1)).isoformat(timespec="seconds")
        self.sessions([sess("L%d" % i, later, ["ls"]) for i in range(10)], append=True)
        v = judge.judge_actions(str(self.root), at=now() + _dt.timedelta(days=4))
        self.assertEqual(v[0]["result"], "kept", v)
        self.assertIn("근거 신호", v[0]["reason"])
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["status"], "kept")

    def test_loosening_diff_is_proposed_not_merged(self):
        write_json(self.root / ".claude/settings.json", HOOKS)
        self.commit_old("hooks")
        loose = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/abs/a.sh"}]}]}}
        res = self.structure({".claude/settings.json": json.dumps(loose)}, title="훅을 절대 경로로 부르기",
                             targets=[".claude/settings.json"])
        self.assertEqual(res["structure"]["merge"], [])
        c = ledger.get_candidate(self.root, res["rows"][0]["ticket_id"])
        self.assertEqual(c["status"], "proposed")
        self.assertIn("훅이 줄어듦", c["proposed_reason"])
        self.assertEqual(json.loads((self.root / ".claude/settings.json").read_text())["hooks"], HOOKS["hooks"])

    def test_human_uncommitted_overlap_skips_the_day(self):
        from vmc_harness.loop import merge
        self.write("calc.py", CALC + "\n# 사람이 고치는 중\n")  # 처리 전에 사람이 고치는 중이면 손대지 않는다
        res = self.structure({"calc.py": CALC + "\n\ndef check(n):\n    return classify(n)\n"})
        self.assertEqual(res["structure"]["merge"], [])
        self.git("checkout", "--", "calc.py")
        with mock.patch("vmc_harness.loop.merge.merge_actions", return_value=[]):  # 준비까지만
            from vmc_harness.loop import tickets
            cid = res["rows"][0]["ticket_id"]
            with mock.patch("vmc_harness.loop.cleanse.claude",
                            FakeClaude({"calc.py": CALC + "\n\ndef check(n):\n    return classify(n)\n"})):
                r = tickets.process(str(self.root), ledger.get_candidate(self.root, cid))
        self.assertEqual(r["outcome"], "prepared", r)
        self.write("calc.py", CALC + "\n# 사람이 고치는 중\n")  # 준비와 머지 사이에 사람이 같은 파일을 고치기 시작
        a = ledger.get_action(self.root, r["action_id"])
        m = merge.merge_actions(str(self.root), [a])
        self.assertEqual(m[0]["result"], "waiting", m)
        a = ledger.get_action(self.root, r["action_id"])
        self.assertEqual(a["status"], "prepared")
        self.assertTrue(a["skip_day"])
        self.assertIn("사람이 고치는 중", (self.root / "calc.py").read_text(encoding="utf-8"))  # 섞지 않았다
        again = merge.merge_actions(str(self.root), [a])
        self.assertIn("오늘은 건너뜀", again[0]["reason"])


class GateSideEffectTest(AutoApplyCase):
    def test_disputed_blocks_revert_auto_gate(self):
        from vmc_harness.ledger import judge
        self.diagnosis([P("x 쓰기 막기", "gate", condition={"type": "path_write", "globs": ["**/*.x"]},
                          backtest={"would_block": 1, "problem_changes": 1, "scenes": []})])
        autoapply.apply(str(self.root), structure=False)
        a = [x for x in ledger.list_actions(self.root) if x["layer"] == "gate"][0]
        gid = a["target"]["gate_id"]
        day = now().astimezone().strftime("%Y-%m-%d")
        write_json(ledger.metrics_path(self.root, day), {"date": day, "pc_id": "p", "exposures": {a["action_id"]: 4},
                                                          "gates": {gid: {"fired": 4, "blocked": 4, "false_block": 0,
                                                                          "disputed": 2, "sec": 0.1}}})
        with mock.patch("vmc_harness.ledger.metrics.rollup", return_value=[]):
            v = judge.judge_actions(str(self.root))
        self.assertEqual(v[0]["result"], "reverted", v)
        self.assertIn("헛막기·반박 2/4", v[0]["reason"])
        self.assertFalse(read_json(self.root / (".harness/gates/%s.json" % gid))["enabled"])
