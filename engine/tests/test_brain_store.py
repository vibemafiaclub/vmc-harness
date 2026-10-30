import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from tests.util import HarnessTestCase

from vmc_harness.brain import conflict, install, mcp_server, promote, search, store, sync
from vmc_harness.core import ledger, notices
from vmc_harness.core.jsonio import read_json


def _card(title, judgment, signals, author="수민", origin="learning"):
    p = store.add_card(title, "상황 설명", judgment, signals, "급한 핫픽스일 때", "사람 원문", author,
                       {"project": "p", "session": "s"}, origin)
    return p.stem


SYNTH = [
    ("사용자에게 넘기기 전에 끝까지 해본다", "도구로 먼저 시도하고 안 되는 것만 넘긴다", ["확인 요청", "체크리스트"]),
    ("검증은 실측으로", "추정을 실측처럼 말하지 않는다", ["반영했나", "확인"]),
    ("작은 페이지부터 바꾼다", "한 페이지 먼저 적용하고 넓힌다", ["화면 수정", "페이지"]),
]


class StoreTest(HarnessTestCase):
    def test_card_roundtrip_and_git(self):
        cid = _card("한 페이지 먼저", "전체 적용 전에 한 페이지로 확인", ["페이지 수정", "디자인 변경"])
        c = store.get_card(cid)
        self.assertEqual(c["title"], "한 페이지 먼저")
        self.assertEqual(c["signals"], ["페이지 수정", "디자인 변경"])
        self.assertEqual(c["not_when"], "급한 핫픽스일 때")
        self.assertEqual(c["quote"], "사람 원문")
        self.assertEqual(c["source"], {"project": "p", "session": "s"})
        self.assertNotIn("uses", c)
        log = subprocess.run(["git", "log", "--oneline"], cwd=store.brain_dir(), capture_output=True, text=True).stdout
        self.assertIn(cid, log)
        store.set_status(cid, "deleted")
        self.assertIsNone(store.get_card(cid))

    def test_author_view(self):
        cid = _card("한 페이지 먼저", "판단", ["s"], author="김팀원")
        c = store.get_card(cid)
        self.assertEqual(store.view(c, viewer="김팀원")["author"], "김팀원")
        self.assertNotIn("author", store.view(c, viewer="고동현"))
        self.assertNotIn("author", store.learned(viewer="고동현")[0])

    def test_bm25_korean(self):
        ids = [_card(t, j, s) for t, j, s in SYNTH]
        r = search.search("페이지 화면을 고쳐야 하는데 어디부터", use_llm=False)
        self.assertEqual(r[0]["id"], ids[2])
        self.assertEqual(r[0]["method"], "bm25")

    def test_llm_select_and_fallback(self):
        ids = [_card(t, j, s) for t, j, s in SYNTH]
        with mock.patch("vmc_harness.core.llm.available", return_value=True), \
                mock.patch("vmc_harness.core.llm.claude_p", return_value={"text": '{"ids": ["%s", "nope"]}' % ids[1], "tokens": 5, "sec": 1}) as cp:
            r = search.search("반영됐는지 확인", root=self.root)
            self.assertEqual([c["id"] for c in r], [ids[1]])
            self.assertIn("카드 목록", cp.call_args.kwargs["system"])
            self.assertEqual(cp.call_args.kwargs["model"], "haiku")
        with mock.patch("vmc_harness.core.llm.available", return_value=True), \
                mock.patch("vmc_harness.core.llm.claude_p", return_value=None):
            r = search.search("페이지 화면", root=self.root)
            self.assertEqual(r[0]["method"], "bm25")
        from vmc_harness.core import events
        self.assertEqual(len(events.read(self.root, "card_search")), 2)
        self.assertTrue(events.read(self.root, "exposure"))

    def test_without_env(self):
        ids = [_card(t, j, s) for t, j, s in SYNTH]
        with mock.patch.dict(os.environ, {"VMC_HARNESS_WITHOUT": ids[2]}):
            r = search.search("페이지 화면", use_llm=False)
            self.assertNotIn(ids[2], [c["id"] for c in r])

    def test_card_exposure_counts_for_action(self):
        from vmc_harness.ledger.selfcheck import make_card_action
        from vmc_harness.ledger import metrics
        cid, a = make_card_action(self.root, {"title": "한 페이지 먼저", "judgment": "한 페이지로 확인",
                                              "signals": ["페이지"], "quote": "앞으로 한 페이지 먼저"},
                                  "explicit_request", {"type": "explicit_request", "quote": "앞으로 한 페이지 먼저"},
                                  "취향", "h", True)
        search.search("페이지", use_llm=False, root=self.root)
        metrics.rollup(self.root)
        self.assertEqual(metrics.exposures(self.root, a["action_id"]), 1)

    def test_conflict_disables(self):
        from vmc_harness.ledger.selfcheck import make_card_action
        cid, a = make_card_action(self.root, {"title": "t", "judgment": "j", "quote": "q"}, "learning",
                                  {"type": "learning", "ref": "C-1"}, "취향", "h", False)
        self.assertFalse(conflict.record(cid, "지시대로 전체 적용", self.root)["disabled"])
        r = conflict.record(cid, None, self.root)
        self.assertTrue(r["disabled"])
        self.assertEqual(store.get_card(cid)["status"], "disabled")
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["status"], "reverted")

    def test_promote_explicit_only(self):
        from vmc_harness.ledger.selfcheck import make_card_action
        from vmc_harness.ledger import exposure, metrics
        c1, a1 = make_card_action(self.root, {"title": "t1", "judgment": "j", "quote": "앞으로 X"}, "explicit_request",
                                  {"type": "explicit_request", "quote": "앞으로 X"}, "취향", "h", True)
        c2, a2 = make_card_action(self.root, {"title": "t2", "judgment": "j", "quote": "q"}, "learning",
                                  {"type": "learning", "ref": "C-1"}, "취향", "h", False)
        for _ in range(3):
            search.search("t1 t2 j", use_llm=False, root=self.root)
        metrics.rollup(self.root)
        with mock.patch("vmc_harness.gates.generate.from_request", return_value=None) as gen:
            self.assertEqual(promote.check(self.root), [c1])
        self.assertEqual(gen.call_args.kwargs["promoted_from"], c1)
        self.assertTrue(read_json(self.root / ".harness" / "cache" / "promotions.json"))
        self.assertTrue(any(n["kind"] == "card_promotable" for n in notices.pending(self.root)))
        self.assertEqual(promote.check(self.root), [])  # 한 번만

    def test_delete_card_is_finding(self):
        from vmc_harness.commands.card import delete_card
        from vmc_harness.ledger.selfcheck import make_card_action
        cid, a = make_card_action(self.root, {"title": "t", "judgment": "j", "quote": "q"}, "learning",
                                  {"type": "learning", "ref": "C-1"}, "취향", "h", False)
        delete_card(self.root, cid)
        f = ledger.list_findings(self.root)[0]
        self.assertEqual((f["kind"], f["button"], f["target"]["card"]), ("button", "delete", cid))
        self.assertIsNone(store.get_card(cid))
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["status"], "reverted")


