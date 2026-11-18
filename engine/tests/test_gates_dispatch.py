"""디스패처·어댑터·엔진 쓰기 보호·DB 직접 접속·P5 주입 필터 (spec 02·01 P5·P8)."""

import json
import os
import re
import sys
import types
import unittest
from unittest import mock

from tests.gates_util import KERNEL_ONLY, GatesCase
from vmc_harness.core import events, inject
from vmc_harness.hooks import contract, dispatcher
from vmc_harness.hooks.adapters import common
from vmc_harness.hooks.contract import HookResult

NUMERIC = re.compile(r"(P30|P50|P80|예산|budget|토큰|tokens?|\d+\s*%)", re.I)


class TestAdapters(unittest.TestCase):
    def test_detect_agent(self):
        self.assertEqual(common.detect_agent("codex", {}), "codex")
        self.assertEqual(common.detect_agent(None, {"transcript_path": "/Users/a/.codex/sessions/x.jsonl"}), "codex")
        self.assertEqual(common.detect_agent(None, {"transcript_path": "/Users/a/.claude/projects/x.jsonl"}), "claude")

    def test_parse_patch(self):
        ops = common.parse_patch("*** Begin Patch\n*** Add File: a.txt\n+x\n*** Update File: b/c.py\n@@\n*** Delete File: d.md\n*** Update File: e.py\n*** Move to: f.py\n*** End Patch")
        self.assertEqual([o["op"] for o in ops], ["add", "update", "delete", "update", "move"])
        self.assertEqual(ops[1]["path"], "b/c.py")

    def test_output_formats(self):
        r = HookResult(block=True, reason="[vmc-harness:gate_violation] x")
        out, rc = common.output("PreToolUse", r)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        out, _ = common.output("Stop", r)
        self.assertEqual(json.loads(out), {"decision": "block", "reason": "[vmc-harness:gate_violation] x"})
        out, _ = common.output("PostToolUse", HookResult(inject=["[vmc-harness:pm_opinion] y"]))
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["additionalContext"], "[vmc-harness:pm_opinion] y")
        self.assertEqual(common.output("Stop", HookResult()), ("", 0))


