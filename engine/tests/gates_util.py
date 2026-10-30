"""gates 테스트 공용 — 훅 입력 JSON 만들기·디스패치."""

import json

from tests.util import HarnessTestCase
from vmc_harness.hooks import dispatcher

KERNEL_ONLY = [("vmc_harness.gates.handlers", True)]


class GatesCase(HarnessTestCase):
    session = "sess-test-1"

    def raw(self, event, tool=None, tool_input=None, **extra):
        r = {"session_id": self.session, "cwd": str(self.root), "hook_event_name": event,
             "transcript_path": "/tmp/x/.claude/projects/p/%s.jsonl" % self.session}
        if tool:
            r["tool_name"] = tool
            r["tool_input"] = tool_input or {}
        r.update(extra)
        return r

    def hook(self, event, tool=None, tool_input=None, agent="claude", modules=KERNEL_ONLY, **extra):
        out, rc = dispatcher.dispatch(event, agent, json.dumps(self.raw(event, tool, tool_input, **extra)),
                                      modules=modules)
        self.assertEqual(rc, 0)
        return json.loads(out) if out else {}

    def assertDenied(self, out, contains=None):
        hso = out.get("hookSpecificOutput") or {}
        self.assertEqual(hso.get("permissionDecision"), "deny", out)
        if contains:
            self.assertIn(contains, hso.get("permissionDecisionReason", ""))

    def assertAllowed(self, out):
        hso = out.get("hookSpecificOutput") or {}
        self.assertNotEqual(hso.get("permissionDecision"), "deny", out)
        self.assertNotEqual(out.get("decision"), "block", out)
