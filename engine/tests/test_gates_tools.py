"""증거·영향 반경·규칙 블록·훅 등록·셸 판정 (spec 02)."""

import http.server
import json
import threading
import unittest
from unittest import mock

from tests.gates_util import GatesCase
from vmc_harness.core import config
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.gates import evidence, impact, rules, shell
from vmc_harness.hooks import install


class TestShell(unittest.TestCase):
    def test_targets(self):
        cases = {
            "echo a > x.txt": ["x.txt"], "cat a | tee -a b": ["b"], "cp a b/c": ["b/c"], "rm -rf d": ["d"],
            "sed -i 's/a/b/' f.py": ["f.py"], "git rm -q g.py": ["g.py"], "dd if=a of=o.bin": ["o.bin"],
            "Set-Content -Path C:\\p\\x.txt -Value 1": ["C:\\p\\x.txt"], "cmd /c del y.txt": ["y.txt"],
            "bash -lc \"echo 1 >> z\"": ["z"], "ls 2>/dev/null": [], "git status": [], "cat x >&2": [],
        }
        for cmd, want in cases.items():
            got, _ = shell.all_targets(cmd)
            self.assertEqual(got, want, cmd)
        self.assertTrue(shell.analyze("python3 -c 'import os'")["opaque"])
        self.assertFalse(shell.analyze("grep -r foo .")["opaque"])


class TestEvidence(GatesCase):
    def test_make_and_list(self):
        png = self._tmp / "s.png"
        png.write_bytes(b"\x89PNG")
        m = evidence.screenshot(self.root, "T-1", str(png), "목록 페이지")
        self.assertEqual(m["kind"], "screenshot")
        self.assertTrue((evidence.evidence_dir(self.root, "T-1") / m["file"]).is_file())
        ms = evidence.list_markers(self.root, "T-1")
        self.assertEqual([x["marker_id"] for x in ms], [m["marker_id"]])
        with self.assertRaises(ValueError):
            evidence.make(self.root, "T-1", "agent_said_ok", "x")
        txt = self._tmp / "s.txt"
        txt.write_text("x")
        with self.assertRaises(ValueError):
            evidence.screenshot(self.root, "T-1", str(txt), "x")

    def test_baseline_from_bootstrap_facts(self):
        f = {"test_baseline": {"runs": [{"runner": "pytest", "dir": "be", "failed": ["tests/test_a.py::t"],
                                         "errors": [], "collection_errors": [{"path": "tests/test_b.py", "reason": "x"}]}]}}
        with mock.patch("vmc_harness.bootstrap.facts.load", lambda root: f):
            b = evidence.baseline_failures(self.root)
        self.assertIn("tests/test_a.py::t", b)
        self.assertIn("be/tests/test_a.py::t", b)
        self.assertIn("tests/test_b.py", b)
        with mock.patch("vmc_harness.bootstrap.facts.load", lambda root: {}):
            self.assertIsNone(evidence.baseline_failures(self.root))

    def test_api_call_records_response(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"ok": true, "password": "hunter2"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.handle_request, daemon=True)
        t.start()
        m = evidence.api_call(self.root, "T-2", "GET", "http://127.0.0.1:%d/api/x" % srv.server_port, target="/api/x")
        srv.server_close()
        data = evidence.load_data(self.root, "T-2", m)
        self.assertEqual(data["response"]["status"], 200)
        self.assertNotIn("hunter2", data["response"]["body"])   # 쓰는 시점 마스킹
        self.assertEqual(m["summary"]["status"], 200)


class TestImpact(GatesCase):
    def test_python_chain_to_api(self):
        self.write("be/core/money.py", "def fmt(x):\n    return x\n")
        self.write("be/service/settle.py", "from be.core.money import fmt\n\ndef total():\n    return fmt(1)\n")
        self.write("be/api/routes.py", "from ..service import settle\n\ndef get():\n    return settle.total()\n")
        self.write("be/other.py", "def fmt():\n    pass\n")
        res = impact.impact(self.root, "be/core/money.py:fmt")
        paths = [c["path"] for c in res["callers"]]
        self.assertEqual(paths[0], "be/service/settle.py")
        self.assertIn("be/api/routes.py", paths)
        self.assertIn("be/api/routes.py", res["apis"])
        self.assertNotIn("be/other.py", paths)

    def test_ts_import_and_symbol(self):
        self.write("fe/lib/date.ts", "export function d() {}\n")
        self.write("fe/components/Row.tsx", "import { d } from '@/lib/date'\nexport const Row = () => d()\n")
        self.write("fe/pages/list.tsx", "import { Row } from '../components/Row'\nexport default Row\n")
        res = impact.impact(self.root, "fe/lib/date.ts")
        self.assertIn("fe/pages/list.tsx", res["pages"])
        res = impact.impact(self.root, "Row")
        self.assertIn("fe/pages/list.tsx", [c["path"] for c in res["callers"]])


