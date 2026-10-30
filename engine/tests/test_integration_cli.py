"""통합 실측 — 실제 CLI 프로세스로 설치부터 제거까지 (영역 사이 이음매).

임시 홈(`VMC_HARNESS_HOME`)·임시 Claude/Codex 설정 폴더(`CLAUDE_CONFIG_DIR`·`CODEX_HOME`)·합성 프로젝트
(git 레포, Python BE + TS FE 흉내, `SQL/` 폴더, `be/tests/`)에서 `python -m vmc_harness …`를 실제로 띄운다.
LLM은 부르지 않는다 — PATH 맨 앞에 늘 실패하는 가짜 `claude`를 두고(편의 장치는 fail-open으로 건너뛴다), codex는 PATH에 없다.
이 PC 사용자 설정(~/.claude·~/.codex·launchd)은 건드리지 않는다(`init --dry-system`).

자세한 출력: VMC_INTEG_VERBOSE=1
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = str(Path(__file__).resolve().parent.parent)
VERBOSE = os.environ.get("VMC_INTEG_VERBOSE") == "1"

FILES = {
    "be/app/__init__.py": "",
    "be/app/api/__init__.py": "",
    "be/app/api/orders.py": (
        "from be.app.db import fetch\n\n\n"
        "def list_orders(limit):\n"
        "    \"\"\"GET /api/orders\"\"\"\n"
        "    return fetch(\"SELECT TOP 10 * FROM orders\")[:limit]\n"),
    "be/app/db.py": "def fetch(sql):\n    return [1, 2, 3]\n",
    "be/app/auth/__init__.py": "",
    "be/app/auth/token.py": "def issue(user):\n    return 'tok-' + user\n",
    "be/tests/__init__.py": "",
    "be/tests/test_orders.py": (
        "import unittest\n\nfrom be.app.api.orders import list_orders\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_limit(self):\n"
        "        self.assertEqual(list_orders(2), [1, 2])\n"),
    "be/__init__.py": "",
    "fe/package.json": json.dumps({"name": "fe", "scripts": {"build": "tsc"}}) + "\n",
    "fe/src/pages/Orders.tsx": "export default function Orders() {\n  return <div>orders</div>;\n}\n",
    "SQL/2026/001_orders_add_note.sql": "ALTER TABLE orders ADD note NVARCHAR(200) NULL;\n",
    "README.md": "# demo\n",
}


class IntegrationCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vmh-integ-"))
        cls.home = cls.tmp / "home"
        cls.root = (cls.tmp / "proj").resolve()
        cls.root.mkdir()
        fake_bin = cls.tmp / "bin"
        fake_bin.mkdir()
        # 가짜 claude — 늘 실패(LLM 없이). codex는 PATH에 없다
        if sys.platform == "win32":
            (fake_bin / "claude.cmd").write_text("@exit /b 1\r\n")
        else:
            p = fake_bin / "claude"
            p.write_text("#!/bin/sh\nexit 1\n")
            p.chmod(0o755)
        git_dir = os.path.dirname(shutil.which("git"))
        py_dir = os.path.dirname(sys.executable)
        cls.env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("CLAUDE", "CODEX", "VMC_HARNESS"))}
        cls.env.update({
            "VMC_HARNESS_HOME": str(cls.home),
            "CLAUDE_CONFIG_DIR": str(cls.tmp / "claude-config"),
            "CODEX_HOME": str(cls.tmp / "codex-home"),
            "PYTHONPATH": ENGINE, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
            "PATH": os.pathsep.join([str(fake_bin), git_dir, py_dir]),
            "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        })
        for rel, text in FILES.items():
            f = cls.root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        for args in (["init", "-q", "-b", "main"], ["config", "user.name", "dev"], ["config", "user.email", "dev@example.com"],
                     ["add", "-A"], ["commit", "-q", "-m", "init"]):
            subprocess.run(["git"] + args, cwd=str(cls.root), check=True, env=cls.env)
        cls.log = []

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(str(cls.tmp), ignore_errors=True)

    # ---- 실행 도우미 ----

    def cli(self, *args, stdin=None, check=True):
        import time
        t0 = time.time()
        r = subprocess.run([sys.executable, "-m", "vmc_harness"] + list(args), cwd=str(self.root), env=self.env,
                           input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=600)
        if VERBOSE:
            print("\n$ vmc-harness %s -> rc=%d (%.2fs)\n%s%s" % (" ".join(args), r.returncode, time.time() - t0,
                                                              r.stdout[-3000:], r.stderr[-2000:]))
        if check and r.returncode != 0:
            self.fail("vmc-harness %s 실패 rc=%d\n%s\n%s" % (" ".join(args), r.returncode, r.stdout[-2000:], r.stderr[-2000:]))
        return r

    def hook(self, event, agent="claude", sid="S-integ", **raw):
        if agent == "claude":
            payload = {"session_id": sid, "transcript_path": str(self.tmp / ("%s.jsonl" % sid)), "cwd": str(self.root),
                       "hook_event_name": event}
        else:
            payload = {"session_id": sid, "transcript_path": None, "cwd": str(self.root), "hook_event_name": event,
                       "model": "gpt-5-codex", "permission_mode": "default"}
        payload.update(raw)
        r = self.cli("hook", event, "--agent", agent, stdin=json.dumps(payload), check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout) if r.stdout.strip() else {}
        return out

    @staticmethod
    def blocked(out, event):
        if event == "PreToolUse":
            hs = out.get("hookSpecificOutput") or {}
            return hs.get("permissionDecision") == "deny" or out.get("decision") == "block"
        return out.get("decision") == "block"

    @staticmethod
    def reason(out):
        hs = out.get("hookSpecificOutput") or {}
        return hs.get("permissionDecisionReason") or out.get("reason") or ""

    def edit_raw(self, rel, new_text, tool="Edit"):
        p = self.root / rel
        old = p.read_text(encoding="utf-8") if p.exists() else ""
        if tool == "Write":
            return {"tool_name": "Write", "tool_input": {"file_path": str(p), "content": new_text}}
        return {"tool_name": "Edit", "tool_input": {"file_path": str(p), "old_string": old, "new_string": new_text}}

    def apply(self, rel, new_text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new_text, encoding="utf-8")

    def events(self):
        cfg = json.loads((self.root / ".harness" / "config.json").read_text(encoding="utf-8"))
        p = self.home / "projects" / cfg["project_id"] / "events.jsonl"
        if not p.is_file():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    # ---- 흐름 ----

    def test_flow(self):
        root = self.root
        # 1. init (사용자 설정 안 건드림, LLM 없음)
        r = self.cli("init", "--dry-system", "--no-llm", "--no-open", "--no-diagnose", str(root))
        self.assertIn("QA 서브에이전트", r.stdout)
        self.assertIn("독자 프로필", r.stdout)
        cfg = json.loads((root / ".harness" / "config.json").read_text(encoding="utf-8"))
        self.assertTrue(cfg.get("project_id"))
        settings = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("PreToolUse", settings.get("hooks", {}))
        self.assertTrue((root / ".codex" / "hooks.json").is_file())
        self.assertTrue((root / ".claude" / "agents" / "vmc-qa.md").is_file())
        self.assertIn("vmc-harness:begin", (root / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertTrue((self.home / "projects" / cfg["project_id"] / "reader-profile.json").is_file())
        hook_file = root / ".git" / "hooks" / "prepare-commit-msg"
        self.assertTrue(hook_file.is_file(), "commit hook 미설치")
        self.assertFalse((self.tmp / "claude-config" / "settings.json").exists(), "--dry-system인데 사용자 설정을 씀")
        # 보호 영역(사람이 [추가]로 확정한 것으로) — 인증
        (root / ".harness" / "protected.json").write_text(
            json.dumps({"paths": [{"glob": "be/app/auth/**", "kind": "auth"}]}), encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(root), check=True, env=self.env)
        subprocess.run(["git", "commit", "-q", "-m", "harness init"], cwd=str(root), check=True, env=self.env)

        # 2. SessionStart → UserPromptSubmit
        out = self.hook("SessionStart", source="startup")
        self.assertNotIn("decision", out)
        self.hook("UserPromptSubmit", prompt="주문 목록 API에 limit 상한을 넣어줘")

        # 3. PreToolUse Edit — 착수 선언 없음 → 막힘
        new_orders = FILES["be/app/api/orders.py"].replace("[:limit]", "[:min(limit, 100)]")
        out = self.hook("PreToolUse", **self.edit_raw("be/app/api/orders.py", new_orders))
        self.assertTrue(self.blocked(out, "PreToolUse"), out)
        self.assertIn("착수 선언", self.reason(out))

        # 4. declare → Edit 통과
        r = self.cli("declare", "--session", "S-integ", "--agent", "claude", "--intent", "주문 목록 limit 상한",
                     "--scope", "be/app/api/orders.py", "--tokens", "10000,20000,40000", "--agent-min", "1,2,3")
        out = self.hook("PreToolUse", **self.edit_raw("be/app/api/orders.py", new_orders))
        self.assertFalse(self.blocked(out, "PreToolUse"), out)
        self.apply("be/app/api/orders.py", new_orders)
        self.hook("PostToolUse", tool_response={"success": True}, **self.edit_raw("be/app/api/orders.py", new_orders))

        # 5. 안전 게이트 — DB 직접 접속 / 장부 손 편집 / 보호 영역
        out = self.hook("PreToolUse", tool_name="Bash",
                        tool_input={"command": "sqlcmd -S devdb -d APP -Q \"SELECT 1\""})
        self.assertTrue(self.blocked(out, "PreToolUse"), out)
        self.assertIn("DB", self.reason(out))
        out = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "python3 -c \"import pyodbc; pyodbc.connect('DSN=dev')\""})
        self.assertTrue(self.blocked(out, "PreToolUse"), out)
        out = self.hook("PreToolUse", **self.edit_raw(".harness/ledger/x.json", "{}", tool="Write"))
        self.assertTrue(self.blocked(out, "PreToolUse"), out)
        out = self.hook("PreToolUse", **self.edit_raw("be/app/auth/token.py", "def issue(user):\n    return user\n"))
        self.assertTrue(self.blocked(out, "PreToolUse"), out)
        self.assertIn("token.py", self.reason(out))   # 경로 속 'token'이 숫자 압박 필터에 걸려 사유가 지워지면 안 된다
        appr = list((root / ".harness" / "ledger" / "approvals").glob("*.json"))
        self.assertEqual(len(appr), 1, "보호 영역 승인 카드가 하나 생겨야 한다")
        aid = appr[0].stem
        self.assertIn(aid, out.get("systemMessage", ""), "승인 카드가 대화창(사람)에 보여야 한다")
        self.assertNotIn(aid, json.dumps(out.get("hookSpecificOutput", {}).get("additionalContext", "")))
        # 대화창에서 승인 → 같은 기록 → 다시 시도하면 통과
        self.cli("approval", aid, "approve")
        token_new = "def issue(user):\n    return 'tok2-' + user\n"
        out = self.hook("PreToolUse", **self.edit_raw("be/app/auth/token.py", token_new))
        self.assertFalse(self.blocked(out, "PreToolUse"), out)
        self.apply("be/app/auth/token.py", token_new)

        # 6. Stop — 증거 없음 → 막힘
        out = self.hook("Stop", stop_hook_active=False)
        self.assertTrue(self.blocked(out, "Stop"), out)
        self.assertIn("증거", self.reason(out))

        # 7. evidence test_run → Stop 통과
        r = self.cli("evidence", "--session", "S-integ", "test_run", "--target", "orders", "--",
                     sys.executable, "-m", "unittest", "discover", "-s", "be/tests", "-t", ".")
        out = self.hook("Stop", stop_hook_active=False)
        self.assertFalse(self.blocked(out, "Stop"), out)
        # 완료 보고 아래(사람에게만): 그 영역 꼭 알릴 일 한 줄 + mod 없는 환경 하루 1회 2줄 — 모델 컨텍스트엔 없음
        msg = out.get("systemMessage", "")
        self.assertIn("하네스", msg)
        self.assertNotIn("additionalContext", json.dumps(out))
        again = self.hook("Stop", stop_hook_active=False)
        self.assertNotIn("하네스 ·", again.get("systemMessage", ""), "하루 1회 2줄이 다시 나옴")

        # 8. report · SessionEnd
        r = self.cli("report", "--session", "S-integ", "--json")
        rep = json.loads(r.stdout)
        self.assertIn("be/app/api", " ".join(rep["modules"]) + " ".join(rep.get("files", [])) or "be/app/api")
        self.hook("SessionEnd", reason="exit")
        tasks = list((root / ".harness" / "ledger" / "tasks").rglob("*.json"))
        self.assertTrue(tasks, "작업 기록이 장부에 없음")
        rec = json.loads(tasks[0].read_text(encoding="utf-8"))
        self.assertEqual((rec.get("status"), rec.get("end_reason")), ("ended", "session_end"))
        self.assertEqual(rec.get("kernel_version"), __import__("vmc_harness").KERNEL_VERSION)
        self.assertIn("gate_sec", rec.get("harness_overhead") or {})
        ev = self.events()
        kinds = {e.get("kind") for e in ev}
        for k in ("gate_block", "gate_time"):
            self.assertIn(k, kinds)
        self.assertTrue(all(e.get("kernel_version") for e in ev))
        task_id = rec["task_id"]

        # 9. Codex 형식 — 선언 없음 → 막힘, 선언 후 apply_patch 통과, sqlcmd 막힘, 생성 게이트, 완료 게이트(QA·스크린샷)
        cx = "019a-codex-thread"
        cx_env = dict(self.env, CLAUDE_CODE_SESSION_ID="S-outer-claude", CODEX_THREAD_ID=cx)
        self.hook("SessionStart", agent="codex", sid=cx, source="startup")
        page = "fe/src/pages/Orders.tsx"
        new_page = FILES[page].replace("orders</div>", "orders list</div>")
        patch = ("*** Begin Patch\n*** Update File: %s\n@@\n-  return <div>orders</div>;\n"
                 "+  return <div>orders list</div>;\n*** End Patch\n" % page)
        out = self.hook("PreToolUse", agent="codex", sid=cx, tool_name="apply_patch", tool_input={"command": patch})
        self.assertTrue(self.blocked(out, "PreToolUse"), out)
        env_bak = self.env
        # Claude 안에서 띄운 Codex: 바깥 Claude 세션 id를 물려받아도 Codex 값으로 해석해야 한다(실측 사례)
        self.env = cx_env
        try:
            self.cli("declare", "--agent", "codex", "--intent", "주문 화면 문구", "--scope", page,
                     "--tokens", "5000,10000,20000", "--agent-min", "1,2,3")
        finally:
            self.env = env_bak
        out = self.hook("PreToolUse", agent="codex", sid=cx, tool_name="apply_patch", tool_input={"command": patch})
        self.assertFalse(self.blocked(out, "PreToolUse"), out)
        self.apply(page, new_page)
        self.hook("PostToolUse", agent="codex", sid=cx, tool_name="apply_patch", tool_input={"command": patch},
                  tool_response={"success": True})
        out = self.hook("PreToolUse", agent="codex", sid=cx, tool_name="Bash",
                        tool_input={"command": "sqlcmd -S devdb -Q \"DELETE FROM orders\""})
        self.assertTrue(self.blocked(out, "PreToolUse"), out)
        # 생성 게이트(사람 명시 요청) — CLI로 만들고 발동 확인
        gate_py = self.tmp / "gate_100vw.py"
        gate_py.write_text("import re\n\ndef check(api):\n    for rel in api.paths:\n"
                           "        if re.search(r'100vw', api.proposed(rel) or ''):\n"
                           "            return '%s에 전체 폭 고정' % rel\n", encoding="utf-8")
        self.cli("gate", "add", "--title", "화면 전체 폭 고정 금지", "--basis", "explicit_request",
                 "--quote", "앞으로 화면을 100vw로 고정하지 마", "--session", cx, "--area", "fe/",
                 "--paths", "fe/**", "--events", "PreToolUse", "--targets", page, "--script", str(gate_py))
        wide = new_page.replace("<div>", "<div style={{width: '100vw'}}>")
        out = self.hook("PreToolUse", agent="codex", sid=cx, tool_name="Write",
                        tool_input={"file_path": str(root / page), "content": wide})
        self.assertTrue(self.blocked(out, "PreToolUse"), out)
        # 완료 게이트 — 페이지가 바뀌었는데 증거 없음 → 막힘
        out = self.hook("Stop", agent="codex", sid=cx, stop_hook_active=False)
        self.assertTrue(self.blocked(out, "Stop"), out)
        # QA 시나리오 PASS(qa 영역이 qa_scenario 마커를 남긴다) → 통과
        scen = {"id": "orders-page", "title": "주문 화면", "pages": ["/orders"], "paths": ["fe/src/pages/**"],
                "expectations": [{"text": "주문 목록 문구가 보인다",
                                  "source": {"type": "current_behavior", "ref": page + "::Orders"}}],
                "data": {"writes": []}}
        sp = root / ".harness" / "qa" / "scenarios" / "orders-page.json"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(scen, ensure_ascii=False), encoding="utf-8")
        shot = self.tmp / "orders.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        self.env = cx_env
        try:
            self.cli("qa", "run", "orders-page")
            self.cli("qa", "status", "set", "orders-page", "PASS", "--evidence", str(shot))
        finally:
            self.env = env_bak
        out = self.hook("Stop", agent="codex", sid=cx, stop_hook_active=False)
        self.assertFalse(self.blocked(out, "Stop"), out)
        # 다시 편집 → 옛 증거는 인정 안 됨 → 스크린샷 증거로 통과
        newer = new_page.replace("orders list", "orders list!")
        self.hook("PreToolUse", agent="codex", sid=cx, tool_name="Write",
                  tool_input={"file_path": str(root / page), "content": newer})
        import time as _t
        _t.sleep(1.1)
        self.apply(page, newer)
        self.hook("PostToolUse", agent="codex", sid=cx, tool_name="Write",
                  tool_input={"file_path": str(root / page), "content": newer}, tool_response={"success": True})
        out = self.hook("Stop", agent="codex", sid=cx, stop_hook_active=False)
        self.assertTrue(self.blocked(out, "Stop"), out)
        _t.sleep(1.1)
        shot2 = self.tmp / "orders2.png"
        shot2.write_bytes(b"\x89PNG\r\n\x1a\nfake2")
        self.env = cx_env
        try:
            self.cli("evidence", "screenshot", str(shot2), "--target", "/orders")
        finally:
            self.env = env_bak
        out = self.hook("Stop", agent="codex", sid=cx, stop_hook_active=False)
        self.assertFalse(self.blocked(out, "Stop"), out)

        # 10. 사람 화면·장부 명령
        r = self.cli("brief", "--json")
        b = json.loads(r.stdout)
        self.assertTrue(b.get("installed"))
        r = self.cli("brief", "--html")
        self.assertTrue(Path(r.stdout.strip().splitlines()[-1]).is_file())
        r = self.cli("why", task_id)
        self.assertIn(task_id, r.stdout)
        self.cli("judge")
        actions = [json.loads(p.read_text(encoding="utf-8"))
                   for p in (root / ".harness" / "ledger" / "actions").glob("*.json")]
        gate_actions = [a for a in actions if (a.get("target") or {}).get("gate_id")]
        self.assertEqual(len(gate_actions), 1, actions)
        aid = gate_actions[0]["action_id"]
        r = self.cli("revert", aid, "--reason", "통합 실측")
        findings = [json.loads(p.read_text(encoding="utf-8"))
                    for p in (root / ".harness" / "ledger" / "findings").glob("*.json")]
        self.assertTrue(any(f.get("kind") == "revert" for f in findings), findings)
        out = self.hook("PreToolUse", agent="codex", sid=cx, tool_name="Write",
                        tool_input={"file_path": str(root / page), "content": wide})
        self.assertFalse(self.blocked(out, "PreToolUse"), "원복한 생성 게이트가 아직 막음: %s" % out)
        r = self.cli("diagnose", "run", "--no-open")   # 가짜 claude — LLM 실패 경로(리포트는 만든다)
        self.assertIn("리포트", r.stdout)

        # 11. remove — 등록한 것 원상복구
        self.cli("remove")
        self.assertFalse((root / ".claude" / "agents" / "vmc-qa.md").exists())
        self.assertFalse((root / ".codex" / "agents" / "vmc-qa.toml").exists())
        settings = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8")) \
            if (root / ".claude" / "settings.json").is_file() else {}
        self.assertNotIn("vmc-harness hook", json.dumps(settings))
        self.assertFalse((root / ".codex" / "hooks.json").exists() and
                         "vmc-harness hook" in (root / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        self.assertNotIn("vmc-harness:begin", (root / "AGENTS.md").read_text(encoding="utf-8")
                         if (root / "AGENTS.md").is_file() else "")
        self.assertFalse(hook_file.is_file() and "vmc-harness" in hook_file.read_text(encoding="utf-8"))
        self.assertFalse((self.home / "projects" / cfg["project_id"]).exists())
        self.assertFalse((self.tmp / "codex-home" / "config.toml").exists() and
                         "vmc-harness" in (self.tmp / "codex-home" / "config.toml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