class McpTest(HarnessTestCase):
    def test_handle(self):
        _card("한 페이지 먼저", "한 페이지로 확인", ["페이지"])
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "vmc-brain")
        self.assertIsNone(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["search_cards", "get_card"])
        with mock.patch("vmc_harness.core.llm.available", return_value=False):
            r = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": "search_cards", "arguments": {"situation": "페이지 수정"}}}, self.root)
        self.assertIn("한 페이지 먼저", r["result"]["content"][0]["text"])
        self.assertEqual(mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "x"})["error"]["code"], -32601)

    def test_stdio_subprocess(self):
        cid = _card("한 페이지 먼저", "한 페이지로 확인", ["페이지"])
        engine = str(Path(__file__).resolve().parent.parent)
        env = dict(os.environ, PYTHONPATH=engine, PATH="/usr/bin:/bin")  # claude 없음 → BM25
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_card", "arguments": {"id": cid}}}]
        p = subprocess.run([sys.executable, "-m", "vmc_harness", "mcp"], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                           capture_output=True, text=True, env=env, cwd=str(self.root), timeout=30)
        lines = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        self.assertEqual([l["id"] for l in lines], [1, 2])
        self.assertIn("원문 인용", lines[1]["result"]["content"][0]["text"])


class InstallTest(HarnessTestCase):
    def test_register_unregister(self):
        (self.root / ".codex").mkdir()
        (self.root / ".codex" / "config.toml").write_text('model = "x"\n', encoding="utf-8")
        res = install.register_mcp(self.root)
        install.register_mcp(self.root)  # 두 번 불러도 한 번만
        mj = read_json(self.root / ".mcp.json")
        self.assertEqual(mj["mcpServers"]["vmc-brain"]["command"], "vmc-harness")
        self.assertEqual(read_json(self.root / ".claude" / "settings.json")["enabledMcpjsonServers"], ["vmc-brain"])
        toml = (self.root / ".codex" / "config.toml").read_text()
        self.assertEqual(toml.count("[mcp_servers.vmc-brain]"), 1)
        self.assertIn('model = "x"', toml)
        self.assertIn("codex_trusted", res)
        install.unregister_mcp(self.root)
        self.assertFalse((self.root / ".mcp.json").exists())
        self.assertNotIn("vmc-brain", (self.root / ".codex" / "config.toml").read_text())

    def test_codex_trusted(self):
        ch = self._tmp / "codexhome"
        ch.mkdir()
        cfg = ch / "config.toml"
        root = str(self.root.resolve())
        variants = [
            "[projects.%s]" % json.dumps(root, ensure_ascii=False),  # 기본 문자열(Windows는 역슬래시가 \\로 이스케이프된다)
            "[projects.'%s']" % root,  # 리터럴 문자열
        ]
        if os.name == "nt":  # Codex가 정규화한 Windows 경로: 긴 경로 접두·소문자·/ 구분자
            variants += ["[projects.'\\\\?\\%s']" % root, "[projects.'%s']" % root.lower().replace("\\", "/")]
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(ch)}):
            for head in variants:
                cfg.write_text('%s\ntrust_level = "trusted"\n' % head, encoding="utf-8")
                self.assertTrue(install.codex_trusted(self.root), head)
            cfg.write_text('[projects.%s]\ntrust_level = "untrusted"\n' % json.dumps(root), encoding="utf-8")
            self.assertFalse(install.codex_trusted(self.root))


