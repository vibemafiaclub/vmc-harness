#!/usr/bin/env python3
"""VMC 하네스 Windows 실기 검증 키트 (release-gate.json `unverified` 확인용). 파일 하나, Python 3.9+ 표준 라이브러리만.

Windows PowerShell:
  irm https://raw.githubusercontent.com/vibemafiaclub/vmc-harness/main/scripts/win_verify.py -OutFile $env:TEMP\\win_verify.py; python $env:TEMP\\win_verify.py

하는 일(단계마다 따로 판정 — 한 단계가 실패해도 다음으로 가고, 마지막에 항상 결과를 모아 올린다):
  0 환경 수집        Windows·python·git·autocrlf·claude·codex 버전·경로, 기존 설치
  1 설치             공개 레포 clone → install/install.py(껍데기·PATH) → release.py build → 커널 설치(라이선스 없이) → 자동 업데이트 끔
  2 샌드박스·init    %TEMP% 아래 합성 git 레포(CRLF+BOM AGENTS.md 등) → init → 줄끝·BOM 보존·@AGENTS.md·훅·MCP 설정 판정, 카드 1장
  3 백그라운드·창    엔진 spawn_detached 자식 생존, job object(KILL_ON_JOB_CLOSE) 부모가 닫혀도 생존, 창 깜빡임(사람 Y/N + 0.1.4 방식 대조군)
  4 훅 비용          hook SessionStart/PreToolUse/PostToolUse/Stop 각 10회 실측(중앙값·p90)
  5 Claude Code      claude -p 세션 3개(정상 종료·강제 종료·새 세션) → 장부·마감·측정·판정 상태, MCP 연결(.cmd 문제)·CLAUDE_PROJECT_DIR
  6 Codex            codex exec 1회(훅·장부·MCP·신뢰), 대화형 TUI 알림은 선택(사람 Y/N)
  7 스케줄러         loop register → schtasks XML 확인 → 작업 실행 → loop tick → 해제
  8 정리             이번 실행이 만든 것만(샌드박스·스케줄러·카드). 설치한 하네스는 남긴다(--uninstall로 제거)
결과: results.json + summary.md + 증거 → zip(바탕화면) → gh가 되면 비공개 보관 레포 릴리스 `win-verify`에 업로드.

받는 쪽: python3 scripts/win_verify.py --analyze <zip|폴더>
맥·리눅스에서 돌리면 기본 격리(VMC_HARNESS_HOME을 임시 폴더로, PATH·사용자 설정 안 건드림 = init --dry-system). Windows 전용 단계는 skip.
"""

import argparse
import datetime as _dt
import hashlib
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
import zipfile
from pathlib import Path

REPO_URL = "https://github.com/vibemafiaclub/vmc-harness.git"
ARCHIVE_REPO = os.environ.get("VMC_VERIFY_ARCHIVE", "vibemafiaclub/vmc-harness-archive")
RELEASE_TAG = "win-verify"
IS_WIN = sys.platform == "win32"
CARD_KEYWORD = "zebra-lantern"
BENCH_RUNS = 10
FLICKER_ROUNDS = 10
FLICKER_GAP_S = 1.5
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
# 이 스크립트를 Claude Code·Codex 안에서 돌려도 바깥 세션 값이 샌드박스 세션에 섞이지 않게
CLEAN_ENV = ("VMC_HARNESS_NESTED", "VMC_HARNESS_SESSION_ID", "VMC_HARNESS_PROJECT", "CLAUDE_CODE_SESSION_ID",
             "CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
             "CLAUDE_PROJECT_DIR", "PYTHONPATH")

# 출고 기준 id → 근거 체크 id (summary 맨 위 표)
GATES = [
    ("win-week", "Windows 작업 장부·판정이 끊기지 않는가 (부분 — 며칠 사용은 별도)",
     ["install.kernel", "install.run-no-license", "init.run", "claude.hooks-fired", "claude.declare",
      "claude.ledger-turns", "claude.session-end", "claude.measured", "claude.idle-close", "claude.judge-state",
      "claude.health"]),
    ("win-scheduler", "Windows 작업 스케줄러 XML 등록·실행·절전 막기",
     ["sched.register", "sched.xml", "sched.run", "sched.tick", "sched.power"]),
    ("codex-tui", "Codex 대화형 TUI 알림·Windows codex.cmd",
     ["codex.trust", "codex.exec-hooks", "codex.mcp", "codex.tui"]),
    ("mcp-cmd (새)", "MCP 등록 명령 `vmc-harness`(확장자 없음)가 Windows에서 뜨는가",
     ["mcp.list", "mcp.search"]),
    ("claude-project-dir (새)", "MCP 서버에 CLAUDE_PROJECT_DIR가 오는가",
     ["mcp.project-dir-claude"]),
    ("window-flicker (새)", "백그라운드 실행 때 콘솔 창이 깜빡이지 않는가",
     ["flicker.human"]),
    ("bg-survive (새)", "훅이 띄운 백그라운드 자식이 부모 종료·job 닫힘 뒤에도 사는가",
     ["bg.detached", "bg.job-breakaway"]),
]

PROBE_SRC = r'''import json, os, sys, time
out, client = sys.argv[1], sys.argv[2]
env = {k: ("<set>" if k not in ("CLAUDE_PROJECT_DIR", "VMC_HARNESS_PROJECT", "VMC_HARNESS_HOME") else v)
       for k, v in os.environ.items() if k.startswith(("CLAUDE", "CODEX", "VMC_"))}
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "probe-%s-%d.json" % (client, os.getpid())), "w", encoding="utf-8") as f:
    json.dump({"client": client, "at": time.time(), "cwd": os.getcwd(), "env": env}, f)
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    mid, m = msg.get("id"), msg.get("method")
    if mid is None:
        continue
    if m == "initialize":
        res = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
               "capabilities": {"tools": {}}, "serverInfo": {"name": "vmc-verify-env", "version": "1"}}
    elif m == "tools/list":
        res = {"tools": []}
    else:
        res = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\n")
    sys.stdout.flush()
'''

PARENT_SRC = r'''import json, os, subprocess, sys, time
mode, work, eng, child = sys.argv[1:5]
sys.path.insert(0, eng)
go = os.path.join(work, "go-" + mode)
end = time.time() + 30
while not os.path.exists(go) and time.time() < end:
    time.sleep(0.05)
args = [sys.executable, child, work, mode, eng]
if mode == "control":   # 0.1.4 방식: DETACHED_PROCESS (콘솔 없음 → 손자 콘솔 앱마다 새 창)
    p = subprocess.Popen(args, creationflags=0x8 | 0x200, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
else:
    from vmc_harness.core import proc
    p = proc.spawn_detached(args)
with open(os.path.join(work, "parent-%s.json" % mode), "w", encoding="utf-8") as f:
    json.dump({"child_pid": p.pid, "parent_exit_at": time.time()}, f)
'''

CHILD_SRC = r'''import json, os, subprocess, sys, time, traceback
work, mode, eng = sys.argv[1:4]
def _fail(e):
    with open(os.path.join(work, "child-%s.json" % mode), "w", encoding="utf-8") as f:
        json.dump({"done": False, "error": "".join(traceback.format_exception_only(type(e), e)).strip()}, f)
try:
    exec(compile(open(__file__ + ".body", encoding="utf-8").read(), __file__ + ".body", "exec"))
except Exception as e:
    _fail(e)
'''

CHILD_BODY = r'''
rounds, gap = int(os.environ.get("VMC_VERIFY_ROUNDS", "10")), float(os.environ.get("VMC_VERIFY_GAP", "1.5"))
sys.path.insert(0, eng)
from vmc_harness.core import proc
in_job = None
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    b = wintypes.BOOL()
    if k32.IsProcessInJob(k32.GetCurrentProcess(), None, ctypes.byref(b)):
        in_job = bool(b.value)
start = time.time()
time.sleep(2)   # 부모가 끝나고(job이면 닫히고) 난 뒤에도 사는지
ok_engine = ok_plain = 0
for i in range(rounds):
    rc, _, _ = proc.run(["git", "--version"], timeout=30)          # 엔진 경로(창 없는 콘솔)
    ok_engine += rc == 0
    r = subprocess.run(["git", "--version"], capture_output=True)  # 플래그 없이(아무 도구나 그냥 부를 때)
    ok_plain += r.returncode == 0
    time.sleep(gap)
with open(os.path.join(work, "child-%s.json" % mode), "w", encoding="utf-8") as f:
    json.dump({"done": True, "in_job": in_job, "ok_engine": ok_engine, "ok_plain": ok_plain, "rounds": rounds,
               "start": start, "end": time.time()}, f)
'''


# ---------------------------------------------------------------- 공용

def now_iso():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def decode(b):
    if b is None:
        return ""
    if isinstance(b, str):
        return b
    if b[:2] in (b"\xff\xfe", b"\xfe\xff") or (len(b) > 3 and b[1:2] == b"\x00"):
        try:
            return b.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "oem", "mbcs", "cp949"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("latin-1")


def tail(s, n=1500):
    s = s or ""
    return s if len(s) <= n else "…" + s[-n:]


class Masker:
    """홈 경로·사용자 이름·PC 이름·비밀값 형태를 가린다."""
    SECRET = re.compile(r"(vmh_[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
                        r"xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+)")

    def __init__(self):
        home = str(Path.home())
        self.pairs = []
        for h in {home, home.replace("\\", "/"), home.replace("\\", "\\\\"), os.path.realpath(home)}:
            if len(h) > 3:
                self.pairs.append((h, "~"))
        self.pairs.sort(key=lambda p: -len(p[0]))
        words = set()
        for k in ("USERNAME", "USER", "LOGNAME", "COMPUTERNAME", "USERDOMAIN", "HOSTNAME"):
            v = os.environ.get(k)
            if v and len(v) >= 3:
                words.add(v)
        try:
            words.add(Path.home().name)
            hn = platform.node().split(".")[0]
            if len(hn) >= 3:
                words.add(hn)
        except Exception:
            pass
        self.words = sorted((w for w in words if len(w) >= 3), key=len, reverse=True)

    def __call__(self, s):
        if not isinstance(s, str):
            return s
        for a, b in self.pairs:
            s = s.replace(a, b)
        for w in self.words:
            s = re.sub(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(w), "<user>", s, flags=re.I)
        return self.SECRET.sub("<secret>", s)

    def obj(self, o):
        if isinstance(o, dict):
            return {self(k) if isinstance(k, str) else k: self.obj(v) for k, v in o.items()}
        if isinstance(o, list):
            return [self.obj(v) for v in o]
        return self(o)


