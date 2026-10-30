"""생성 게이트 로더·만들기·대상 추적·메타 게이트(L1 diff) (spec 02 §생성 게이트, 01 P3)."""

import json
import subprocess
import sys
import types
import unittest
from unittest import mock

from tests.gates_util import GatesCase
from vmc_harness.core import events, ledger, notices
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.gates import generate, generated, meta

GEN = [("vmc_harness.gates.generated", False)]
BODY = '''import re

def check(api):
    for rel in api.paths:
        if re.search(r"width:\\s*100vw", api.proposed(rel) or ""):
            return "%s에 전체 폭 고정" % rel
'''
BASIS = {"type": "explicit_request", "quote": "정산 페이지는 앞으로 전체 폭으로 고정하지 마",
         "source": {"session": "s-1", "date": "2026-10-23", "area": "fe/settle/"}}


class TestGenerated(GatesCase):
    def setUp(self):
        super().setUp()
        self.write("fe/settle/page.css", "a{}\n")
        self.commit()

    def make(self, **kw):
        args = dict(title="정산 페이지 폭", basis=BASIS, trigger={"paths": ["fe/settle/**"], "events": ["PreToolUse"]},
                    script_text=BODY, targets=["fe/settle/page.css"])
        args.update(kw)
        return generated.create(self.root, **args)

    def test_create_records_action_and_cost_line(self):
        r = self.make()
        m = r["meta"]
        self.assertIn("작업마다 +", r["cost_line"])
        a = ledger.get_action(self.root, m["action_id"])
        self.assertEqual(a["layer"], "gate")
        self.assertEqual(a["basis"]["quote"], BASIS["quote"])   # 원문 그대로
        self.assertEqual(a["target"], {"gate_id": m["gate_id"]})
        self.assertTrue(m["human_requested"])

    def test_basis_enforced(self):
        with self.assertRaises(ledger.MetaGateError):
            self.make(basis={"type": "learning", "quote": "x"})
        with self.assertRaises(ledger.MetaGateError):
            self.make(basis={"type": "explicit_request", "quote": "x", "source": {}})
        with self.assertRaises(ledger.MetaGateError):
            self.make(basis={"type": "machine_fact"})
        with self.assertRaises(generated.GateScriptError):
            self.make(script_text="import os\ndef check(api):\n    os.remove('x')\n")
        with self.assertRaises(generated.GateScriptError):
            self.make(script_text="def check(api):\n    open('x','w')\n")

    def test_trigger_block_and_exposure(self):
        m = self.make()["meta"]
        calls = []
        with mock.patch("vmc_harness.ledger.exposure.record", lambda root, **kw: calls.append(kw)):
            out = self.hook("PreToolUse", "Edit", {"file_path": str(self.root / "fe/settle/page.css"),
                                                   "old_string": "a{}", "new_string": "a{width: 100vw}"}, modules=GEN)
            self.assertDenied(out, "정산 페이지 폭")
            out = self.hook("PreToolUse", "Edit", {"file_path": str(self.root / "fe/settle/page.css"),
                                                   "old_string": "a{}", "new_string": "a{width: 10px}"}, modules=GEN)
            self.assertAllowed(out)
            out = self.hook("PreToolUse", "Edit", {"file_path": str(self.root / "fe/other.css"),
                                                   "old_string": "a", "new_string": "width: 100vw"}, modules=GEN)
            self.assertAllowed(out)
        self.assertEqual(len(calls), 2)                     # 조건에 안 걸린 호출은 실행·노출 없음
        self.assertEqual(calls[0]["gate_id"], m["gate_id"])
        self.assertTrue(events.read(self.root, "gate_fire"))
        self.assertTrue([e for e in events.read(self.root, "gate_time") if e.get("gate_id") == m["gate_id"]])

    def test_broken_script_fails_open(self):
        m = self.make()["meta"]
        (self.root / (".harness/gates/%s.py" % m["gate_id"])).write_text("def check(api):\n    return 1/0\n", encoding="utf-8")
        out = self.hook("PreToolUse", "Edit", {"file_path": str(self.root / "fe/settle/page.css"),
                                               "old_string": "a", "new_string": "b"}, modules=GEN)
        self.assertAllowed(out)
        self.assertTrue(events.read(self.root, "gate_error"))
        self.assertIn("harness_error", [n["kind"] for n in notices.pending(self.root)])

    def test_missing_target_disabled_with_buttons(self):
        m = self.make()["meta"]
        subprocess.run(["git", "rm", "-q", "fe/settle/page.css"], cwd=self.root, check=True)
        self.commit("rm")
        res = generated.check_targets(self.root)
        self.assertEqual(res[0][1], "disabled")
        g = generated.get(self.root, m["gate_id"])
        self.assertFalse(g["enabled"])
        n = [x for x in notices.pending(self.root) if x["kind"] == "gate_disabled"][0]
        self.assertIn("가리키던 파일이 없어져서 껐어요", n["text"])
        self.assertEqual(n["buttons"], ["켜기", "유지"])
        self.assertEqual(meta.judge_worktree(self.root), [])   # 하네스가 끈 것은 풀기 증거가 있는 것으로 본다
        generated.enable(self.root, m["gate_id"])
        self.assertTrue(generated.get(self.root, m["gate_id"])["enabled"])

    def test_renamed_target_retargeted(self):
        m = self.make()["meta"]
        subprocess.run(["git", "mv", "fe/settle/page.css", "fe/settle/main.css"], cwd=self.root, check=True)
        self.commit("mv")
        res = generated.check_targets(self.root)
        self.assertEqual(res[0][1], "moved")
        g = generated.get(self.root, m["gate_id"])
        self.assertEqual(g["targets"], ["fe/settle/main.css"])
        self.assertTrue(g["enabled"])

    def test_from_request_with_draft(self):
        r = generate.from_request(self.root, BASIS["quote"], {"session": "s-1"},
                                  draft={"gateable": True, "title": "폭", "trigger": {"paths": ["fe/settle/**"]},
                                         "targets": [], "script": BODY})
        self.assertTrue(r["gate_id"].startswith("g-"))
        self.assertIsNone(generate.from_request(self.root, "문체를 더 부드럽게", {}, draft={"gateable": False}))
        self.assertIsNone(generate.from_request(self.root, "x", {}, draft={"gateable": True, "script": "import os\ndef check(api): pass"}))


