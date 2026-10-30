"""검증 게이트 소요 실측(gates.verify_timing·ledger.metrics.verification_wait)과 운영 접속 설정 보호(gates.prod_config). 합성 데이터만."""

import json

from tests.gates_util import GatesCase
from vmc_harness.core import events
from vmc_harness.core.jsonio import write_json
from vmc_harness.gates import meta, prod_config, verify_timing
from vmc_harness.hooks import dispatcher

PROD_ENV = "DATABASE_URL=mssql://app:pw@sqlprod01:1433/DIPortal\n"
APPSETTINGS_PROD = json.dumps({"ConnectionStrings": {"Main": "Server=sqlprod01;Database=DIPortal;User Id=app;Password=x;"}})


def _facts(root, commands):
    write_json(root / ".harness" / "cache" / "facts.json",
               {"generated_at": "2026-10-23T00:00:00+09:00",
                "facts": {"commands": {"value": commands, "command": "synthetic"}}})


def _cmd(name, kind, source="package.json", run=None):
    return {"dir": ".", "kind": kind, "name": name, "run": run or "npm run %s" % name, "source": source, "command": "x"}


class VerifyClassifyTest(GatesCase):
    def g(self, command):
        h = verify_timing.classify(str(self.root), command)
        return (h["kinds"], h["gate"]) if h else None

    def test_patterns_conservative(self):
        self.assertEqual(self.g("python -m pytest -q tests/a.py"), (["test"], "pytest"))
        self.assertEqual(self.g("pytest"), (["test"], "pytest"))
        self.assertEqual(self.g("uv run pytest -x"), (["test"], "pytest"))
        self.assertEqual(self.g("npx eslint ."), (["lint"], "eslint"))
        self.assertEqual(self.g("npx tsc --noEmit"), (["typecheck"], "tsc"))
        self.assertEqual(self.g("dotnet build Portal.sln"), (["build"], "dotnet build"))
        self.assertEqual(self.g("cargo clippy"), (["lint"], "cargo clippy"))
        self.assertEqual(self.g("cd fe && npm run build"), (["build"], "npm run build"))
        self.assertEqual(self.g("black --check ."), (["lint"], "black"))
        self.assertIsNone(self.g("black ."))              # 고치기 명령
        self.assertIsNone(self.g("jest --watch"))         # 감시 모드
        self.assertIsNone(self.g("dotnet run"))
        self.assertIsNone(self.g("go run main.go"))
        self.assertIsNone(self.g("npm install"))
        self.assertIsNone(self.g("npm run foo"))          # 이름으로 모르는 스크립트
        self.assertIsNone(self.g("echo test && ls"))
        self.assertEqual(self.g("npm run lint && npm test"), (["lint", "test"], "npm run lint + npm test"))

    def test_facts_first(self):
        _facts(self.root, [_cmd("check", "test"), _cmd("test", "other"), _cmd("verify-all", "lint", "Makefile", "make verify-all"),
                           _cmd("scripts/ci.sh", "test", "scripts/ci.sh", "scripts/ci.sh")])
        self.assertEqual(self.g("npm run check"), (["test"], "npm run check"))
        self.assertIsNone(self.g("npm test"))             # facts가 test 스크립트를 검증 명령이 아니라고 안다
        self.assertEqual(self.g("make verify-all"), (["lint"], "make verify-all"))
        self.assertEqual(self.g("bash scripts/ci.sh"), (["test"], "scripts/ci.sh"))
        self.assertEqual(verify_timing.classify(str(self.root), "npm run check")["source"], "facts")

    def test_git_commit_only_with_customer_hooks(self):
        self.assertIsNone(self.g('git commit -m "x"'))    # 고객 훅 없음
        from vmc_harness.task import trailers
        trailers.install_commit_hook(str(self.root))      # 하네스 자신의 훅만 — 고객 게이트 아님
        self.assertIsNone(self.g('git commit -m "x"'))
        self.write(".git/hooks/pre-commit", "#!/bin/sh\nnpm test\n")
        self.assertEqual(self.g('git commit -m "x"'), (["commit_hooks"], "git commit (훅 포함 근사)"))
        self.assertTrue(verify_timing.classify(str(self.root), "git commit -m x")["approx"])
        self.assertIsNone(self.g('git commit --no-verify -m "x"'))


