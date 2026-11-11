"""메타 게이트 — 설치 때 고른 프로파일은 기준, Stop은 (세션, 변경)마다 한 번, 대화 "승인"으로 승인 카드 승인 (spec 02·00 기준 2).

현장 결함: 설치 직후(설정 파일 커밋 전) solo 프로파일을 standard 대비 '느슨하게 바꿈'으로 보고 Stop마다 막아
두 세션이 4분에 40번 막혔다(그중 한 세션은 파일을 하나도 안 고쳤다).
"""

import json
from unittest import mock

from tests.gates_util import GatesCase
from vmc_harness.core import ledger
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.gates import meta, state

CFG = ".harness/config.json"


class TestMetaApproval(GatesCase):
    def _cfg(self, **kw):
        cfg = read_json(self.root / CFG)
        cfg.update(kw)
        return cfg

    def _edit_profile(self, profile):
        return self.hook("PreToolUse", "Write", {"file_path": str(self.root / CFG),
                                                 "content": json.dumps(self._cfg(profile=profile))})

    def test_init_profile_is_baseline_before_commit(self):
        write_json(self.root / CFG, self._cfg(profile="solo"))   # 설치 직후 — HEAD에 설정 파일 없음
        self.assertEqual(meta.judge_worktree(self.root), [])
        self.assertAllowed(self.hook("Stop"))
        self.commit("install")
        self.assertEqual(meta.judge_worktree(self.root), [])
        # 커밋 뒤에는 HEAD가 기준 — 더 엄격하게(solo→gxp)는 조이기라 통과
        write_json(self.root / CFG, self._cfg(profile="gxp"))
        self.assertEqual(meta.judge_worktree(self.root), [])

    def test_stop_blocks_once_per_session_and_change(self):
        self.commit("install")   # standard가 기준
        write_json(self.root / CFG, self._cfg(profile="solo"))   # 셸 스크립트 등으로 이미 바뀐 상태
        out = self.hook("Stop")
        self.assertEqual(out.get("decision"), "block", out)
        self.assertIn("느슨한 프로파일", out["reason"])
        self.assertIn("'승인'", out["reason"])
        self.assertIn("systemMessage", out)   # 사람에게 승인 카드
        for _ in range(3):
            self.assertAllowed(self.hook("Stop"))
        # 다른 세션은 그 세션에서 한 번
        self.session = "sess-test-2"
        self.assertEqual(self.hook("Stop").get("decision"), "block")
        self.assertAllowed(self.hook("Stop"))
        # 카드는 하나만(답 없는 같은 카드를 다시 쓴다)
        cards = [a for a in ledger.list_approvals(self.root) if (a.get("detail") or {}).get("kind") == "profile_loosen"]
        self.assertEqual(len(cards), 1)

    def test_pretool_still_blocks_every_time_until_approved_in_chat(self):
        self.commit("install")
        out = self._edit_profile("solo")
        self.assertDenied(out, "느슨한 프로파일")
        self.assertIn("'승인'", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertDenied(self._edit_profile("solo"))
        # 승인 문구를 품은 다른 말은 승인이 아니다
        self.hook("UserPromptSubmit", prompt="승인하지 마")
        self.assertDenied(self._edit_profile("solo"))
        self.hook("UserPromptSubmit", prompt="승인!")
        self.assertAllowed(self._edit_profile("solo"))
        a = [a for a in ledger.list_approvals(self.root) if (a.get("detail") or {}).get("kind") == "profile_loosen"]
        self.assertEqual([x["answer"] for x in a], ["approve"])

    def test_chat_approval_needs_recent_meta_block(self):
        self.commit("install")
        self.assertDenied(self._edit_profile("solo"))
        with mock.patch.object(state, "minutes_since", return_value=10 ** 6):
            self.hook("UserPromptSubmit", prompt="승인")
        self.assertDenied(self._edit_profile("solo"))
        # 마지막 차단이 메타 게이트가 아니면(다른 게이트가 그 뒤에 막음) 승인하지 않는다
        state.record_block(self.root, mock.Mock(session_id=self.session, tool_name="Bash", write_paths=[], command="x",
                                                event="PreToolUse", state={}), "tamper", "r")
        self.hook("UserPromptSubmit", prompt="승인")
        self.assertFalse(any(a.get("answer") for a in ledger.list_approvals(self.root)))

    def test_gate_loosen_without_card_has_no_approve_line(self):
        self.commit("install")
        cfg = self._cfg(project_id="other")
        out = self.hook("PreToolUse", "Write", {"file_path": str(self.root / CFG), "content": json.dumps(cfg)})
        self.assertDenied(out, "project_id")
        self.assertNotIn("'승인'", out["hookSpecificOutput"]["permissionDecisionReason"])