class TestMeta(GatesCase):
    def setUp(self):
        super().setUp()
        self.write("fe/a.css", "a{}\n")
        self.commit()
        self.gid = generated.create(self.root, "폭", BASIS, {"paths": ["fe/**"]}, BODY, targets=["fe/a.css"])["meta"]["gate_id"]
        write_json(self.root / ".harness/protected.json", {"paths": ["be/auth/**", "audit/**"]})
        self.commit("gates")

    def test_tighten_ok_loosen_needs_finding(self):
        p = self.root / (".harness/gates/%s.json" % self.gid)
        g = read_json(p)
        g["trigger"]["paths"] = ["fe/**", "be/**"]
        write_json(p, g)
        self.assertEqual(meta.judge_worktree(self.root), [])
        g["trigger"]["paths"] = ["fe/settle/**"]
        write_json(p, g)
        self.assertTrue(meta.judge_worktree(self.root))
        g["enabled"] = False
        write_json(p, g)
        self.assertTrue(meta.judge_worktree(self.root))
        ledger.create_finding(self.root, "false_block", {"gate_id": self.gid})
        self.assertEqual(meta.judge_worktree(self.root), [])

    def test_agent_delete_gate_blocked_pre(self):
        out = self.hook("PreToolUse", "Write", {"file_path": str(self.root / (".harness/gates/%s.json" % self.gid)),
                                                "content": json.dumps({"gate_id": self.gid, "enabled": False})})
        self.assertDenied(out, "메타 게이트")
        out = self.hook("PreToolUse", "Bash", {"command": "rm .harness/gates/%s.json" % self.gid})
        self.assertDenied(out, "메타 게이트")

    def test_protected_shrink_and_profile_loosen_need_human(self):
        out = self.hook("PreToolUse", "Write", {"file_path": str(self.root / ".harness/protected.json"),
                                                "content": json.dumps({"paths": ["be/auth/**"]})})
        self.assertDenied(out, "보호 영역 목록 줄이기")
        out = self.hook("PreToolUse", "Write", {"file_path": str(self.root / ".harness/protected.json"),
                                                "content": json.dumps({"paths": ["be/auth/**", "audit/**", "clock/**"]})})
        self.assertAllowed(out)
        cfg = read_json(self.root / ".harness/config.json")
        cfg2 = dict(cfg, profile="prototype")
        out = self.hook("PreToolUse", "Write", {"file_path": str(self.root / ".harness/config.json"), "content": json.dumps(cfg2)})
        self.assertDenied(out, "느슨한 프로파일")
        cfg3 = dict(cfg, profile="gxp")
        self.assertAllowed(self.hook("PreToolUse", "Write", {"file_path": str(self.root / ".harness/config.json"), "content": json.dumps(cfg3)}))
        a = meta.request_loosen(self.root, "T-1", "protected_shrink", ["audit/**"], "감사 폴더 폐기")
        ledger.answer_approval(self.root, a["approval_id"], "approve")
        out = self.hook("PreToolUse", "Write", {"file_path": str(self.root / ".harness/protected.json"),
                                                "content": json.dumps({"paths": ["be/auth/**"]})})
        self.assertAllowed(out)

    def test_stop_checks_worktree(self):
        (self.root / (".harness/gates/%s.json" % self.gid)).unlink()
        out = self.hook("Stop", stop_hook_active=False)
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("게이트", out["reason"])


if __name__ == "__main__":
    unittest.main()