class TestRules(GatesCase):
    def test_render_preserves_customer_text(self):
        self.write("AGENTS.md", "# 우리 팀 규칙\n\n- 커밋은 한국어로\n")
        rules.add_explicit(self.root, "결재 SP는 절대 직접 고치지 마", {"session": "abcdef123456", "date": "2026-10-23", "area": "be/"})
        r = rules.render(self.root)
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 우리 팀 규칙\n\n- 커밋은 한국어로\n"))
        self.assertIn(rules.BEGIN, text)
        self.assertIn('"결재 SP는 절대 직접 고치지 마"', text)
        self.assertEqual((self.root / "CLAUDE.md").read_text(encoding="utf-8"), "@AGENTS.md\n")
        self.assertTrue(r["changed"])
        self.assertFalse(rules.render(self.root)["changed"])     # 같은 내용이면 그대로
        self.assertEqual(rules.unrender(self.root), True)
        self.assertEqual((self.root / "AGENTS.md").read_text(encoding="utf-8"), "# 우리 팀 규칙\n\n- 커밋은 한국어로\n")

    def test_existing_claude_md_untouched(self):
        self.write("CLAUDE.md", "my own\n")
        rules.render(self.root)
        self.assertEqual((self.root / "CLAUDE.md").read_text(encoding="utf-8"), "my own\n")

    def test_budget(self):
        with mock.patch.object(config, "get", side_effect=lambda k, root=None, profile=None:
                               10 if k == "budget.rule_block_tokens" else config.defaults()[0][k]):
            with self.assertRaises(rules.RuleBudgetError):
                rules.render(self.root)

    def test_machine_fact_refresh(self):
        self.write("ver.txt", "110\n")
        r = rules.add_machine_fact(self.root, ["python3", "-c", "print(open('ver.txt').read().strip())"], "호환성 수준 {value}")
        rules.render(self.root)
        self.assertIn("호환성 수준 110", (self.root / "AGENTS.md").read_text(encoding="utf-8"))
        self.write("ver.txt", "150\n")
        self.assertEqual(rules.refresh_machine_facts(self.root), [])          # 간격 안 — 다시 안 읽음
        self.assertEqual(rules.refresh_machine_facts(self.root, force=True), [r["id"]])
        self.assertIn("호환성 수준 150", (self.root / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("읽은 명령", (self.root / "AGENTS.md").read_text(encoding="utf-8"))


class TestInstall(GatesCase):
    def test_register_merge_and_unregister(self):
        user = {"permissions": {"allow": ["Bash(ls)"]},
                "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-lint"}]}]}}
        write_json(self.root / ".claude/settings.json", user)
        with mock.patch("vmc_harness.hooks.install._codex_exe", lambda: None):
            r = install.register(self.root)
            self.assertEqual(r["claude"], "injected")
            self.assertEqual(r["codex_trust"]["status"], "no_codex")
            self.assertEqual(install.register(self.root)["claude"], "already_present")
        s = read_json(self.root / ".claude/settings.json")
        self.assertEqual(s["permissions"], user["permissions"])
        self.assertEqual(set(s["hooks"]), {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd",
                                           "PostToolUseFailure"})   # Claude만 — 실패한 검증 명령 소요 기록
        pre = s["hooks"]["PreToolUse"]
        self.assertEqual(pre[0]["hooks"][0]["command"], "my-lint")
        ours = [h for g in pre for h in g["hooks"] if "vmc-harness hook" in h["command"]]
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]["command"], "vmc-harness hook PreToolUse --agent claude")
        c = read_json(self.root / ".codex/hooks.json")
        self.assertEqual(c["hooks"]["Stop"][0]["hooks"][0]["command"], "vmc-harness hook Stop --agent codex")
        with mock.patch("vmc_harness.hooks.install._codex_exe", lambda: None):
            install.unregister(self.root)
        s = read_json(self.root / ".claude/settings.json")
        self.assertEqual(s, user)
        self.assertFalse((self.root / ".codex/hooks.json").exists())

    def test_corrupt_settings_not_overwritten(self):
        p = self.root / ".claude/settings.json"
        p.parent.mkdir(parents=True)
        p.write_text("{oops", encoding="utf-8")
        r = install.register(self.root, agents=("claude",))
        self.assertTrue(r["claude"].startswith("error"))
        self.assertEqual(p.read_text(encoding="utf-8"), "{oops")


if __name__ == "__main__":
    unittest.main()