def read_json(p, default=None):
    try:
        with open(str(p), "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def rmtree(p):
    def onerr(func, path, _exc):   # 커널은 읽기 전용으로 설치된다 — 부모 폴더·자신의 쓰기 권한을 풀고 다시
        try:
            os.chmod(os.path.dirname(path), 0o700)
            os.chmod(path, 0o700)
            func(path)
        except OSError:
            pass
    if Path(p).exists():
        shutil.rmtree(str(p), onerror=onerr)


def desktop_dir():
    if IS_WIN:
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0 and buf.value:
                return Path(buf.value)
        except Exception:
            pass
    d = Path.home() / "Desktop"
    return d if d.is_dir() else Path.home()


def interactive():
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def banner(lines):
    bar = "=" * 64
    print("\n" + bar)
    for ln in lines:
        print("  " + ln)
    print(bar, flush=True)


def _gitbash():
    """Claude Code가 Bash 도구로 쓰는 Git Bash(WSL bash 아님)."""
    cand = [os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")]
    g = shutil.which("git")
    if g:
        root = Path(g).resolve().parent.parent
        cand += [str(root / "bin" / "bash.exe"), str(root.parent / "bin" / "bash.exe")]
    cand += [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"]
    for c in cand:
        if c and Path(c).is_file():
            return c
    return None


# ---------------------------------------------------------------- 실행기

class Verify:
    def __init__(self, a):
        self.a = a
        self.t0 = time.time()
        self.stamp = _dt.datetime.now().strftime("%y%m%d-%H%M%S")
        self.work = Path(a.work or tempfile.gettempdir()).resolve() / ("vmc-verify-" + self.stamp)
        self.out = self.work / "out"
        self.evid = self.out / "evidence"
        self.evid.mkdir(parents=True, exist_ok=True)
        self.isolate = a.isolate or not IS_WIN
        self.home = (self.work / "home") if self.isolate else (
            Path(os.environ["VMC_HARNESS_HOME"]).expanduser() if os.environ.get("VMC_HARNESS_HOME") else Path.home() / ".vmc-harness")
        self.sandbox = self.work / "sandbox"
        self.repo = None
        self.version = None
        self.launcher = None
        self.engine = None   # 설치된 커널 engine/
        self.results = []
        self.env_info = {}
        self.mask = Masker()
        self.log_f = open(str(self.out / "log.txt"), "a", encoding="utf-8")
        self.cmdlog = open(str(self.out / "cmdlog.jsonl"), "a", encoding="utf-8")
        self.state = {"created": [], "card_id": None, "sessions": {}}
        self.answers = {}

    # ---- 기록
    def say(self, msg):
        line = "[%5.0fs] %s" % (time.time() - self.t0, msg)
        print(line, flush=True)
        self.log_f.write(self.mask(line) + "\n")
        self.log_f.flush()

    def add(self, cid, title, result, evidence="", detail=None):
        assert result in ("pass", "fail", "skip", "human")
        rec = {"id": cid, "title": title, "result": result, "evidence": self.mask(str(evidence)),
               "detail": self.mask.obj(detail) if detail is not None else None}
        self.results = [r for r in self.results if r["id"] != cid] + [rec]
        mark = {"pass": "통과", "fail": "실패", "skip": "건너뜀", "human": "사람 확인"}[result]
        self.say("  [%s] %s — %s" % (mark, title, tail(self.mask(str(evidence)), 200)))
        return rec

    def get(self, cid):
        for r in self.results:
            if r["id"] == cid:
                return r
        return None

    # ---- 명령
    def env(self, extra=None):
        e = dict(os.environ)
        for k in CLEAN_ENV:
            e.pop(k, None)
        e["PATH"] = str(self.home / "bin") + os.pathsep + e.get("PATH", "")
        if self.isolate:
            e["VMC_HARNESS_HOME"] = str(self.home)
        e["PYTHONIOENCODING"] = "utf-8"
        e["PYTHONUTF8"] = "1"
        if extra:
            e.update(extra)
        return e

    def run(self, args, cwd=None, timeout=300, input_text=None, env=None, log=True):
        t = time.time()
        try:
            r = subprocess.run([str(x) for x in args], cwd=str(cwd) if cwd else None, timeout=timeout,
                               input=input_text.encode("utf-8") if input_text is not None else None,
                               env=env or self.env(), capture_output=True)
            res = {"rc": r.returncode, "out": decode(r.stdout), "err": decode(r.stderr)}
        except subprocess.TimeoutExpired as e:
            res = {"rc": -9, "out": decode(e.stdout), "err": "timeout %ss" % timeout}
        except OSError as e:
            res = {"rc": -127, "out": "", "err": str(e)}
        res["sec"] = round(time.time() - t, 3)
        if log:
            self.cmdlog.write(json.dumps(self.mask.obj({"args": [str(x) for x in args], "cwd": str(cwd or ""),
                                                        "rc": res["rc"], "sec": res["sec"], "out": tail(res["out"], 3000),
                                                        "err": tail(res["err"], 2000)}), ensure_ascii=False) + "\n")
            self.cmdlog.flush()
        return res

    def vh(self, *args, cwd=None, timeout=300, input_text=None):
        return self.run([self.launcher] + list(args), cwd=cwd or self.sandbox, timeout=timeout, input_text=input_text)

    def py_engine(self, code, timeout=120):
        """설치된 커널 엔진으로 파이썬 한 토막."""
        e = self.env({"PYTHONPATH": str(self.engine)})
        return self.run([sys.executable, "-c", code], cwd=self.sandbox, timeout=timeout, env=e)

    # ---- 하네스 상태 읽기
    def project_id(self):
        return (read_json(self.sandbox / ".harness" / "config.json", {}) or {}).get("project_id")

    def local_dir(self):
        pid = self.project_id()
        return self.home / "projects" / pid if pid else None

    def local_tasks(self):
        d = self.local_dir()
        out = []
        if d and (d / "tasks").is_dir():
            for f in (d / "tasks").glob("*.json"):
                if f.name.count(".") == 1:
                    t = read_json(f)
                    if isinstance(t, dict):
                        t["_path"] = str(f)
                        out.append(t)
        return out

    def ledger_tasks(self):
        base = self.sandbox / ".harness" / "ledger" / "tasks"
        out = {}
        if base.is_dir():
            for f in base.rglob("*.json"):
                r = read_json(f)
                if isinstance(r, dict) and r.get("task_id"):
                    r["_path"] = str(f.relative_to(self.sandbox))
                    out[r["task_id"]] = r
        return out

    def events(self, kind=None):
        d = self.local_dir()
        rows = []
        if d and (d / "events.jsonl").is_file():
            for line in (d / "events.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if kind is None or r.get("kind") == kind:
                    rows.append(r)
        return rows

    def session_rec(self, sid):
        d = self.local_dir()
        return read_json(d / "sessions" / ("%s.json" % sid)) if d else None

    def ask(self, question, choices="yn"):
        if self.a.no_ask or not interactive():
            return None
        opts = "/".join(choices)
        while True:
            try:
                ans = input("\n>>> %s [%s]: " % (question, opts)).strip().lower()
            except EOFError:
                return None
            if ans[:1] in choices:
                return ans[:1]

    # ================================================================ 0 환경
    def step0_env(self):
        info = {"at": now_iso(), "platform": platform.platform(), "python": sys.version.split()[0],
                "python_exe": sys.executable, "isolate": self.isolate, "home": str(self.home)}
        if IS_WIN:
            info["win_ver"] = platform.version()
            r = self.run(["cmd", "/c", "ver"], timeout=20)
            info["cmd_ver"] = r["out"].strip()
        for name in ("git", "claude", "codex", "vmc-harness", "node"):
            info[name + "_which"] = shutil.which(name)
            if IS_WIN:
                info[name + "_where"] = self.run(["where", name], timeout=20, env=dict(os.environ))["out"].strip()
        info["git_version"] = self.run(["git", "--version"], timeout=30)["out"].strip()
        info["git_autocrlf"] = self.run(["git", "config", "--global", "core.autocrlf"], timeout=20)["out"].strip() or "(unset)"
        info["git_autocrlf_system"] = self.run(["git", "config", "--system", "core.autocrlf"], timeout=20)["out"].strip() or "(unset)"
        for name in ("claude", "codex"):
            exe = shutil.which(name)
            if exe:
                info[name + "_version"] = self.run([exe, "--version"], timeout=60)["out"].strip()[:200]
        if IS_WIN:
            info["git_bash"] = _gitbash()
        prev = None
        cur = self.home / "kernel" / "current"
        if cur.is_file():
            prev = cur.read_text(encoding="utf-8").strip()
        info["previous_kernel"] = prev
        info["previous_shell"] = (self.home / "bin" / "vmc_harness_shell.py").is_file()
        self.env_info = info
        (self.out / "env.json").write_text(json.dumps(self.mask.obj(info), ensure_ascii=False, indent=2), encoding="utf-8")
        self.add("env.python", "Python 3.9+", "pass" if sys.version_info >= (3, 9) else "fail", info["python"])
        self.add("env.git", "git 설치", "pass" if info.get("git_which") else "fail",
                 "%s · autocrlf global=%s system=%s" % (info["git_version"], info["git_autocrlf"], info["git_autocrlf_system"]))
        self.add("env.claude", "Claude Code 설치", "pass" if info.get("claude_which") else "skip",
                 info.get("claude_version") or "없음 — 5단계 건너뜀")
        self.add("env.codex", "Codex 설치", "pass" if info.get("codex_which") else "skip",
                 info.get("codex_version") or "없음 — 6단계 건너뜀")
        if info.get("claude_which") and not self.a.skip_claude:
            self._claude_auth(info["claude_which"])
        self.add("env.previous", "기존 하네스 설치", "pass",
                 ("기존 커널 %s → 이번 빌드로 덮어씀" % prev) if prev else "기존 설치 없음")

    def _claude_auth(self, claude):
        """Claude 로그인 사전 확인 — 만료면 세션 단계가 전부 '실패'로 잘못 찍힌다(2026-10-30 첫 실행). 로그인을 받거나 건너뛴다."""
        for attempt in range(3):
            r = self.run([claude, "-p", "reply with the single word ok", "--model", "haiku"], cwd=self.work, timeout=120)
            text = (r["out"] + r["err"]).lower()
            if r["rc"] == 0 and "authenticate" not in text and "login" not in text:
                self.add("env.claude-auth", "Claude 로그인 상태", "pass", "ok")
                return True
            print("\n[!] Claude Code 로그인이 안 돼 있습니다: %s" % tail(r["out"] + r["err"], 200).strip())
            print("    새 PowerShell 창에서 `claude` 를 실행해 로그인(/login)한 뒤 이 창으로 돌아오세요.")
            if self.ask("로그인했으면 y, Claude 단계를 건너뛰려면 n", "yn") != "y":
                break
        self.a.skip_claude = True
        self.add("env.claude-auth", "Claude 로그인 상태", "skip", "로그인 안 됨 — Claude 세션 단계 건너뜀")
        return False

    # ================================================================ 1 설치
    def step1_install(self):
        a = self.a
        if a.repo:
            self.repo = Path(a.repo).resolve()
        elif (Path(__file__).resolve().parent.parent / "engine" / "vmc_harness").is_dir() and not a.clone:
            self.repo = Path(__file__).resolve().parent.parent
        else:
            self.repo = self.work / "repo"
            r = self.run(["git", "clone", "--depth", "1", a.repo_url, self.repo], timeout=600)
            if r["rc"] != 0:
                self.add("install.clone", "레포 clone", "fail", tail(r["err"]))
                raise RuntimeError("clone 실패")
        commit = self.run(["git", "rev-parse", "HEAD"], cwd=self.repo, timeout=30)["out"].strip()
        self.env_info["repo_commit"] = commit
        self.add("install.clone", "레포 준비", "pass", "%s @ %s" % ("clone" if self.repo == self.work / "repo" else "로컬", commit[:10]))

        args = [sys.executable, self.repo / "install" / "install.py"] + (["--no-path"] if self.isolate else [])
        r = self.run(args, timeout=120)
        lp = self.home / "bin" / ("vmc-harness.cmd" if IS_WIN else "vmc-harness")
        self.launcher = str(lp)
        self.add("install.shell", "CLI 껍데기 설치(install.py)", "pass" if r["rc"] == 0 and lp.is_file() else "fail",
                 tail(r["out"] + r["err"], 400))

        r = self.run([sys.executable, self.repo / "scripts" / "release.py", "build", "--out", self.work / "build"],
                     cwd=self.repo, timeout=300)
        zpath = r["out"].strip().splitlines()[0] if r["rc"] == 0 and r["out"].strip() else None
        m = re.search(r"version (\S+)", r["out"])
        self.version = m.group(1) if m else None
        self.add("install.build", "릴리스 zip 빌드", "pass" if zpath and Path(zpath).is_file() else "fail",
                 "%s (%s)" % (Path(zpath).name if zpath else "-", self.version) if zpath else tail(r["err"]))
        if not zpath:
            raise RuntimeError("build 실패")

        code = ("import sys; sys.path.insert(0, %r)\n"
                "from vmc_harness.dist import shell\n"
                "d = shell.kernel_root() / %r\n"
                "shell.rmtree(d) if d.exists() else None\n"
                "v = shell.install_release(%r); shell.set_current(v); print(v)\n") % (
                    str(self.repo / "engine"), self.version, zpath)
        r = self.run([sys.executable, "-c", code], timeout=300, env=self.env())
        self.engine = self.home / "kernel" / (self.version or "?") / "engine"
        ok = r["rc"] == 0 and r["out"].strip().endswith(self.version or "?") and self.engine.is_dir()
        self.add("install.kernel", "커널 설치(install_release·set_current)", "pass" if ok else "fail",
                 tail(r["out"] + r["err"], 400))

        r = self.vh("update", "--auto", "off", cwd=self.work)
        self.add("install.auto-off", "자동 업데이트 끔", "pass" if r["rc"] == 0 else "fail", tail(r["out"] + r["err"], 300))

        r1 = self.vh("--version", cwd=self.work)
        r2 = self.vh("card", "list", cwd=self.work)
        ok = r1["rc"] == 0 and (self.version or "?") in r1["out"] and r2["rc"] == 0
        self.add("install.run-no-license", "라이선스 없이 커널 실행", "pass" if ok else "fail",
                 "--version=%s rc=%d · card list rc=%d %s" % (r1["out"].strip(), r1["rc"], r2["rc"], tail(r2["err"], 200)))

        if IS_WIN and not self.isolate:
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                    val, _ = winreg.QueryValueEx(k, "Path")
                hit = str(self.home / "bin").lower() in str(val).lower()
                self.add("install.path", "사용자 PATH 등록(HKCU)", "pass" if hit else "fail",
                         "bin 폴더 %s" % ("있음" if hit else "없음"))
            except Exception as e:
                self.add("install.path", "사용자 PATH 등록(HKCU)", "fail", repr(e))
        else:
            self.add("install.path", "사용자 PATH 등록(HKCU)", "skip", "격리 실행 — PATH를 안 건드림")

        if IS_WIN:
            bash = _gitbash()
            if bash:
                r = self.run([bash, "-c", "command -v vmc-harness || echo NOTFOUND; vmc-harness --version"],
                             cwd=self.work, timeout=60)
                ok = r["rc"] == 0 and (self.version or "?") in r["out"]
                self.add("install.git-bash", "Git Bash에서 `vmc-harness` 실행(Claude Bash 도구·훅 셸)",
                         "pass" if ok else "fail", tail(r["out"] + r["err"], 400),
                         {"note": "실패면 Claude Bash 도구에서 `vmc-harness declare`가 안 돈다 — bin에 확장자 없는 sh 실행기 필요"})
            else:
                self.add("install.git-bash", "Git Bash에서 `vmc-harness` 실행", "skip", "Git Bash 못 찾음")

    # ================================================================ 2 샌드박스·init
    def step2_sandbox(self):
        sb = self.sandbox
        sb.mkdir(parents=True)
        self.state["created"].append(str(sb))
        self.agents_orig = b"\xef\xbb\xbf# Sandbox agents\r\n\r\n- keep CRLF and BOM\r\n- verify only\r\n"
        (sb / "AGENTS.md").write_bytes(self.agents_orig)
        self.claude_orig = b"# Claude notes\n\nsandbox project for harness verification\n"
        (sb / "CLAUDE.md").write_bytes(self.claude_orig)
        (sb / "README.md").write_text("# sandbox\n\nsynthetic project\n", encoding="utf-8")
        (sb / "src").mkdir()
        (sb / "src" / "__init__.py").write_text("", encoding="utf-8")
        (sb / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
                                            encoding="utf-8")
        (sb / "tests").mkdir()
        (sb / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (sb / "tests" / "test_calc.py").write_text(
            "import unittest\n\nfrom src.calc import add, sub\n\n\nclass T(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n\n"
            "    def test_sub(self):\n        self.assertEqual(sub(3, 2), 1)\n", encoding="utf-8")
        (sb / "db" / "migrations").mkdir(parents=True)
        (sb / "db" / "migrations" / "0001_init.sql").write_text(
            "CREATE TABLE item (id INT PRIMARY KEY, name VARCHAR(50));\n", encoding="utf-8")
        (sb / "audit").mkdir()
        (sb / "audit" / "log_writer.py").write_text("def write(entry):\n    return entry\n", encoding="utf-8")
        for args in (["git", "init", "-q"], ["git", "config", "user.name", "verify-bot"],
                     ["git", "config", "user.email", "verify@example.invalid"], ["git", "add", "-A"],
                     ["git", "commit", "-q", "-m", "init"]):
            self.run(args, cwd=sb, timeout=60)
        self.agents_committed = (sb / "AGENTS.md").read_bytes()

        args = ["init", str(sb), "--no-llm", "--no-learning", "--no-diagnose", "--no-tests", "--no-open"]
        if self.isolate:
            args.append("--dry-system")
        r = self.vh(*args, timeout=900)
        (self.evid / "init-output.txt").write_text(self.mask(r["out"] + "\n--- stderr\n" + r["err"]), encoding="utf-8")
        self.add("init.run", "init 실행", "pass" if r["rc"] == 0 and self.project_id() else "fail",
                 "rc=%d %.0fs · %s" % (r["rc"], r["sec"], tail((r["out"] + r["err"]).strip(), 300)))

        b = (sb / "AGENTS.md").read_bytes()
        lone_lf = len(re.findall(rb"(?<!\r)\n", b))
        orig_body = self.agents_orig[3:]
        ok = b.startswith(b"\xef\xbb\xbf") and lone_lf == 0 and orig_body.rstrip(b"\r\n") in b and b != self.agents_orig
        self.add("init.agents-crlf-bom", "AGENTS.md CRLF·BOM 보존(바이트 비교)", "pass" if ok else "fail",
                 "BOM=%s · 단독 LF=%d · CRLF=%d · 원문 보존=%s · 블록 추가=%s" % (
                     b.startswith(b"\xef\xbb\xbf"), lone_lf, b.count(b"\r\n"), orig_body.rstrip(b"\r\n") in b, b != self.agents_orig),
                 {"head_hex": b[:24].hex(), "committed_equal_written": self.agents_committed == self.agents_orig})
        c = (sb / "CLAUDE.md").read_bytes()
        ok = b"@AGENTS.md" in c and c.startswith(self.claude_orig.rstrip(b"\n"))
        self.add("init.claude-import", "CLAUDE.md에 @AGENTS.md 추가(기존 내용 보존)", "pass" if ok else "fail",
                 tail(c.decode("utf-8", "replace"), 200))

        st = read_json(sb / ".claude" / "settings.json", {}) or {}
        hooks = st.get("hooks") or {}
        evs = sorted(ev for ev, lst in hooks.items() if "vmc-harness hook" in json.dumps(lst))
        need = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"}
        self.add("init.hooks", "Claude 훅 등록(.claude/settings.json)", "pass" if need <= set(evs) else "fail",
                 ", ".join(evs) or "없음")
        mcp = read_json(sb / ".mcp.json", {}) or {}
        srv = (mcp.get("mcpServers") or {}).get("vmc-brain")
        self.add("init.mcp-config", "MCP 등록(.mcp.json vmc-brain)", "pass" if srv else "fail", json.dumps(srv, ensure_ascii=False))
        ct = (sb / ".codex" / "config.toml")
        chk = (sb / ".codex" / "hooks.json")
        ok = ct.is_file() and "vmc-brain" in ct.read_text(encoding="utf-8") and chk.is_file()
        self.add("init.codex-config", "Codex 설정(.codex/config.toml MCP·hooks.json)", "pass" if ok else "fail",
                 "config.toml=%s hooks.json=%s" % (ct.is_file(), chk.is_file()))

        r = self.vh("card", "add", "--title", "검증 카드 %s" % CARD_KEYWORD,
                    "--judgment", "%s 상황에서는 검증 결과를 한 줄로 남긴다" % CARD_KEYWORD,
                    "--quote", "%s 검증은 한 줄로 남겨" % CARD_KEYWORD,
                    "--situation", "%s 검증 상황에서 무엇을 남길지 정할 때" % CARD_KEYWORD,
                    "--signal", CARD_KEYWORD,
                    "--why-not-higher", "실기 검증용 합성 카드(검증 끝나면 지움)")
        try:
            self.state["card_id"] = json.loads(r["out"].strip().splitlines()[-1]).get("card")
        except Exception:
            self.state["card_id"] = None
        self.add("init.card", "테스트 카드 추가", "pass" if self.state["card_id"] else "fail",
                 self.state["card_id"] or tail(r["out"] + r["err"], 300))

    # ================================================================ 3 백그라운드·창
    def _bg_launch(self, mode, job=None):
        d = self.work / "bg"
        d.mkdir(exist_ok=True)
        parent, child = d / "verify_parent.py", d / "verify_child.py"
        parent.write_text(PARENT_SRC, encoding="utf-8")
        child.write_text(CHILD_SRC, encoding="utf-8")
        Path(str(child) + ".body").write_text(CHILD_BODY, encoding="utf-8")
        env = self.env({"VMC_VERIFY_ROUNDS": str(FLICKER_ROUNDS), "VMC_VERIFY_GAP": str(FLICKER_GAP_S)})
        args = [sys.executable, str(parent), mode, str(d), str(self.engine), str(child)]
        p = subprocess.Popen(args, cwd=str(d), env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if job is not None:
            job.setdefault("assigned", []).append(bool(job["k32"].AssignProcessToJobObject(job["h"], int(p._handle))))
        (d / ("go-" + mode)).write_text("go", encoding="utf-8")
        return p

    @staticmethod
    def _job(breakaway_ok):
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IOC(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rt", "wt", "ot")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                        ("SchedulingClass", ctypes.c_uint32)]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.CreateJobObjectW(None, None)
        info = EXT()
        info.Basic.LimitFlags = 0x2000 | (0x800 if breakaway_ok else 0)  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
        k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info))
        return {"k32": k32, "h": h}

    def _bg_wait(self, modes, limit):
        d = self.work / "bg"
        end = time.time() + limit
        while time.time() < end and not all((d / ("child-%s.json" % m)).is_file() for m in modes):
            time.sleep(0.5)
        return {m: {"parent": read_json(d / ("parent-%s.json" % m)), "child": read_json(d / ("child-%s.json" % m))}
                for m in modes}

    def step3_background(self):
        limit = FLICKER_ROUNDS * (FLICKER_GAP_S + 1.5) + 20
        if not IS_WIN:
            p = self._bg_launch("detached")
            p.wait(30)
            res = self._bg_wait(["detached"], limit)["detached"]
            c, par = res["child"] or {}, res["parent"] or {}
            ok = c.get("done") and c.get("ok_engine") == FLICKER_ROUNDS and par and c["end"] > par["parent_exit_at"]
            self.add("bg.detached", "spawn_detached 자식이 부모 종료 뒤 끝까지(posix)", "pass" if ok else "fail",
                     "부모 종료 뒤 %.1fs 더 돌고 완료" % (c["end"] - par["parent_exit_at"]) if ok else json.dumps(res),
                     res)
            for cid, t in (("bg.job-breakaway", "job(KILL_ON_JOB_CLOSE) 닫혀도 생존"), ("flicker.human", "창 깜빡임(사람 확인)")):
                self.add(cid, t, "skip", "Windows 전용")
            return
        banner(["[사람 확인 1/2] 지금부터 약 25초 동안 화면을 보세요.",
                "작은 검은 창(콘솔)이 잠깐이라도 떴다 사라지는지 봅니다.",
                "이 창은 그대로 두고, 다른 창을 앞에 띄우지 마세요."])
        if interactive() and not self.a.no_ask:
            try:
                input("준비되면 Enter: ")
            except EOFError:
                pass
        jb = self._job(True)
        jn = self._job(False)
        p_a = self._bg_launch("detached")
        p_b = self._bg_launch("job_breakaway", jb)
        p_c = self._bg_launch("job_nobreakaway", jn)
        for p in (p_a, p_b, p_c):
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                pass
        time.sleep(0.5)
        for j in (jb, jn):
            j["k32"].CloseHandle(j["h"])   # Claude Code·Codex가 끝나며 job을 닫는 상황
        closed_at = time.time()
        res = self._bg_wait(["detached", "job_breakaway"], limit)
        res["job_nobreakaway"] = self._bg_wait(["job_nobreakaway"], 3)["job_nobreakaway"]
        res["job_assigned"] = {"breakaway": jb.get("assigned"), "nobreakaway": jn.get("assigned")}
        q1 = self.ask("방금 25초 동안 검은 창이 한 번이라도 깜빡였나요?")
        self.say("대조군(0.1.4 방식)을 띄웁니다 — 이번엔 창이 깜빡여야 정상입니다.")
        banner(["[사람 확인 2/2] 다시 약 25초 동안 화면을 보세요(0.1.4 방식 대조군)."])
        p_d = self._bg_launch("control")
        try:
            p_d.wait(30)
        except subprocess.TimeoutExpired:
            pass
        res["control"] = self._bg_wait(["control"], limit)["control"]
        q2 = self.ask("이번 25초 동안은 검은 창이 깜빡였나요?")
        self.answers.update(flicker_new=q1, flicker_control=q2)
        (self.evid / "background.json").write_text(json.dumps(res, indent=2), encoding="utf-8")

        def survived(m, after):
            c, par = res[m]["child"] or {}, res[m]["parent"] or {}
            return bool(c.get("done") and par and c.get("end", 0) > after)
        c = res["detached"]["child"] or {}
        self.add("bg.detached", "spawn_detached 자식이 부모 종료 뒤 끝까지", "pass" if survived("detached", (res["detached"]["parent"] or {}).get("parent_exit_at", 0)) and c.get("ok_engine") == FLICKER_ROUNDS else "fail",
                 "child=%s" % json.dumps(c), res["detached"])
        cb = res["job_breakaway"]["child"] or {}
        assigned = all(jb.get("assigned") or [False])
        self.add("bg.job-breakaway", "job(KILL_ON_JOB_CLOSE·BREAKAWAY_OK) 닫혀도 자식 생존",
                 "pass" if assigned and survived("job_breakaway", closed_at) else "fail",
                 "부모 job 배정=%s · child in_job=%s done=%s" % (assigned, cb.get("in_job"), cb.get("done")), res["job_breakaway"])
        cn = res["job_nobreakaway"]["child"] or {}
        self.add("bg.job-nobreakaway", "(참고) 이탈 불허 job이 닫히면 자식은 죽는다 — 알려진 한계",
                 "pass", "생존=%s in_job=%s" % (bool(cn.get("done")), cn.get("in_job")), res["job_nobreakaway"])
        if q1 is None:
            r, ev = "human", "질문에 답하지 않음(비대화형) — 다시 실행해 확인"
        elif q1 == "y":
            r, ev = "fail", "새 방식에서도 창이 깜빡임(대조군 %s)" % q2
        elif q2 == "y":
            r, ev = "pass", "새 방식 안 깜빡임 · 대조군(0.1.4) 깜빡임 — 관찰이 유효"
        else:
            r, ev = "human", "둘 다 안 보임 — 관찰이 대조군도 못 잡아 판정 불가(터미널 설정 등)"
        self.add("flicker.human", "백그라운드 실행 창 깜빡임(사람 확인 + 대조군)", r, ev, {"new": q1, "control": q2})

    # ================================================================ 4 훅 비용
    def step4_hooks(self):
        sid = "verify-bench-" + uuid.uuid4().hex[:8]
        base = {"session_id": sid, "transcript_path": "", "cwd": str(self.sandbox)}
        payloads = {
            "SessionStart": dict(base, hook_event_name="SessionStart", source="startup"),
            "PreToolUse": dict(base, hook_event_name="PreToolUse", tool_name="Bash",
                               tool_input={"command": "git status"}, tool_use_id="toolu_bench"),
            "PostToolUse": dict(base, hook_event_name="PostToolUse", tool_name="Bash",
                                tool_input={"command": "git status"}, tool_use_id="toolu_bench",
                                tool_response={"stdout": "", "stderr": "", "interrupted": False}),
            "Stop": dict(base, hook_event_name="Stop", stop_hook_active=False),
        }
        limit_s = self._config_int("hook_timeout_s", 8)
        base_py = self.run([sys.executable, "-c", "pass"], timeout=30)["sec"]
        stats, bad = {}, []
        for ev, pl in payloads.items():
            xs = []
            for _ in range(BENCH_RUNS):
                r = self.run([self.launcher, "hook", ev, "--agent", "claude"], cwd=self.sandbox, timeout=60,
                             input_text=json.dumps(pl), log=False)
                xs.append(r["sec"])
                if r["rc"] != 0:
                    bad.append("%s rc=%d %s" % (ev, r["rc"], tail(r["err"], 200)))
            xs.sort()
            stats[ev] = {"median_ms": round(statistics.median(xs) * 1000), "p90_ms": round(xs[int(0.9 * (len(xs) - 1))] * 1000),
                         "max_ms": round(xs[-1] * 1000)}
        (self.evid / "hook-bench.json").write_text(json.dumps({"stats": stats, "python_startup_s": base_py, "errors": bad},
                                                              indent=2), encoding="utf-8")
        worst = max(v["p90_ms"] for v in stats.values())
        ok = not bad and worst < limit_s * 1000
        self.add("hook.cost", "훅 기동 비용(%d회씩, 실행기(.cmd·sh) 기동 포함)" % BENCH_RUNS, "pass" if ok else "fail",
                 " · ".join("%s 중앙 %dms p90 %dms" % (k, v["median_ms"], v["p90_ms"]) for k, v in stats.items())
                 + " (python 기동 %dms, 상한 %ds)" % (base_py * 1000, limit_s), {"stats": stats, "errors": bad[:5]})

    def _config_int(self, key, default):
        try:
            text = (self.engine / "vmc_harness" / "config" / "defaults.toml").read_text(encoding="utf-8")
            m = re.search(r"^%s\s*=\s*(\d+)" % re.escape(key), text, re.M)
            return int(m.group(1)) if m else default
        except OSError:
            return default

    # ================================================================ 5 Claude Code
    def _add_probes(self):
        pd = self.work / "probe"
        pd.mkdir(exist_ok=True)
        (pd / "probe.py").write_text(PROBE_SRC, encoding="utf-8")
        out = pd / "out"
        mp = self.sandbox / ".mcp.json"
        data = read_json(mp, {}) or {}
        data.setdefault("mcpServers", {})["vmc-verify-env"] = {
            "type": "stdio", "command": sys.executable, "args": [str(pd / "probe.py"), str(out), "claude"]}
        mp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        ct = self.sandbox / ".codex" / "config.toml"
        if ct.is_file():
            q = lambda s: "'" + s + "'"
            block = "\n[mcp_servers.vmc-verify-env]\ncommand = %s\nargs = [%s, %s, %s]\n" % (
                q(sys.executable), q(str(pd / "probe.py")), q(str(out)), q("codex"))
            with open(str(ct), "a", encoding="utf-8", newline="\n") as f:
                f.write(block)
        return out

    def _probe_env(self, client):
        out = self.work / "probe" / "out"
        recs = [read_json(f) for f in sorted(out.glob("probe-%s-*.json" % client))] if out.is_dir() else []
        return [r for r in recs if r]

    def _claude_mcp_list(self, claude):
        r = self.run([claude, "mcp", "list"], cwd=self.sandbox, timeout=180)
        lines = [ln for ln in (r["out"] + r["err"]).splitlines() if "vmc-" in ln]
        st = {}
        for ln in lines:
            name = "vmc-brain" if "vmc-brain" in ln else ("vmc-verify-env" if "vmc-verify-env" in ln else None)
            if name:
                low = ln.lower()
                st[name] = ("pending" if "pending" in low else
                            "fail" if ("✗" in ln or "✘" in ln or "failed" in low) else
                            "ok" if ("✓" in ln or "✔" in ln or "connected" in low) else "unknown")
        return st, r

    def _claude(self, claude, name, prompt, sid, timeout, watch=None, model=None):
        cdir = self.evid / "claude"
        cdir.mkdir(exist_ok=True)
        args = [claude, "-p", "--session-id", sid, "--output-format", "stream-json", "--verbose",
                "--dangerously-skip-permissions"]
        if model or self.a.claude_model:
            args += ["--model", model or self.a.claude_model]
        kw = {"creationflags": CREATE_NEW_PROCESS_GROUP} if IS_WIN else {"start_new_session": True}
        raw = cdir / ("%s.raw.jsonl" % name)
        t0 = time.time()
        with open(str(raw), "wb") as fo:
            p = subprocess.Popen(args, cwd=str(self.sandbox), env=self.env(), stdin=subprocess.PIPE,
                                 stdout=fo, stderr=subprocess.STDOUT, **kw)
            p.stdin.write(prompt.encode("utf-8"))
            p.stdin.close()
            killed = None
            while p.poll() is None:
                if watch and watch(time.time() - t0):
                    killed = "watch"
                elif time.time() - t0 > timeout:
                    killed = "timeout"
                if killed:
                    self._kill_tree(p)
                    break
                time.sleep(2)
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                pass
        text = raw.read_text(encoding="utf-8", errors="replace")
        raw.unlink()
        (cdir / ("%s.jsonl" % name)).write_text(self.mask(text), encoding="utf-8")
        result, mcp = None, {}
        for ln in text.splitlines():
            try:
                o = json.loads(ln)
            except ValueError:
                continue
            if o.get("type") == "result":
                result = o
            elif o.get("type") == "system" and o.get("subtype") == "init":
                mcp = {m.get("name"): m.get("status") for m in o.get("mcp_servers") or [] if isinstance(m, dict)}
        tools = re.findall(r'"name":\s*"([A-Za-z_][\w.-]*)"', text)
        info = {"rc": p.returncode, "sec": round(time.time() - t0, 1), "killed": killed,
                "result": tail((result or {}).get("result") or "", 400), "is_error": (result or {}).get("is_error"),
                "tools": sorted(set(t for t in tools if t[:1].isupper() or t.startswith("mcp__"))),
                "mcp": {k: v for k, v in mcp.items() if k and k.startswith("vmc-")}}
        self.state["sessions"][name] = dict(info, session_id=sid)
        self.say("  claude %s: rc=%s %.0fs killed=%s tools=%s" % (name, p.returncode, info["sec"], killed, ",".join(info["tools"])))
        return info

    def _kill_tree(self, p):
        if IS_WIN:
            self.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], timeout=30)
        else:
            import signal
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                p.kill()

    def _tasks_of(self, sid):
        return [t for t in self.local_tasks() if t.get("session_id") == sid]

    def _mcp_verdict(self, claude, list_status):
        """세션 init 이벤트의 MCP 서버 상태로 판정한다(claude mcp list는 미승인 프로젝트 서버를 검사하지 않는다)."""
        ms = self.state["sessions"]["session1"].get("mcp") or {}
        brain = ms.get("vmc-brain", "missing")
        detail = {"session_init": {k: v for k, v in ms.items() if k.startswith("vmc-")}, "mcp_list": list_status,
                  "command": ((read_json(self.sandbox / ".mcp.json", {}) or {}).get("mcpServers") or {}).get("vmc-brain")}
        if brain != "connected" and IS_WIN:
            # 증거만: `cmd /c vmc-harness mcp`로 바꾸면 뜨는가(짧은 세션 1회, 끝나면 원래대로 되돌린다 — 수정은 하지 않음)
            mp = self.sandbox / ".mcp.json"
            orig = mp.read_bytes()
            data = json.loads(orig.decode("utf-8"))
            data["mcpServers"]["vmc-brain"] = {"type": "stdio", "command": "cmd", "args": ["/c", "vmc-harness", "mcp"]}
            mp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            try:
                info = self._claude(claude, "mcp-cmd-c", "'확인' 한 단어만 답하라. 도구는 쓰지 마라.", str(uuid.uuid4()), 180,
                                    model="haiku")
                detail["cmd_c_variant"] = (info.get("mcp") or {}).get("vmc-brain", "missing")
            finally:
                mp.write_bytes(orig)
        self.add("mcp.list", "브레인 MCP 연결(Claude 세션 init — 등록 명령 `vmc-harness`, 확장자 없음)",
                 "pass" if brain == "connected" else "fail",
                 "vmc-brain=%s · 대조 probe(절대 경로 python)=%s%s" % (
                     brain, ms.get("vmc-verify-env", "missing"),
                     (" · `cmd /c vmc-harness mcp`로 바꾸면=%s" % detail["cmd_c_variant"]) if "cmd_c_variant" in detail else ""),
                 detail)

    def step5_claude(self):
        claude = shutil.which("claude")
        ids = ["claude.hooks-fired", "claude.declare", "claude.ledger-turns", "claude.session-end", "claude.measured",
               "claude.idle-close", "claude.judge-state", "claude.health", "mcp.list", "mcp.search",
               "mcp.project-dir-claude"]
        if not claude or self.a.skip_claude:
            for i in ids:
                self.add(i, i, "skip", "claude 없음" if not claude else "--skip-claude 또는 로그인 안 됨")
            return
        self._add_probes()
        st, r = self._claude_mcp_list(claude)   # 프로젝트 서버는 대화형 승인 전이면 '승인 대기'로만 나온다 — 참고 증거
        (self.evid / "claude-mcp-list.txt").write_text(self.mask(r["out"] + "\n" + r["err"]), encoding="utf-8")

        smoke = self.a.claude_smoke
        s1 = str(uuid.uuid4())
        if smoke:
            p1 = ("vmc-brain MCP 서버의 search_cards 도구를 situation='%s 검증 상황'으로 한 번만 호출하고, "
                  "찾은 카드 제목만 한 줄로 답하라. 다른 도구는 쓰지 마라." % CARD_KEYWORD)
        else:
            p1 = ("이 저장소에는 VMC 하네스가 설치되어 있다. 질문하지 말고 다음을 순서대로 끝까지 하라.\n"
                  "1) vmc-brain MCP의 search_cards 도구를 situation='%s 검증 상황에서 무엇을 남길지'로 한 번 호출한다.\n"
                  "2) AGENTS.md의 하네스 규칙대로 착수 선언을 한다(범위: src/calc.py).\n"
                  "3) src/calc.py의 add 함수 바로 위에 주석 한 줄 `# verified`를 추가한다.\n"
                  "4) `python -m unittest discover -s tests`로 테스트를 실행한다.\n"
                  "5) 결과를 한 줄로 답한다." % CARD_KEYWORD)
        self._claude(claude, "session1", p1, s1, self.a.claude_timeout)
        time.sleep(3)
        self._mcp_verdict(claude, st)
        srec = self.session_rec(s1)
        self.add("claude.hooks-fired", "Claude 훅이 실제로 돎(세션 기록 생성)", "pass" if srec else "fail",
                 "sessions/%s.json %s" % (s1[:8], "있음" if srec else "없음 — 훅 명령이 실행되지 않음"), srec)
        searches = self.events("card_search")
        self.add("mcp.search", "세션에서 search_cards 호출 → 노출 기록(card_search 이벤트)", "pass" if searches else "fail",
                 "card_search %d건 · 세션 도구 %s" % (len(searches), ",".join(self.state["sessions"]["session1"]["tools"])))
        probes = self._probe_env("claude")
        got = [p for p in probes if (p.get("env") or {}).get("CLAUDE_PROJECT_DIR")]
        self.add("mcp.project-dir-claude", "MCP 서버에 CLAUDE_PROJECT_DIR 전달(Claude Code)",
                 "pass" if got else ("fail" if probes else "fail"),
                 ("받음: %s" % got[0]["env"]["CLAUDE_PROJECT_DIR"]) if got else (
                     "probe %d번 기동, 값 없음 (cwd=%s)" % (len(probes), probes[0].get("cwd") if probes else "-")),
                 {"probes": probes[-3:]})
        if smoke:
            for i in ("claude.declare", "claude.ledger-turns", "claude.session-end", "claude.measured", "claude.idle-close"):
                self.add(i, i, "skip", "--claude-smoke(세션 1회만)")
            self._judge_health()
            return

        t1 = self._tasks_of(s1)
        led = self.ledger_tasks()
        rec1 = led.get(t1[-1]["task_id"]) if t1 else None
        self.add("claude.declare", "착수 선언 → 작업 장부 기록(Bash 도구의 `vmc-harness declare`)",
                 "pass" if t1 and rec1 else "fail",
                 ("task %s · 장부 %s" % (t1[-1]["task_id"][:8], rec1.get("_path") if rec1 else "없음")) if t1 else
                 "이 세션 작업 없음 — 선언 실패(도구 기록: %s)" % ",".join(self.state["sessions"]["session1"]["tools"]))

        # 세션 2 — 긴 작업 도중 강제 종료
        s2 = str(uuid.uuid4())
        seen_written = []
        first_seen = {}

        def watch(el):
            ts = self._tasks_of(s2)
            if not ts:
                return False
            first_seen.setdefault("t", el)
            r = self.ledger_tasks().get(ts[-1]["task_id"])
            if r and r.get("written_at") and r["written_at"] not in seen_written:
                seen_written.append(r["written_at"])
            return bool(ts[-1].get("edits")) and len(seen_written) >= 2 and el - first_seen["t"] > 20 or el > 300
        p2 = ("질문하지 말고 진행하라. AGENTS.md 규칙대로 착수 선언(범위: src/)을 한 뒤, src/gen_01.py부터 src/gen_40.py까지 "
              "파일을 하나씩 만든다. 각 파일에는 서로 다른 함수 하나를 넣고, 파일 하나를 만들 때마다 "
              "`python -m unittest discover -s tests`를 실행한다. 여러 파일을 한 번에 만들지 말고 하나씩 진행하라.")
        info2 = self._claude(claude, "session2", p2, s2, 420, watch=watch)
        time.sleep(5)
        t2 = self._tasks_of(s2)
        rec2 = self.ledger_tasks().get(t2[-1]["task_id"]) if t2 else None
        if rec2 and rec2.get("written_at") and rec2["written_at"] not in seen_written:
            seen_written.append(rec2["written_at"])
        self.add("claude.ledger-turns", "장부가 착수 때 쓰이고 매 턴 갱신(written_at)",
                 "pass" if rec2 and len(seen_written) >= 2 else "fail",
                 "세션2 장부 갱신 %d번 관찰 · status=%s · 강제 종료=%s" % (len(seen_written), (rec2 or {}).get("status"), info2["killed"]),
                 {"written_at": seen_written})
        # finalize_idle_min은 커널 설정이라 프로젝트에서 줄일 수 없다 → 로컬 작업 상태의 활동 시각을 그만큼 앞으로 당겨 시간 경과를 모사
        idle = self._config_int("finalize_idle_min", 120)
        shifted = 0
        if t2:
            t = read_json(t2[-1]["_path"])
            delta = _dt.timedelta(minutes=idle + 5)
            for k in ("last_activity_at", "last_edit_at", "redeclared_at", "declared_at", "started_at", "first_edit_at"):
                v = t.get(k)
                if v:
                    try:
                        t[k] = (_dt.datetime.fromisoformat(v.replace("Z", "+00:00")) - delta).isoformat(timespec="seconds")
                        shifted += 1
                    except ValueError:
                        pass
            Path(t2[-1]["_path"]).write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")

        s3 = str(uuid.uuid4())
        self._claude(claude, "session3", "한 줄로 '확인'이라고만 답하라. 도구는 쓰지 마라.", s3, 300)
        time.sleep(5)
        rec2b = self.ledger_tasks().get(t2[-1]["task_id"]) if t2 else None
        ok = rec2b and rec2b.get("status") == "ended" and rec2b.get("end_reason") == "idle"
        self.add("claude.idle-close", "SessionEnd 없이 죽은 작업이 새 세션 시작 때 마감(idle)",
                 "pass" if ok else "fail",
                 "status=%s end_reason=%s (활동 시각 %d분 앞당김 %d칸)" % ((rec2b or {}).get("status"), (rec2b or {}).get("end_reason"),
                                                                     idle + 5, shifted), {k: (rec2b or {}).get(k) for k in ("status", "end_reason", "ended_at", "last_activity_at")})

        rec1 = self.ledger_tasks().get(t1[-1]["task_id"]) if t1 else None
        self.add("claude.session-end", "정상 종료 세션의 작업 마감", "pass" if rec1 and rec1.get("status") == "ended" else "fail",
                 "status=%s end_reason=%s" % ((rec1 or {}).get("status"), (rec1 or {}).get("end_reason")))
        end = time.time() + 150
        while time.time() < end:
            led = self.ledger_tasks()
            r1 = led.get(t1[-1]["task_id"]) if t1 else None
            r2 = led.get(t2[-1]["task_id"]) if t2 else None
            if (r1 or {}).get("measured_at") and (r2 or {}).get("measured_at"):
                break
            time.sleep(5)
        self.add("claude.measured", "백그라운드 실측 완료(measured_at)", "pass" if (r1 or {}).get("measured_at") else "fail",
                 "세션1=%s 세션2=%s" % ((r1 or {}).get("measured_at"), (r2 or {}).get("measured_at")),
                 {"session1_actual": (r1 or {}).get("actual"), "session2_actual": (r2 or {}).get("actual")})
        self._judge_health()

    def _judge_health(self):
        d = self.local_dir()
        end = time.time() + 90
        js = {}
        while time.time() < end:
            js = read_json(d / "judge-state.json", {}) or {} if d else {}
            if js.get("started_at") and js.get("last_run"):
                break
            time.sleep(3)
        self.add("claude.judge-state", "세션 시작 판정 분리 실행(spawned_at·started_at·last_run)",
                 "pass" if js.get("spawned_at") and js.get("started_at") else "fail",
                 "spawned=%s started=%s last_run=%s" % (js.get("spawned_at"), js.get("started_at"), js.get("last_run")),
                 {k: v for k, v in js.items() if k != "summary"})
        h = read_json(d / "health.json", {}) if d else {}
        self.add("claude.health", "건강 점검 상태(health.json)", "pass" if h and h.get("checked_at") and not h.get("stalled") else "fail",
                 "checked_at=%s stalled=%s" % ((h or {}).get("checked_at"), (h or {}).get("stalled")), h)

    # ================================================================ 6 Codex
    def step6_codex(self):
        codex = shutil.which("codex")
        ids = ["codex.trust", "codex.exec-hooks", "codex.exec-ledger", "codex.mcp", "mcp.project-dir-codex", "codex.tui"]
        if not codex or self.a.skip_codex:
            for i in ids:
                self.add(i, i, "skip", "codex 없음" if not codex else "--skip-codex")
            return
        if not (self.work / "probe" / "probe.py").is_file():
            self._add_probes()
        cfg = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "config.toml"
        text = cfg.read_text(encoding="utf-8", errors="replace") if cfg.is_file() else ""
        real = os.path.realpath(str(self.sandbox))
        lines = [ln for ln in text.splitlines() if real.replace("\\", "\\\\") in ln or real in ln]
        hook_trust = len(re.findall(r"vmc-harness hook|trusted_hash", text))
        if self.isolate:
            self.add("codex.trust", "Codex 신뢰 등록(~/.codex/config.toml)", "skip", "격리 실행(--dry-system) — 신뢰 등록 안 함")
        else:
            self.add("codex.trust", "Codex 신뢰 등록(~/.codex/config.toml)", "pass" if lines else "fail",
                     "샌드박스 줄 %d개 · trusted_hash %d개" % (len(lines), hook_trust), {"lines": lines[:6]})
        r = self.run([codex, "mcp", "list"], cwd=self.sandbox, timeout=120)
        (self.evid / "codex-mcp-list.txt").write_text(self.mask(r["out"] + "\n" + r["err"]), encoding="utf-8")
        has = "vmc-brain" in r["out"]
        before = {f.name for f in (self.local_dir() / "sessions").glob("*.json")} if (self.local_dir() / "sessions").is_dir() else set()
        prompt = ("질문하지 말고 끝까지 하라. 1) vmc-brain MCP의 search_cards를 situation='%s 검증 상황'으로 한 번 호출한다. "
                  "2) AGENTS.md 하네스 규칙대로 착수 선언(범위: src/calc.py)을 한다. 3) src/calc.py 맨 끝에 주석 `# codex verified` "
                  "한 줄을 추가한다. 4) 결과를 한 줄로 답한다." % CARD_KEYWORD)
        args = [codex, "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "-C", str(self.sandbox)]
        if self.isolate:
            args.append("--dangerously-bypass-hook-trust")
        args.append("-")
        n_search = len(self.events("card_search"))
        r = self.run(args, cwd=self.sandbox, timeout=self.a.claude_timeout, input_text=prompt)
        (self.evid / "codex-exec.jsonl").write_text(self.mask(r["out"] + "\n--- stderr\n" + tail(r["err"], 4000)), encoding="utf-8")
        failed = re.findall(r'"type":"turn.failed","error":\{"message":"([^"]+)"', r["out"])
        auth = bool(failed) and re.search(r"(?i)log ?in|sign in|token|401|auth", failed[0] or "")
        tid = None
        m = re.search(r'"thread_id"\s*:\s*"([^"]+)"', r["out"])
        if m:
            tid = m.group(1)
        time.sleep(3)
        after = {f.name for f in (self.local_dir() / "sessions").glob("*.json")} if (self.local_dir() / "sessions").is_dir() else set()
        new = [read_json(self.local_dir() / "sessions" / n) for n in sorted(after - before)]
        cx = [s for s in new if s and s.get("agent") == "codex"]
        self.add("codex.exec-hooks", "codex exec에서 하네스 훅이 돎", "pass" if cx else "fail",
                 "rc=%d %.0fs · 새 codex 세션 기록 %d개 (thread %s)" % (r["rc"], r["sec"], len(cx), (tid or "-")[:8]))
        if auth:
            msg = "Codex 로그인 만료로 모델 호출 실패(하네스 문제 아님) — `codex login` 뒤 다시: %s" % failed[0][:120]
            for i, t in (("codex.exec-ledger", "codex 착수 선언 → 작업 장부"), ("codex.mcp", "Codex MCP(vmc-brain) 등록·호출")):
                self.add(i, t, "skip", msg)
            self._codex_probe_and_tui(codex)
            return
        sids = {s.get("session_id") for s in cx} | ({tid} if tid else set())
        ct = [t for t in self.local_tasks() if t.get("session_id") in sids]
        led = self.ledger_tasks()
        self.add("codex.exec-ledger", "codex 착수 선언 → 작업 장부", "pass" if ct and ct[-1]["task_id"] in led else "fail",
                 ("task %s · agent=%s" % (ct[-1]["task_id"][:8], led.get(ct[-1]["task_id"], {}).get("agent"))) if ct else "작업 없음")
        n2 = len(self.events("card_search"))
        self.add("codex.mcp", "Codex MCP(vmc-brain) 등록·호출", "pass" if has and n2 > n_search else "fail",
                 "codex mcp list에 vmc-brain %s · exec 중 card_search %d건" % ("있음" if has else "없음", n2 - n_search))
        if failed:
            self.add("codex.exec-error", "(참고) codex exec 오류", "fail", failed[0][:300])
        self._codex_probe_and_tui(codex)

    def _codex_probe_and_tui(self, codex):
        probes = self._probe_env("codex")
        self.add("mcp.project-dir-codex", "(참고) Codex MCP 서버 환경(프로젝트 위치 단서)", "pass" if probes else "fail",
                 ("cwd=%s env=%s" % (probes[-1].get("cwd"), ",".join(sorted(probes[-1].get("env") or {})))) if probes else "probe 기동 안 됨",
                 {"probes": probes[-2:]})
        self._codex_tui(codex)

    def _codex_tui(self, codex):
        if not IS_WIN or self.a.no_ask or not interactive():
            self.add("codex.tui", "Codex 대화형 TUI 알림 표시(사람 확인)", "skip", "Windows 대화형 실행에서만")
            return
        banner(["[선택] Codex 대화형 화면 확인 — 약 2분",
                "1) 새 PowerShell 창을 열고:  cd \"%s\"" % self.sandbox,
                "2) codex  실행(신뢰 질문이 나오면 허용)",
                "3) 이 문장을 그대로 입력:",
                "   착수 선언을 범위 src/calc.py로 하고, README.md 맨 끝에 'tui check' 한 줄을 추가해줘",
                "4) 작업이 끝나면 화면 아래쪽에 '범위 밖 변경 1건' 같은 하네스 알림 줄이 보이는지 확인",
                "5) codex 창은 닫고 이 창으로 돌아와 답하세요 (건너뛰려면 s)"])
        before = {t["task_id"] for t in self.local_tasks()}
        q = self.ask("하네스 알림 줄이 보였나요? (y=보임 / n=안 보임 / s=건너뜀)", "yns")
        self.answers["codex_tui"] = q
        new = [t for t in self.local_tasks() if t["task_id"] not in before and t.get("agent") == "codex"]
        oos = [o for t in new for o in ((t.get("scope_cache") or {}).get("out_of_scope") or [])]
        ev = "새 codex 작업 %d개 · 범위 밖 기록 %d건" % (len(new), len(oos))
        if q in (None, "s"):
            self.add("codex.tui", "Codex 대화형 TUI 알림 표시(사람 확인)", "skip", "건너뜀 · " + ev)
        else:
            self.add("codex.tui", "Codex 대화형 TUI 알림 표시(사람 확인)", "pass" if q == "y" else "fail",
                     ("보였음" if q == "y" else "안 보였음") + " · " + ev)

    # ================================================================ 7 스케줄러
    def step7_scheduler(self):
        d = self.local_dir()
        cfgp = self.sandbox / ".harness" / "config.json"
        cfg = read_json(cfgp, {}) or {}
        cfg["profile"] = "gxp"   # loop.enabled=false — tick이 정리(upkeep)만 하고 밤 루프(claude -p)는 돌리지 않게
        cfgp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        if IS_WIN:
            r = self.vh("loop", "register", "--root", str(self.sandbox), timeout=120)
            st = read_json(d / "scheduler.json", {}) or {}
            self.add("sched.register", "스케줄러 등록(variant)", "pass" if st.get("ok") and st.get("variant") == "xml" else "fail",
                     "method=%s variant=%s reason=%s · %s" % (st.get("method"), st.get("variant"), st.get("reason"), tail(r["out"], 200)), st)
            name = "VMC Harness Loop %s" % (self.project_id() or "")[:8]
            r = self.run(["schtasks", "/Query", "/TN", name, "/XML"], timeout=60)
            x = r["out"]
            (self.evid / "schtask.xml").write_text(self.mask(x), encoding="utf-8")
            want = {"WakeToRun": "true", "DisallowStartIfOnBatteries": "false", "StopIfGoingOnBatteries": "false"}
            got = {k: (re.search(r"<%s>(\w+)</%s>" % (k, k), x) or [None, None])[1] for k in want}
            self.add("sched.xml", "등록된 작업 XML(배터리 무관·WakeToRun)", "pass" if got == want else "fail",
                     " ".join("%s=%s" % kv for kv in got.items()))
            h0 = (read_json(d / "health.json", {}) or {}).get("checked_at")
            time.sleep(1.5)
            t_run = time.time()
            r = self.run(["schtasks", "/Run", "/TN", name], timeout=60)
            h1 = None
            end = time.time() + 90
            while time.time() < end:
                h1 = (read_json(d / "health.json", {}) or {}).get("checked_at")
                if h1 and h1 != h0:
                    break
                time.sleep(3)
            q = self.run(["schtasks", "/Query", "/TN", name, "/V", "/FO", "LIST"], timeout=60)
            last = re.findall(r"^(?:Last Result|마지막 결과)\s*:\s*(.+)$", q["out"], re.M)
            self.add("sched.run", "작업 스케줄러가 tick 실행(health.json 갱신)", "pass" if h1 and h1 != h0 else "fail",
                     "/Run rc=%d · checked_at %s → %s · 마지막 결과 %s (%.0fs)" % (r["rc"], h0, h1, last[:1], time.time() - t_run))
        else:
            for i, t in (("sched.register", "스케줄러 등록"), ("sched.xml", "작업 XML"), ("sched.run", "작업 실행")):
                self.add(i, t, "skip", "Windows 전용(맥 launchd는 실사용자 설정이라 시험에서 안 건드림)")
        r = self.vh("loop", "tick", "--root", str(self.sandbox), timeout=300)
        h = read_json(d / "health.json", {}) or {}
        self.add("sched.tick", "`loop tick` 직접 실행(정리만, 루프는 끔)", "pass" if r["rc"] == 0 else "fail",
                 "rc=%d · %s · health.checked_at=%s" % (r["rc"], tail(r["out"].strip(), 200), h.get("checked_at")))
        code = ("import json\nfrom vmc_harness.loop import power\nh = power.inhibit()\ns = power.summary(h)\n"
                "power.release(h)\nprint(json.dumps({'summary': s, 'sleep_seconds': power.sleep_seconds()}, default=str))\n")
        r = self.py_engine(code)
        try:
            pw = json.loads(r["out"].strip().splitlines()[-1])
        except Exception:
            pw = None
        if IS_WIN:
            ok = r["rc"] == 0 and pw is not None and bool((pw.get("summary") or {}).get("ok"))
            self.add("sched.power", "루프 중 절전 막기(SetThreadExecutionState)·절전 설정", "pass" if ok else "fail",
                     tail(r["out"].strip() + r["err"], 300), pw)
            self.vh("loop", "unregister", "--root", str(self.sandbox), timeout=120)
            q = self.run(["schtasks", "/Query", "/TN", "VMC Harness Loop %s" % (self.project_id() or "")[:8]], timeout=60)
            self.add("sched.unregister", "스케줄러 해제", "pass" if q["rc"] != 0 else "fail", "해제 뒤 Query rc=%d" % q["rc"])
        else:
            self.add("sched.power", "절전 막기", "skip" if r["rc"] == 0 else "fail", "맥 caffeinate — 참고만: " + tail(r["out"].strip() + r["err"], 200))

    # ================================================================ 수집·정리
    def collect(self):
        def copy_tree(src, dst):
            if not src or not Path(src).is_dir():
                return
            for f in Path(src).rglob("*"):
                if not f.is_file() or "cache" in f.relative_to(src).parts or f.stat().st_size > 2_000_000:
                    continue
                t = Path(dst) / f.relative_to(src)
                t.parent.mkdir(parents=True, exist_ok=True)
                if f.suffix.lower() in (".json", ".jsonl", ".md", ".txt", ".toml", ".xml", ".log", ".py", ".pyw", ".cmd", ".sh", ""):
                    t.write_text(self.mask(f.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
        copy_tree(self.sandbox / ".harness", self.evid / "sandbox-harness")
        copy_tree(self.local_dir(), self.evid / "local")
        sf = self.evid / "sandbox-files"
        sf.mkdir(exist_ok=True)
        for rel in ("CLAUDE.md", ".claude/settings.json", ".mcp.json", ".codex/config.toml", ".codex/hooks.json"):
            p = self.sandbox / rel
            if p.is_file():
                (sf / rel.replace("/", "__").lstrip(".")).write_text(self.mask(p.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
        p = self.sandbox / "AGENTS.md"
        if p.is_file():
            b = p.read_bytes()
            (sf / "AGENTS.md.hex.txt").write_text(b[:600].hex(" ", 16) if sys.version_info >= (3, 8) else b[:600].hex(),
                                                 encoding="utf-8")
        probe = self.work / "probe" / "out"
        if probe.is_dir():
            copy_tree(probe, self.evid / "probe")

    def cleanup(self):
        if self.a.keep:
            self.add("cleanup", "이번 실행이 만든 것 정리", "skip", "--keep: 샌드박스·스케줄러·카드를 남김(%s)" % self.work)
            return
        notes = []
        if self.launcher and self.sandbox.is_dir() and self.project_id():
            if self.state.get("card_id"):
                r = self.vh("card", "delete", self.state["card_id"], timeout=60)
                notes.append("카드 삭제 rc=%d" % r["rc"])
            r = self.vh("remove", "--purge-harness", timeout=300)
            notes.append("remove rc=%d" % r["rc"])
            if IS_WIN:
                q = self.run(["schtasks", "/Query", "/TN", "VMC Harness Loop %s" % (self.project_id() or "x")[:8]], timeout=60)
                notes.append("스케줄러 남음=%s" % (q["rc"] == 0))
        if not self.a.keep:
            rmtree(self.sandbox)
            for sub in ("repo", "build", "bg", "probe"):
                rmtree(self.work / sub)
            if self.isolate:
                rmtree(self.home)
        notes.append("샌드박스 삭제=%s" % (not self.sandbox.exists()))
        self.add("cleanup", "이번 실행이 만든 것 정리", "pass", " · ".join(notes))

    def finish(self):
        total = round(time.time() - self.t0)
        meta = {"kit": "win_verify", "at": now_iso(), "elapsed_s": total, "version": self.version,
                "repo_commit": self.env_info.get("repo_commit"), "platform": platform.platform(),
                "isolate": self.isolate, "answers": self.answers,
                "claude_sessions": self.state["sessions"], "env": self.env_info}
        data = {"meta": self.mask.obj(meta), "results": self.results}
        (self.out / "results.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        md = render_summary(data)
        (self.out / "summary.md").write_text(md, encoding="utf-8")
        self.log_f.close()
        self.cmdlog.close()
        dest = Path(self.a.out).resolve() if self.a.out else desktop_dir()
        dest.mkdir(parents=True, exist_ok=True)
        host = hashlib.sha256(platform.node().encode("utf-8")).hexdigest()[:6]
        zp = dest / ("vmc-win-verify-%s-%s-%s.zip" % ("win" if IS_WIN else sys.platform, host, self.stamp))
        with zipfile.ZipFile(str(zp), "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(self.out.rglob("*")):
                if f.is_file():
                    zf.write(str(f), str(f.relative_to(self.out)))
        print("\n" + md)
        print("결과 zip: %s" % zp)
        uploaded = None if self.a.no_upload else upload(zp)
        if uploaded:
            print("업로드 완료: %s (릴리스 %s)" % (ARCHIVE_REPO, RELEASE_TAG))
        else:
            print("자동 업로드 %s — 이 zip을 디스코드에 올려 주세요: %s" % ("끔" if self.a.no_upload else "실패", zp))
        if not self.a.keep:
            rmtree(self.work)
        return zp


def upload(zp):
    gh = shutil.which("gh")
    if not gh:
        return False
    if subprocess.run([gh, "auth", "status"], capture_output=True).returncode != 0:
        return False
    if subprocess.run([gh, "release", "view", RELEASE_TAG, "-R", ARCHIVE_REPO], capture_output=True).returncode != 0:
        r = subprocess.run([gh, "release", "create", RELEASE_TAG, "-R", ARCHIVE_REPO, "--prerelease", "--title",
                            "Windows 실기 검증 결과", "--notes", "scripts/win_verify.py가 올린 결과 zip"], capture_output=True)
        if r.returncode != 0:
            print(decode(r.stderr))
            return False
    r = subprocess.run([gh, "release", "upload", RELEASE_TAG, str(zp), "-R", ARCHIVE_REPO, "--clobber"], capture_output=True)
    if r.returncode != 0:
        print(decode(r.stderr))
    return r.returncode == 0


# ---------------------------------------------------------------- 요약

MARK = {"pass": "통과", "fail": "실패", "skip": "건너뜀", "human": "사람 확인 필요", "partial": "부분 통과(일부 건너뜀)"}


def gate_result(results, ids):
    by = {r["id"]: r for r in results}
    rs = [by[i]["result"] for i in ids if i in by]
    if not rs or all(r == "skip" for r in rs):
        return "skip"
    if "fail" in rs:
        return "fail"
    if "human" in rs:
        return "human"
    return "pass" if all(r == "pass" for r in rs) and len(rs) == len(ids) else "partial"


def render_summary(data):
    meta, results = data.get("meta") or {}, data.get("results") or []
    by = {r["id"]: r for r in results}
    L = ["# VMC 하네스 Windows 실기 검증 결과", "",
         "- 실행: %s · %s · 소요 %s초" % (meta.get("at"), meta.get("platform"), meta.get("elapsed_s")),
         "- 하네스 %s (커밋 %s)%s" % (meta.get("version"), (meta.get("repo_commit") or "")[:10],
                                   " · 격리 실행(사용자 설정 안 건드림)" if meta.get("isolate") else ""),
         "- Claude Code %s · Codex %s" % ((meta.get("env") or {}).get("claude_version") or "없음",
                                         (meta.get("env") or {}).get("codex_version") or "없음"), "",
         "## 출고 기준(release-gate) · 새 확인 항목", "", "| 항목 | 판정 | 근거 체크 |", "|---|---|---|"]
    for gid, title, ids in GATES:
        g = gate_result(results, ids)
        parts = ["%s %s" % (i, {"pass": "✓", "fail": "✗", "skip": "–", "human": "?"}[by[i]["result"]]) for i in ids if i in by]
        L.append("| **%s** %s | %s | %s |" % (gid, title, MARK[g], " · ".join(parts)))
    L += ["", "`win-week`는 하루치 흐름만 본다 — 며칠 사용(일일 집계가 날짜를 넘어 쌓이는지)은 실사용으로 따로 확인.",
          "`learning-verdict`는 이 키트 범위 밖(노출 10회·최대 21일).", ""]
    fails = [r for r in results if r["result"] in ("fail", "human")]
    if fails:
        L += ["## 실패·확인 필요", ""]
        for r in fails:
            L.append("- **%s** %s — %s" % (r["id"], r["title"], r["evidence"]))
        L.append("")
    L += ["## 전체 체크", "", "| id | 내용 | 결과 | 근거 |", "|---|---|---|---|"]
    for r in results:
        L.append("| %s | %s | %s | %s |" % (r["id"], r["title"], MARK[r["result"]],
                                           str(r["evidence"]).replace("|", "/").replace("\n", " ")[:300]))
    L.append("")
    return "\n".join(L)


def analyze(path):
    p = Path(path)
    if p.is_file() and p.suffix == ".zip":
        with zipfile.ZipFile(str(p)) as zf:
            data = json.loads(zf.read("results.json").decode("utf-8"))
    else:
        data = read_json(p / "results.json") if p.is_dir() else read_json(p)
    if not data:
        print("results.json을 못 읽음: %s" % path)
        return 2
    print(render_summary(data))
    sess = (data.get("meta") or {}).get("claude_sessions") or {}
    if sess:
        print("## Claude 세션")
        for k, v in sess.items():
            print("- %s: rc=%s %ss killed=%s tools=%s — %s" % (k, v.get("rc"), v.get("sec"), v.get("killed"),
                                                              ",".join(v.get("tools") or []), v.get("result")))
    return 0


def uninstall(a):
    """설치한 하네스(~/.vmc-harness, PATH)를 지운다. 브레인 카드는 남긴다."""
    home = Path(os.environ.get("VMC_HARNESS_HOME") or (Path.home() / ".vmc-harness"))
    cur = (home / "kernel" / "current")
    if not cur.is_file():
        print("설치된 커널 없음: %s" % home)
        return 1
    eng = home / "kernel" / cur.read_text(encoding="utf-8").strip() / "engine"
    sys.path.insert(0, str(eng))
    from vmc_harness.dist.uninstall import remove_home
    print(json.dumps(remove_home(), ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    p = argparse.ArgumentParser(description="VMC 하네스 Windows 실기 검증 키트")
    p.add_argument("--analyze", metavar="ZIP_OR_DIR", help="결과 zip·폴더의 요약을 다시 출력")
    p.add_argument("--uninstall", action="store_true", help="이 키트가 설치한 하네스(~/.vmc-harness·PATH) 제거")
    p.add_argument("--repo", help="하네스 레포 경로(기본: 이 파일이 레포 안이면 그 레포, 아니면 clone)")
    p.add_argument("--clone", action="store_true", help="레포 안에서 돌려도 새로 clone")
    p.add_argument("--repo-url", default=REPO_URL)
    p.add_argument("--work", help="작업 폴더 상위(기본 임시 폴더)")
    p.add_argument("--out", help="결과 zip 둘 폴더(기본 바탕화면)")
    p.add_argument("--isolate", action="store_true", help="VMC_HARNESS_HOME 격리·PATH·사용자 설정 안 건드림(맥·리눅스는 항상)")
    p.add_argument("--no-ask", action="store_true", help="사람 질문 없이(해당 항목은 사람 확인 필요로)")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--keep", action="store_true", help="정리하지 않음 — 샌드박스·작업 폴더·스케줄러·카드를 남김(디버그)")
    p.add_argument("--skip-claude", action="store_true")
    p.add_argument("--skip-codex", action="store_true")
    p.add_argument("--claude-smoke", action="store_true", help="claude -p 짧은 세션 1회만(MCP·훅 흐름 확인)")
    p.add_argument("--claude-model", help="claude -p 모델(기본: 계정 기본)")
    p.add_argument("--claude-timeout", type=int, default=600)
    p.add_argument("--only", help="쉼표로 단계 번호만(디버그, 예: 0,1,2,4). 1·2는 뒤 단계의 전제")
    a = p.parse_args(argv)
    if a.analyze:
        return analyze(a.analyze)
    if a.uninstall:
        return uninstall(a)
    if sys.version_info < (3, 9):
        print("Python 3.9 이상이 필요합니다")
        return 2
    v = Verify(a)
    banner(["VMC 하네스 실기 검증 — 예상 15~25분 (claude·codex 세션 포함)",
            "사람이 할 일: 약 2~3분 뒤 '화면 보기' 안내가 나오면 25초씩 두 번 보고 y/n 답하기",
            "마지막에 Codex 대화형 확인(선택, s로 건너뛰기 가능)",
            "결과는 바탕화면 zip + 자동 업로드. 작업 폴더: %s" % v.mask(str(v.work))])
    only = set((a.only or "0,1,2,3,4,5,6,7").split(","))
    steps = [("0", "환경 수집", v.step0_env), ("1", "설치", v.step1_install), ("2", "샌드박스·init", v.step2_sandbox),
             ("3", "백그라운드·창 깜빡임", v.step3_background), ("4", "훅 비용", v.step4_hooks),
             ("5", "Claude Code 실세션", v.step5_claude), ("6", "Codex", v.step6_codex), ("7", "스케줄러", v.step7_scheduler)]
    fatal = False
    for n, title, fn in steps:
        if n not in only:
            continue
        if fatal and n != "0":
            v.add("step%s" % n, title, "skip", "앞 단계(설치·init) 실패로 건너뜀")
            continue
        v.say("== %s. %s" % (n, title))
        try:
            fn()
        except Exception as e:
            v.add("step%s" % n, "%s 단계 오류" % title, "fail", "%s: %s" % (type(e).__name__, e),
                  {"traceback": tail(traceback.format_exc(), 3000)})
            if n in ("1", "2"):
                fatal = True
    try:
        v.say("== 수집·정리")
        v.collect()
    except Exception as e:
        v.add("collect", "증거 수집", "fail", repr(e), {"traceback": tail(traceback.format_exc(), 2000)})
    try:
        v.cleanup()
    except Exception as e:
        v.add("cleanup", "정리", "fail", repr(e))
    v.finish()
    if not v.isolate:
        print("\n설치한 하네스는 남겨 둠. 지우려면: python \"%s\" --uninstall  (자동 업데이트는 꺼 둠: vmc-harness update --auto on 으로 다시 켬)"
              % Path(__file__).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
