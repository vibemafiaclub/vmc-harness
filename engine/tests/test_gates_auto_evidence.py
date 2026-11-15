"""완료 게이트 — 마지막 편집 뒤 이 세션이 이미 돌린 테스트·화면 캡처를 자동 증거로 (spec 02 완료 게이트).

현장 실측(0.1.11, 한 레포 6일): 동작 증거 차단 24건 중 16건은 에이전트가 편집 뒤 이미 캡처·테스트를 했는데
`vmc-harness evidence`로 등록하지 않아 막힌 절차 막힘이었다. 훅이 본 성공 실행(테스트 종료 코드 0, 브라우저 캡처 호출)만
마커로 남긴다. curl은 자동 증거가 아니다(실제 확인이 없던 차단에도 배포 대기·문자열 확인 curl은 있었다). 픽스처는 합성이다.
"""

import time

from vmc_harness.gates import auto_evidence, state

from tests.test_gates_safety import GatesCase, TaskMixin

OK = {"tool_response": {"stdout": "", "stderr": "", "interrupted": False}, "duration_ms": 1200}
BROWSER = "browser-tool <<'PY'\nnew_tab('http://localhost:3000/settle')\nprint(capture_screenshot('/tmp/vmh-s.png'))\nPY"


class TestCaptureTool(GatesCase):
    def test_detects_browser_capture_only(self):
        self.assertTrue(auto_evidence.capture_tool(BROWSER))
        self.assertTrue(auto_evidence.capture_tool(
            '"/Applications/Chrome" --headless=new --screenshot=/tmp/a.png http://localhost:3000/x'))
        self.assertTrue(auto_evidence.capture_tool("npx playwright screenshot http://localhost:3000 /tmp/a.png"))
        self.assertIsNone(auto_evidence.capture_tool("grep -rn captureScreenshot src | head"))
        self.assertIsNone(auto_evidence.capture_tool("curl -s -o /dev/null -w '%{http_code}' http://localhost:3000/x"))
        self.assertIsNone(auto_evidence.capture_tool("vmc-harness evidence screenshot /tmp/a.png --target x"))

    def test_script_file_with_capture_call(self):
        self.write("scripts/print_deck.py", "from playwright.sync_api import sync_playwright\n"
                                             "def go(p):\n    p.pdf({'path': 'out.pdf'})\n    p.screenshot(path='a.png')\n")
        self.write("scripts/count.py", "print(1)\n")
        self.assertEqual(auto_evidence.capture_tool("python3 scripts/print_deck.py /decks/a out.pdf", cwd=str(self.root)),
                         "python3 print_deck.py")
        self.assertIsNone(auto_evidence.capture_tool("python3 scripts/count.py", cwd=str(self.root)))
        self.write("tools/render.py", "def go(page):\n    page.pdf(\n        path='o.pdf',\n    )\n")
        self.assertTrue(auto_evidence.capture_tool("cd tools; python3 render.py /x o.pdf", cwd=str(self.root)))
        self.write("tools/stats.py", "from scipy.stats import norm\nprint(norm.pdf(0.5))\n")
        self.assertIsNone(auto_evidence.capture_tool("cd tools && python3 stats.py", cwd=str(self.root)))


class TestAutoEvidenceGate(TaskMixin, GatesCase):
    def setUp(self):
        super().setUp()
        self.write("fe/pages/settle.tsx", "export const A = 1\n")
        self.write("fe/api/settle/route.ts", "export const R = 1\n")
        self.commit()
        self.hook("SessionStart", source="startup")
        self.set_task()

    def stop(self):
        return self.hook("Stop", stop_hook_active=False)

    def edit(self, rel, text):
        self.write(rel, text)
        state.note_writes(self.root, self.session, [rel])
        time.sleep(0.05)

    def bash_ok(self, cmd):
        self.hook("PostToolUse", "Bash", {"command": cmd}, **OK)

    def test_capture_after_page_edit_passes_and_later_edit_blocks(self):
        self.edit("fe/pages/settle.tsx", "export const A = 2\n")
        self.assertEqual(self.stop().get("decision"), "block")
        self.bash_ok(BROWSER)
        self.assertEqual(self.stop(), {})
        self.edit("fe/pages/settle.tsx", "export const A = 3\n")   # 캡처 뒤 편집 — 다시 증거가 필요하다
        self.assertEqual(self.stop().get("decision"), "block")

    def test_api_change_needs_test_not_capture_or_curl(self):
        self.edit("fe/api/settle/route.ts", "export const R = 2\n")
        self.bash_ok("curl -s -X POST http://localhost:3000/api/settle -d '{}'")
        self.bash_ok(BROWSER)
        self.assertEqual(self.stop().get("decision"), "block")
        self.bash_ok("pytest -q tests/test_settle.py")
        self.assertEqual(self.stop(), {})

    def test_failed_test_is_not_evidence(self):
        self.edit("fe/api/settle/route.ts", "export const R = 2\n")
        self.hook("PostToolUseFailure", "Bash", {"command": "pytest -q"}, error="Exit code 1")
        self.assertEqual(self.stop().get("decision"), "block")