class TestDispatch(GatesCase):
    def test_nested_and_uninstalled_pass(self):
        with mock.patch.dict(os.environ, {"VMC_HARNESS_NESTED": "1"}):
            out = self.hook("PreToolUse", "Write", {"file_path": str(self.root / ".harness/ledger/x.json"), "content": "{}"})
        self.assertEqual(out, {})
        other = self._tmp / "plain"
        other.mkdir()
        raw = {"session_id": "s", "cwd": str(other), "tool_name": "Write",
               "tool_input": {"file_path": str(other / "x"), "content": ""}}
        self.assertEqual(dispatcher.dispatch("PreToolUse", "claude", json.dumps(raw), modules=KERNEL_ONLY), ("", 0))

    def test_engine_write_guard_claude_write(self):
        out = self.hook("PreToolUse", "Write", {"file_path": str(self.root / ".harness/ledger/findings/F.json"), "content": "{}"})
        self.assertDenied(out, "엔진 쓰기 보호")

    def test_engine_write_guard_kernel_and_evidence(self):
        k = self.home / "kernel" / "0.1.0" / "x.py"
        out = self.hook("PreToolUse", "Edit", {"file_path": str(k), "old_string": "a", "new_string": "b"})
        self.assertDenied(out, "엔진")
        out = self.hook("PreToolUse", "Write", {"file_path": str(self.root / ".harness/cache/evidence/T-1/m.marker.json"), "content": "{}"})
        self.assertDenied(out)

    def test_engine_write_guard_codex_patch_and_shell(self):
        patch = "*** Begin Patch\n*** Add File: .harness/ledger/actions/A.json\n+{}\n*** End Patch"
        out = self.hook("PreToolUse", "apply_patch", {"command": patch}, agent="codex")
        self.assertDenied(out)
        for cmd in ("echo x >> .harness/ledger/tasks/a.jsonl", "rm -rf .harness/ledger",
                    "sed -i 's/a/b/' .harness/ledger/baseline.json", "cp /tmp/x .harness/ledger/baseline.json",
                    "powershell -Command \"Set-Content -Path .harness\\ledger\\x.json -Value 1\""):
            out = self.hook("PreToolUse", "Bash", {"command": cmd}, agent="codex")
            self.assertDenied(out)

    def test_read_only_allowed(self):
        for cmd in ("cat .harness/ledger/baseline.json", "ls .harness/ledger", "git status", "vmc-harness why T-1"):
            out = self.hook("PreToolUse", "Bash", {"command": cmd})
            self.assertAllowed(out)
        out = self.hook("PreToolUse", "Write", {"file_path": str(self.root / "src/app.py"), "content": "x"})
        self.assertAllowed(out)

    def test_db_direct_blocked_and_missing_guard_fail_closed(self):
        fake = types.ModuleType("vmc_harness.db.sqlguard")
        fake.is_direct_db_command = lambda c, cwd=None: "sqlcmd" in c
        with mock.patch.dict(sys.modules, {"vmc_harness.db.sqlguard": fake}):
            out = self.hook("PreToolUse", "Bash", {"command": "sqlcmd -S x -Q \"select 1\""})
            self.assertDenied(out, "DB 직접 접속 차단됨")
            self.assertAllowed(self.hook("PreToolUse", "Bash", {"command": "ls"}))
        with mock.patch.dict(sys.modules, {"vmc_harness.db.sqlguard": None}):
            out = self.hook("PreToolUse", "Bash", {"command": "ls"})
            self.assertDenied(out, "판정 도구 없음")
            # 하네스 자신의 명령은 판정 대상이 아니다
            self.assertAllowed(self.hook("PreToolUse", "Bash", {"command": "vmc-harness evidence list"}))

    def test_safety_exception_blocks_other_exception_passes(self):
        boom = types.ModuleType("vmh_test_boom")

        def on_pre_tool(ctx):
            raise RuntimeError("boom")
        boom.on_pre_tool = on_pre_tool
        with mock.patch.dict(sys.modules, {"vmh_test_boom": boom}):
            out = self.hook("PreToolUse", "Bash", {"command": "ls"}, modules=[("vmh_test_boom", True)])
            self.assertDenied(out, "안전 게이트 확인 실패")
            out = self.hook("PreToolUse", "Bash", {"command": "ls"}, modules=[("vmh_test_boom", False)])
            self.assertAllowed(out)
        errs = events.read(self.root, "gate_error")
        self.assertTrue(errs and errs[-1]["module"] == "vmh_test_boom")
        times = events.read(self.root, "gate_time")
        self.assertTrue(times and times[-1]["gate_id"] == "dispatcher:PreToolUse")

    def test_missing_module_skipped(self):
        out = self.hook("PreToolUse", "Bash", {"command": "ls"}, modules=[("vmc_harness.nonexistent_area.handlers", True)])
        self.assertAllowed(out)

    def test_bad_stdin_blocks_pretool_only(self):
        out, _ = dispatcher.dispatch("PreToolUse", "claude", "{not json", modules=KERNEL_ONLY, cwd=str(self.root))
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(dispatcher.dispatch("Stop", "claude", "{not json", modules=KERNEL_ONLY, cwd=str(self.root)), ("", 0))


