"""실행 뒤 자동 증거 (spec 02 완료 게이트 — "증거는 하네스가 만든 산출물만").

에이전트가 마지막 편집 뒤 이미 테스트를 돌리거나 화면을 찍었는데 `vmc-harness evidence`로 등록하지 않아 종료가 막히던
절차 막힘(현장 실측 16건)을 줄인다. 훅이 직접 본 성공 실행만 증거 마커로 남긴다 — 에이전트의 "확인했습니다"가 아니다.
- 테스트: 검증 명령 기록(verify_timing, 종류 test)이 종료 코드 0이면 test_run 마커(새 실패 0건)
- 화면 캡처: 브라우저 캡처 호출(headless `--screenshot`·`--print-to-pdf`·Playwright·Puppeteer `.screenshot(`·`.pdf({`·
  CDP `captureScreenshot`·`printToPDF`·browser-use `capture_screenshot`·macOS `screencapture`)이 명령이나 heredoc 본문,
  또는 인터프리터로 바로 돌린 스크립트 파일에 있는 성공 실행이면 screenshot 마커(자동 표시). 읽기 명령(grep·cat…) 안 글자는 아니다
- curl 같은 HTTP 호출은 자동 증거가 아니다 — 배포 대기·문자열 확인과 동작 확인을 기록만으로 가를 수 없다(현장: 실제 확인이 없던
  차단도 curl은 있었다). API는 `vmc-harness evidence api_call`이나 테스트로
- 완료 게이트는 자동 화면 캡처를 페이지(또는 영향 반경) 변경에만 인정한다(completion)
"""

import os
import re

from vmc_harness.core import config
from vmc_harness.core.clock import now
from vmc_harness.gates import evidence, shell, state

AUTO = "auto"
CAPTURE = re.compile(r"--screenshot\b|--print-to-pdf\b|\bscreencapture\b|capture_?[sS]creenshot|captureScreenshot|printToPDF"
                     r"|\.screenshot\(|\.pdf\(\s*(?:\{|\n|path\s*=)|\bplaywright\s+screenshot\b")
READERS = {"grep", "rg", "egrep", "fgrep", "cat", "less", "more", "head", "tail", "sed", "awk", "echo", "printf", "git",
           "vmc-harness", "ls", "find", "wc", "diff", "bat", "jq"}
LABEL = "셸 실행 기록 — 자동"


def _script(args, cwd):
    """인터프리터 인자 중 첫 스크립트 파일(없으면 None)."""
    for a in args:
        a = str(a)
        if a.startswith("-"):
            if a in ("-c", "-e", "-m"):
                return None
            continue
        p = os.path.expanduser(a)
        if not os.path.isabs(p):
            p = os.path.join(cwd or ".", p)
        return p if os.path.isfile(p) else None
    return None


def capture_tool(command, cwd=None, root=None):
    """브라우저 캡처를 한 명령이면 그 이름(로그용 — 원문 명령은 남기지 않는다), 아니면 None."""
    cap = int(config.get("gates.auto_evidence_script_max_bytes", root))
    for seg, body in shell._segments_heredoc(command or ""):
        toks = [str(t) for t in seg]
        while toks and ("=" in toks[0] and not toks[0].startswith("-")):
            toks = toks[1:]
        while toks and shell._base(toks[0]) in shell.PREFIX_CMDS:
            toks = toks[1:]
        if not toks:
            continue
        cmd = shell._base(toks[0])
        if cmd in shell.CD_CMDS:
            t = shell._cd_target(toks[1:])
            if t is not shell._UNKNOWN:
                t = os.path.expanduser(os.path.expandvars(t))
                cwd = t if os.path.isabs(t) else os.path.join(cwd or ".", t)
            continue
        if cmd in READERS:
            continue
        if CAPTURE.search(" ".join(toks[1:])) or (body and CAPTURE.search(body)):
            return cmd
        if cmd in shell.INTERPRETERS or cmd in ("tsx", "ts-node"):
            p = _script(toks[1:], cwd)
            try:
                if p and os.path.getsize(p) <= cap:
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        if CAPTURE.search(f.read()):
                            return "%s %s" % (cmd, os.path.basename(p))
            except OSError:
                continue
    return None


def on_post(ctx, verify=None):
    """성공한 셸 실행(PostToolUse) 뒤 — 자동 증거 마커를 남기면 그 마커, 아니면 None."""
    if ctx.tool_name != "Bash" or not ctx.command or not ctx.root or (ctx.tool_input or {}).get("run_in_background"):
        return None
    from vmc_harness.gates import verify_timing
    rc, interrupted = verify_timing.exit_code(ctx.raw, False)
    if interrupted or rc not in (0, None):
        return None
    key = state.task_key(ctx.root, ctx.session_id, ctx.task_id)
    at = now().isoformat()
    if verify and "test" in (verify.get("kinds") or []) and verify.get("exit_code") == 0:
        summary = {"rc": 0, "failures": 0, "new_failures": 0, "unparsed_failure": False, AUTO: "verify_run"}
        return evidence.make(ctx.root, key, "test_run", "%s (%s)" % (verify.get("gate"), LABEL),
                             data={AUTO: True, "gate": verify.get("gate"), "ended_at": verify.get("ended_at"),
                                   "sec": verify.get("sec"), "summary": summary})
    tool = capture_tool(ctx.command, ctx.cwd, ctx.root)
    if tool:
        return evidence.make(ctx.root, key, "screenshot", "%s (%s)" % (tool, LABEL),
                             data={AUTO: True, "tool": tool, "at": at, "summary": {AUTO: "capture"}})
    return None


def is_auto_capture(marker):
    return marker.get("kind") == "screenshot" and (marker.get("summary") or {}).get(AUTO) == "capture"