class SyncTest(HarnessTestCase):
    def _remote_with_card(self):
        remote = self._tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        other = self._tmp / "other"
        subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True, capture_output=True)
        (other / "cards").mkdir()
        (other / "cards" / "K-000000-remote.md").write_text("---\nid: K-000000-remote\ntitle: 원격 카드\nstatus: active\norigin: learning\n---\n\n# 원격 카드\n\n## 판단\n\n원격 판단\n", encoding="utf-8")
        for args in (["add", "-A"], ["-c", "user.name=o", "-c", "user.email=o@o", "commit", "-q", "-m", "remote"], ["push", "-q", "origin", "HEAD:main"]):
            subprocess.run(["git"] + args, cwd=other, check=True, capture_output=True)
        return remote, other

    def test_link_merges_unrelated_and_pushes(self):
        remote, other = self._remote_with_card()
        local = _card("로컬 카드", "로컬 판단", ["x"])
        res = sync.link(str(remote))
        self.assertTrue(res["merged"], res)
        self.assertTrue(res["pushed"], res)
        ids = {c["id"] for c in store.list_cards()}
        self.assertEqual(ids, {local, "K-000000-remote"})
        subprocess.run(["git", "pull", "-q", "origin", "main"], cwd=other, check=True, capture_output=True)
        self.assertTrue((other / "cards" / ("%s.md" % local)).exists())
        # 다른 PC가 새 카드를 올리고 이쪽도 새 카드 → pull 합치기 + push
        (other / "cards" / "K-000001-remote.md").write_text("---\nid: K-000001-remote\ntitle: 둘째\nstatus: active\norigin: learning\n---\n\n# 둘째\n", encoding="utf-8")
        for args in (["add", "-A"], ["-c", "user.name=o", "-c", "user.email=o@o", "commit", "-q", "-m", "r2"], ["push", "-q", "origin", "HEAD:main"]):
            subprocess.run(["git"] + args, cwd=other, check=True, capture_output=True)
        _card("로컬 둘째", "판단", ["y"])
        r = sync.pull()
        self.assertTrue(r["pulled"], r)
        self.assertTrue(r.get("pushed"), r)
        self.assertIn("K-000001-remote", {c["id"] for c in store.list_cards()})

    def test_pull_without_remote(self):
        self.assertEqual(sync.pull(), {"remote": False})