class VerifyDispatchTest(GatesCase):
    def _runs(self):
        return events.read(str(self.root), "verify_run")

    def test_claude_duration_and_failure(self):
        ti = {"command": "python -m pytest -q"}
        self.hook("PreToolUse", "Bash", ti, tool_use_id="tu1")
        self.hook("PostToolUse", "Bash", ti, tool_use_id="tu1", duration_ms=91234,
                  tool_response={"stdout": "ok", "stderr": "", "interrupted": False, "isImage": False})
        self.hook("PreToolUse", "Bash", ti, tool_use_id="tu2")
        out, rc = dispatcher.dispatch("PostToolUseFailure", "claude", json.dumps(self.raw(
            "PostToolUseFailure", "Bash", ti, tool_use_id="tu2", error="Exit code 1", is_interrupt=False, duration_ms=40000)))
        self.assertEqual((out, rc), ("", 0))
        r = self._runs()
        self.assertEqual([(x["gate"], x["sec"], x["exit_code"], x["ok"], x["timing"]) for x in r],
                         [("pytest", 91.234, 0, True, "agent"), ("pytest", 40.0, 1, False, "agent")])
        self.assertNotIn("command", r[0])                 # 원문 명령은 남기지 않는다
        # 하네스 오버헤드(gate_time)에 섞이지 않는다
        self.assertFalse([e for e in events.read(str(self.root), "gate_time") if "verify" in str(e.get("gate_id"))])

    def test_codex_pre_post_difference(self):
        ti = {"command": "npm run lint"}
        self.hook("PreToolUse", "Bash", ti, agent="codex", tool_use_id="c1")
        self.hook("PostToolUse", "Bash", ti, agent="codex", tool_use_id="c1", tool_response={"exit_code": 2, "output": "x"})
        r = self._runs()
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["timing"], r[0]["exit_code"]), ("pre_post", 2))
        self.assertGreaterEqual(r[0]["sec"], 0)

    def test_non_verify_and_background_not_recorded(self):
        self.hook("PreToolUse", "Bash", {"command": "ls -la"}, tool_use_id="a")
        self.hook("PostToolUse", "Bash", {"command": "ls -la"}, tool_use_id="a", duration_ms=5)
        ti = {"command": "pytest", "run_in_background": True}
        self.hook("PreToolUse", "Bash", ti, tool_use_id="b")
        self.hook("PostToolUse", "Bash", ti, tool_use_id="b", duration_ms=5)
        self.assertEqual(self._runs(), [])

    def test_failure_event_registered_for_claude_only(self):
        from vmc_harness.hooks import install
        c, x = {}, {}
        install.merge_hooks(c, "claude")
        install.merge_hooks(x, "codex")
        self.assertIn("PostToolUseFailure", c["hooks"])
        self.assertNotIn("PostToolUseFailure", x["hooks"])


