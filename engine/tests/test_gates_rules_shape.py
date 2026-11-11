"""규칙 블록 — rules.json 모양이 여러 가지여도 게이트가 깨지지 않는다(현장 결함: 'str' object has no attribute 'get').

부트스트랩이 쓰는 기계 사실 규칙은 basis.source가 문자열("bootstrap")이다. 손으로 고친 파일에는 규칙 항목이 문자열일 수도 있다.
픽스처는 합성 데이터만.
"""

import json
from unittest import mock

from tests.gates_util import GatesCase
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.gates import rules

SHAPE = {"rules": [
    {"id": "r-aaa111", "text": "패키지 관리자: {value}", "value": "pnpm",
     "basis": {"type": "machine_fact", "command": ["vmc-harness", "init", "--print-fact", "r-aaa111"], "source": "bootstrap"},
     "checked_at": "2026-11-01T00:00:00+09:00", "created_at": "2026-11-01T00:00:00+09:00", "enabled": True},
    "손으로 적은 규칙 한 줄",
    {"id": "r-bbb222", "basis": "explicit_request", "enabled": True},
    {"id": "r-ccc333", "basis": {"type": "explicit_request", "quote": "로그는 지우지 마", "source": "2026-11-02"}, "enabled": True},
]}


class TestRulesShape(GatesCase):
    def setUp(self):
        super().setUp()
        write_json(self.root / ".harness" / "rules.json", SHAPE)

    def test_refresh_and_render_tolerate_string_shapes(self):
        with mock.patch.object(rules, "read_fact", return_value="pnpm"):
            self.assertEqual(rules.refresh_machine_facts(str(self.root), force=True), [])
        block = rules.render_block(str(self.root))
        self.assertIn("패키지 관리자: pnpm", block)
        self.assertIn("로그는 지우지 마", block)
        self.assertNotIn("손으로 적은 규칙", block)
        # 파일의 다른 모양 항목은 그대로 남는다
        self.assertIn("손으로 적은 규칙 한 줄", read_json(self.root / ".harness" / "rules.json")["rules"])

    def test_session_start_logs_no_rules_gate_error(self):
        from vmc_harness.core import events
        with mock.patch.object(rules, "read_fact", return_value="npm"):
            self.hook("SessionStart", modules=[("vmc_harness.gates.generated", False)])
        errs = list(events.read(str(self.root), "gate_error"))
        self.assertEqual(errs, [], errs)
        data = read_json(self.root / ".harness" / "rules.json")
        self.assertEqual(data["rules"][0]["value"], "npm")   # 값이 바뀌면 갱신까지 된다
        self.assertEqual(json.dumps(data["rules"][1:], ensure_ascii=False), json.dumps(SHAPE["rules"][1:], ensure_ascii=False))