class TestInjectFilter(GatesCase):
    """01 P5 — 허용 목록 밖 주입은 버린다. 커널 테스트: P80을 넘기는 데모에서 작업 중 숫자 주입 0건."""

    def _pressure_module(self):
        m = types.ModuleType("vmh_test_pressure")

        def res():
            return HookResult(inject=[
                "P80 초과: 예상 6분, 지금 14분",                                   # 태그 없는 원문
                "[vmc-harness:gate_violation] 예산 150% 사용",                      # 태그 위조 + 숫자
                inject.message("declare_context", "비슷한 작업 실측 P50 8분") or "", # 착수 정보 — 작업 중이면 금지
                "[vmc-harness:budget_warning] 남은 토큰 1200",                      # 허용 목록 밖 유형
                inject.message("pm_opinion", "범위 밖 파일은 이번 요청과 관계없어 보임"),  # 허용
            ])
        for name in ("on_session_start", "on_user_prompt", "on_pre_tool", "on_post_tool", "on_stop"):
            setattr(m, name, lambda ctx: res())
        return m

    def test_p80_demo_zero_numeric_injection_during_work(self):
        mod = self._pressure_module()
        modules = list(contract.HANDLER_MODULES) + [("vmh_test_pressure", False)]
        seen = []
        with mock.patch.dict(sys.modules, {"vmh_test_pressure": mod}):
            self.hook("SessionStart", modules=modules, source="startup")
            for i in range(12):   # 예측 P80을 훌쩍 넘기는 긴 작업(편집 반복)
                for ev, tool, ti in (("PreToolUse", "Edit", {"file_path": str(self.root / "src/a.py"), "old_string": "a", "new_string": "b"}),
                                     ("PostToolUse", "Edit", {"file_path": str(self.root / "src/a.py")}),
                                     ("PreToolUse", "Bash", {"command": "ls"})):
                    seen.append(self.hook(ev, tool, ti, modules=modules))
            seen.append(self.hook("Stop", modules=modules, stop_hook_active=False))
        texts = []
        for out in seen:
            hso = out.get("hookSpecificOutput") or {}
            texts.extend([hso.get("additionalContext") or "", hso.get("permissionDecisionReason") or "", out.get("reason") or ""])
        injected = "\n".join(t for t in texts if t)
        # 착수 선언 차단은 착수 시점 정보다(01 P5 허용) — 채울 옵션 이름(--tokens)이 실린다. 숫자는 없다
        during = "\n".join(x for x in injected.splitlines() if "착수 선언 없음" not in x)
        self.assertEqual(NUMERIC.findall(during), [], during)
        self.assertEqual(re.findall(r"\d", "\n".join(x for x in injected.splitlines() if "착수 선언 없음" in x)), [])
        self.assertIn("[vmc-harness:pm_opinion]", injected)
        for line in injected.splitlines():
            self.assertRegex(line, r"^\[vmc-harness:(gate_violation|missing_evidence|pm_opinion|declare_context)\] ")
        dropped = events.read(self.root, "inject_dropped")
        self.assertTrue(dropped)

    def test_declare_context_allowed_before_work(self):
        mod = types.ModuleType("vmh_test_decl")
        mod.on_session_start = lambda ctx: HookResult(inject=[inject.message("declare_context", "비슷한 과거 작업 P50 8분")])
        with mock.patch.dict(sys.modules, {"vmh_test_decl": mod}):
            out = self.hook("SessionStart", modules=[("vmh_test_decl", False)], source="startup")
        self.assertIn("declare_context", out["hookSpecificOutput"]["additionalContext"])

    def test_forged_block_reason_replaced_but_still_blocks(self):
        mod = types.ModuleType("vmh_test_forge")
        mod.on_pre_tool = lambda ctx: HookResult(block=True, reason="예산 초과 120%")
        with mock.patch.dict(sys.modules, {"vmh_test_forge": mod}):
            out = self.hook("PreToolUse", "Bash", {"command": "ls"}, modules=[("vmh_test_forge", True)])
        self.assertDenied(out)
        self.assertNotIn("120", out["hookSpecificOutput"]["permissionDecisionReason"])
        # 사유를 못 실어도 규칙(막은 모듈) 이름은 남는다(현장 결함 C8)
        self.assertIn("규칙: vmh_test_forge", out["hookSpecificOutput"]["permissionDecisionReason"])


if __name__ == "__main__":
    unittest.main()


class TestHookStartupImports(GatesCase):
    """E2 — 훅은 도구 호출마다 새 프로세스다. 다른 명령 모듈·Stop 전용 모듈·DB 클라이언트를 읽지 않아야 한다(동작은 같다)."""

    def test_pretool_imports_stay_small(self):
        import subprocess
        from pathlib import Path
        engine = str(Path(__file__).resolve().parent.parent)
        raw = json.dumps(self.raw("PreToolUse", "Bash", {"command": "ls"}))
        code = ("import io, sys\nsys.stdin = io.TextIOWrapper(io.BytesIO(sys.argv[1].encode()))\n"
                "from vmc_harness import cli\nsys.stdout = io.StringIO()\ncli.main(['hook', 'PreToolUse', '--agent', 'claude'])\n"
                "sys.stderr.write('\\n'.join(m for m in sys.modules if m.startswith('vmc_harness')))\n")
        env = dict(os.environ, PYTHONPATH=engine)
        r = subprocess.run([sys.executable, "-c", code, raw], capture_output=True, text=True, env=env, cwd=str(self.root))
        mods = set(r.stderr.split())
        self.assertIn("vmc_harness.gates.handlers", mods, r.stderr)
        for m in ("vmc_harness.commands.init", "vmc_harness.commands.report", "vmc_harness.gates.completion",
                  "vmc_harness.db.client", "vmc_harness.bootstrap.facts"):
            self.assertNotIn(m, mods)