class VerificationWaitTest(GatesCase):
    def test_weekly_wait_and_candidates_are_reference_only(self):
        from vmc_harness.core import ledger
        from vmc_harness.ledger import metrics
        evs = []
        for day in range(7):
            for _ in range(5):   # 주 35회 × 90초
                evs.append({"at": "2026-10-%02dT10:00:00+09:00" % (12 + day), "kind": "verify_run", "gate": "git commit (훅 포함 근사)",
                            "kinds": ["commit_hooks"], "approx": True, "sec": 90, "exit_code": 0})
            evs.append({"at": "2026-10-%02dT11:00:00+09:00" % (12 + day), "kind": "verify_run", "gate": "eslint",
                        "kinds": ["lint"], "sec": 4, "exit_code": 1 if day == 0 else 0})
        agg = metrics.aggregate(evs, pc="pc-1")
        for d, m in agg.items():
            write_json(ledger.metrics_path(str(self.root), d, "pc-1"), m)
        w = metrics.verification_wait(str(self.root), since="2026-10-12", until="2026-10-18")
        top = w["gates"][0]
        self.assertEqual((top["gate"], top["runs"], top["avg_sec"], top["runs_per_week"], top["wait_sec_per_week"]),
                         ("git commit (훅 포함 근사)", 35, 90.0, 35.0, 3150.0))
        self.assertEqual([c["gate"] for c in w["candidates"]], ["git commit (훅 포함 근사)"])
        self.assertIn("주 52분 대기", w["candidates"][0]["suggestion"])
        self.assertIn("근사", w["candidates"][0]["suggestion"])
        lint = [g for g in w["gates"] if g["gate"] == "eslint"][0]
        self.assertEqual((lint["fails"], lint["runs"]), (1, 7))
        self.assertIn("verification_wait", metrics.team(str(self.root))["reference"])
        self.assertEqual(ledger.list_findings(str(self.root)), [])   # 판정 입력(finding)을 만들지 않는다

    def test_short_window_not_extrapolated(self):
        from vmc_harness.ledger import metrics
        rows = [{"date": "2026-10-20", "verify": {"pytest": {"runs": 3, "sec": 300, "fails": 0, "unknown_exit": 0, "kinds": ["test"]}}}]
        w = metrics.verification_wait(str(self.root), rows=rows)
        self.assertEqual(w["gates"][0]["runs_per_week"], 3.0)
        self.assertEqual(w["candidates"], [])


