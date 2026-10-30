"""고객 파일 쓰기가 원래 형식을 지키는가 (B4·B5 재현 — Windows CRLF+BOM 파일을 흉내 낸 합성 데이터).

- AGENTS.md: CRLF+BOM 파일에 블록을 넣고·바꾸고·빼도 블록 밖 바이트는 그대로, 블록도 CRLF
- CLAUDE.md: `@AGENTS.md`가 없으면 한 줄 덧붙임(줄끝·BOM 유지), 있으면 그대로, remove가 그 줄만 걷음
- .codex/config.toml(QA 블록): 같은 방식
"""

import io
from contextlib import redirect_stdout
from unittest import mock

from tests.util import HarnessTestCase
from vmc_harness.core import textfile
from vmc_harness.gates import rules as grules

BOM = b"\xef\xbb\xbf"
CUSTOMER = "# 프로젝트 지침\r\n\r\n- 줄끝은 CRLF\r\n- 한글 경로 그대로\r\n"


class CustomerFileTest(HarnessTestCase):
    def _bytes(self, rel):
        return (self.root / rel).read_bytes()

    def test_agents_crlf_bom_roundtrip(self):
        orig = BOM + CUSTOMER.encode("utf-8")
        (self.root / "AGENTS.md").write_bytes(orig)
        grules.render(self.root)
        b = self._bytes("AGENTS.md")
        self.assertTrue(b.startswith(orig))  # 고객 내용 바이트 그대로(BOM 포함)
        added = b[len(orig):]
        self.assertIn(b"vmc-harness:begin", added)
        self.assertNotIn(b"\n", added.replace(b"\r\n", b""))  # 블록도 CRLF — 맨 LF 없음
        # 블록 갱신(규칙 추가) — 블록 밖은 그대로
        grules.add_explicit(self.root, "테스트는 지우지 않는다", {"date": "2026-10-30", "area": "be/"})
        grules.render(self.root)
        b2 = self._bytes("AGENTS.md")
        self.assertTrue(b2.startswith(orig))
        self.assertIn("테스트는 지우지 않는다".encode("utf-8"), b2)
        self.assertNotIn(b"\n", b2.replace(b"\r\n", b""))
        # 블록 빼기 → 원래 바이트로
        self.assertTrue(grules.unrender(self.root))
        self.assertEqual(self._bytes("AGENTS.md"), orig)

    def test_lf_file_stays_lf_and_non_utf8_bytes_survive(self):
        orig = "# a\n\nb\n".encode("utf-8") + "한글".encode("cp949") + b"\n"  # UTF-8이 아닌 바이트가 섞인 파일
        (self.root / "AGENTS.md").write_bytes(orig)
        grules.render(self.root)
        b = self._bytes("AGENTS.md")
        self.assertTrue(b.startswith(orig))
        self.assertNotIn(b"\r\n", b)
        grules.unrender(self.root)
        self.assertEqual(self._bytes("AGENTS.md"), orig)

    def test_textfile_detect(self):
        self.assertEqual(textfile.detect_eol("a\r\nb\r\nc\n"), "\r\n")
        self.assertEqual(textfile.detect_eol("a\nb"), "\n")
        self.assertEqual(textfile.detect_eol(""), "\n")

    def test_claude_import_added_kept_and_removed(self):
        orig = BOM + "# Claude 전용 지침\r\n- 따로 관리\r\n".encode("utf-8")
        (self.root / "CLAUDE.md").write_bytes(orig)
        self.assertEqual(grules.ensure_claude_import(self.root), "added")
        b = self._bytes("CLAUDE.md")
        self.assertEqual(b, orig + b"@AGENTS.md\r\n")
        self.assertEqual(grules.ensure_claude_import(self.root), "present")  # 두 번 넣지 않는다
        self.assertEqual(self._bytes("CLAUDE.md"), b)
        self.assertEqual(textfile.read(self.root / "CLAUDE.md")[0][:-len("@AGENTS.md\r\n")],
                         grules.remove_claude_import(textfile.read(self.root / "CLAUDE.md")[0]))
        # 이미 불러오는 형태들은 그대로
        for text in ("See @AGENTS.md\n", "@./AGENTS.md\n"):
            (self.root / "CLAUDE.md").write_text(text, encoding="utf-8")
            self.assertEqual(grules.ensure_claude_import(self.root), "present")
        # 줄바꿈 없이 끝난 파일 — 파일 줄끝으로 한 줄 띄워 붙인다
        (self.root / "CLAUDE.md").write_bytes(b"x\r\ny")
        grules.ensure_claude_import(self.root)
        self.assertEqual(self._bytes("CLAUDE.md"), b"x\r\ny\r\n@AGENTS.md\r\n")

    def test_init_reports_import_and_remove_strips_it(self):
        from vmc_harness.bootstrap import init, remove, report
        from vmc_harness.core.jsonio import read_json
        orig = "# Claude 전용\r\n".encode("utf-8")
        (self.root / "CLAUDE.md").write_bytes(orig)
        (self.root / "AGENTS.md").write_bytes(CUSTOMER.encode("utf-8"))
        self.commit("base")
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(report, "to_pdf", return_value=(None, "x")):
            init.run(self.root, dry_system=True, run_tests=False, use_llm=False, do_diagnose=False,
                     learning=False, open_report=False, out=print)
        self.assertIn("`@AGENTS.md` 한 줄을 덧붙였어요", buf.getvalue())
        self.assertEqual(self._bytes("CLAUDE.md"), orig + b"@AGENTS.md\r\n")
        self.assertTrue(read_json(self.root / ".harness" / "config.json").get("claude_import_added_at"))
        self.assertIn("<code>@AGENTS.md</code> 한 줄을 덧붙였습니다", report.build_html(self.root))
        with redirect_stdout(io.StringIO()):
            remove.run(self.root)
        self.assertEqual(self._bytes("CLAUDE.md"), orig)
        self.assertEqual(self._bytes("AGENTS.md"), CUSTOMER.encode("utf-8"))

    def test_codex_config_toml_keeps_crlf(self):
        from vmc_harness.qa import install
        orig = BOM + b'model = "x"\r\n\r\n[profiles.a]\r\nk = 1\r\n'
        p = self.root / ".codex" / "config.toml"
        p.parent.mkdir(parents=True)
        p.write_bytes(orig)
        install.register(self.root, agents=("codex",))
        b = p.read_bytes()
        self.assertTrue(b.startswith(orig))
        self.assertIn(b"[agents.vmc-qa]", b)
        self.assertNotIn(b"\n", b.replace(b"\r\n", b""))
        install.unregister(self.root)
        self.assertEqual(p.read_bytes(), orig)
