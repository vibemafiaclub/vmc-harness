"""셸 분석기 heredoc 본문 오인 (spec 02 — 고객 재현 D3 합성: 본문 안 `=>`·`>`를 리다이렉트로 읽어 경로로 잡음)."""

from tests.gates_util import GatesCase
from vmc_harness.core.jsonio import write_json
from vmc_harness.gates import shell

READ_LEDGER = ("python3 - <<'EOF'\nimport json, glob\nfor f in glob.glob('.harness/ledger/**/*.json', recursive=True):\n"
               "    d = json.load(open(f))\n    print(f, d.get('kind'), '=>', len(d))\nEOF")


class TestHeredocAnalysis(GatesCase):
    def test_heredoc_body_is_not_command(self):
        a = shell.analyze("cat > notes.md <<'EOF'\nconst f = (a) => a > b\nx >> y.txt\nEOF")
        self.assertEqual(a["writes"], ["notes.md"])
        self.assertFalse(a["opaque"])
        a = shell.analyze(READ_LEDGER)
        self.assertEqual(a["writes"], [])
        self.assertEqual(a["opaque_cmds"], ["python3"])

    def test_crlf_heredoc(self):
        a = shell.analyze("cat <<EOF > out.txt\r\nk => v\r\nEOF\r\nrm old.txt\r\n")
        self.assertEqual(a["writes"], ["out.txt"])
        self.assertEqual(a["deletes"], ["old.txt"])

    def test_shell_heredoc_body_is_command(self):
        a = shell.analyze("bash <<EOF\nrm -rf build\necho hi > log.txt\nEOF")
        self.assertEqual(a["deletes"], ["build"])
        self.assertEqual(a["writes"], ["log.txt"])
        self.assertTrue(shell.analyze("cat x.sh | bash")["opaque"])

    def test_hook_does_not_block_heredoc_arrow(self):
        write_json(self.root / ".harness/protected.json", {"paths": ["auth/**"]})
        out = self.hook("PreToolUse", "Bash", {"command": "cat > doc.md <<EOF\nlogin => auth/login.py\nEOF"})
        self.assertAllowed(out)