class ProdConfigTest(GatesCase):
    def setUp(self):
        super().setUp()
        self.write(".env.production", PROD_ENV)
        self.write(".env.development", "DATABASE_URL=mssql://app:pw@sqldev01:1433/DIPortal\n")
        self.write("be/appsettings.Production.json", APPSETTINGS_PROD)
        self.write("be/appsettings.Development.json", APPSETTINGS_PROD.replace("sqlprod01", "sqldev01"))
        self.write("fe/.env.production.example", PROD_ENV)
        self.write("fe/realtime.json", '{"Server=rt01;": 1, "Password=x": 1}')
        self.write("fe/.env.live", "NEXT_PUBLIC_API=/api\n")

    def read(self, rel):
        return self.hook("PreToolUse", "Read", {"file_path": str(self.root / rel)})

    def sh(self, command, agent="claude"):
        return self.hook("PreToolUse", "Bash", {"command": command}, agent=agent)

    def seen(self):
        return events.read(str(self.root), "prod_config_seen")

    def test_read_tool(self):
        self.assertDenied(self.read(".env.production"), "운영 접속 정보 파일 읽기 차단됨(.env.production)")
        self.assertDenied(self.read("be/appsettings.Production.json"))
        for rel in (".env.development", "be/appsettings.Development.json", "fe/.env.production.example", "fe/realtime.json"):
            self.assertAllowed(self.read(rel))
        self.assertAllowed(self.read("fe/.env.live"))      # 이름만 운영 — 접속 정보 형태 없음 → 기록만
        self.assertEqual([(e["action"], e["path"]) for e in self.seen()], [("read", "fe/.env.live")])

    def test_shell_reads(self):
        for c in ("cat .env.production", "type be/appsettings.Production.json",
                  "grep DATABASE_URL .env.production", "cp .env.production .env", "head -5 < .env.production",
                  'bash -c "cat .env.production | grep URL"'):
            self.assertDenied(self.sh(c), "운영 접속 정보 파일 읽기 차단됨")
        self.assertDenied(self.sh('pwsh -Command "Get-Content -Path be/appsettings.Production.json"', agent="codex"))
        for c in ("cat .env.development", "ls -la", "cat fe/.env.production.example", "git status"):
            self.assertAllowed(self.sh(c))

    def test_launch_with_prod_env(self):
        self.assertDenied(self.sh("node --env-file=.env.production server.js"), "운영 접속 정보 파일로 실행 차단됨")
        self.assertDenied(self.sh("docker run --env-file .env.production app:latest"))
        r = self.sh("cd be && ASPNETCORE_ENVIRONMENT=Production dotnet run")
        self.assertDenied(r, "운영 설정으로 앱 실행 차단됨(ASPNETCORE_ENVIRONMENT=Production")
        self.assertDenied(self.sh('pwsh -Command "$env:ASPNETCORE_ENVIRONMENT=\'Production\'; dotnet run"', agent="codex"))
        self.assertDenied(self.sh("NODE_ENV=production npm start"))
        self.assertAllowed(self.sh("NODE_ENV=production npm run build"))   # 빌드는 앱 실행이 아니다
        self.assertAllowed(self.sh("ASPNETCORE_ENVIRONMENT=Development dotnet run"))
        self.assertAllowed(self.sh("git diff .env.production"))            # 읽기인지 모름 → 기록만
        acts = [e["action"] for e in self.seen()]
        self.assertIn("mention", acts)

    def test_launch_without_prod_file_records_only(self):
        (self.root / ".env.production").unlink()
        (self.root / "be" / "appsettings.Production.json").unlink()
        self.assertAllowed(self.sh("NODE_ENV=production node server.js"))
        self.assertEqual([e["action"] for e in self.seen()], ["launch"])

    def test_allow_list_and_meta_gate(self):
        cfg = json.loads((self.root / ".harness" / "config.json").read_text(encoding="utf-8"))
        before = json.dumps(cfg)
        cfg["prod_allow"] = {"files": [".env.production"], "names": ["LiveDB"]}
        after = json.dumps(cfg)
        v = meta.judge_file(str(self.root), ".harness/config.json", before, after)
        self.assertTrue(v and "운영 판정 허용 칸" in v[0], v)
        from vmc_harness.core import ledger
        a = meta.request_loosen(str(self.root), "T-261023-aaaaaa", "prod_allow", ["files:.env.production", "names:LiveDB"], "스테이징")
        ledger.answer_approval(str(self.root), a["approval_id"], "approve")
        self.assertEqual(meta.judge_file(str(self.root), ".harness/config.json", before, after), [])
        (self.root / ".harness" / "config.json").write_text(after, encoding="utf-8")
        self.assertAllowed(self.read(".env.production"))
        self.assertDenied(self.read("be/appsettings.Production.json"))
        from vmc_harness.db.client import looks_production
        self.assertIsNone(looks_production(str(self.root), "LiveDB"))     # 12 운영 이름 오탐 — 허용 칸
        self.assertEqual(looks_production(str(self.root), "LiveDB2"), "LiveDB2")

    def test_protected_entry_blocks_without_name_pattern(self):
        self.write("config/secrets.json", '{"db": "Server=a;Password=b;"}')
        self.assertAllowed(self.read("config/secrets.json"))
        write_json(self.root / ".harness" / "protected.json",
                   {"paths": [{"glob": "config/secrets.json", "kind": "prod_config"}]})
        self.assertDenied(self.read("config/secrets.json"))

    def test_proceed_anyway(self):
        self.assertDenied(self.read(".env.production"))
        self.hook("UserPromptSubmit", prompt="그거 스테이징이야, 그냥 진행")
        self.assertAllowed(self.read(".env.production"))
        self.assertDenied(self.read(".env.production"))

    def test_init_suggestion(self):
        from vmc_harness.bootstrap import protected_suggest
        c = {x["glob"]: x["category"] for x in protected_suggest.suggest(str(self.root))}
        self.assertEqual(c.get(".env.production"), "prod_config")
        self.assertEqual(c.get("be/appsettings.Production.json"), "prod_config")
        for rel in (".env.development", "fe/.env.production.example", "fe/realtime.json", "fe/.env.live"):
            self.assertNotIn(rel, c)

    def test_errors_fail_open(self):
        from unittest import mock
        with mock.patch.object(prod_config, "check", side_effect=RuntimeError("boom")):
            self.assertAllowed(self.read(".env.production"))
        self.assertTrue([e for e in events.read(str(self.root), "gate_error") if e.get("module") == "gates.prod_config"])
