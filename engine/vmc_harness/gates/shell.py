"""셸 명령이 쓰는(만들기·고치기·지우기) 파일 판정 — bash·zsh·cmd·PowerShell 공통(spec 02 엔진 쓰기 보호·검증물 변조).

완벽한 셸 해석이 아니라 안전 게이트용 보수적 판정이다. 판정할 수 없는 쓰기(인라인 스크립트·xargs·git reset 등)는
`opaque=True`로 알리고(`opaque_cmds`에 그렇게 만든 명령 이름), 호출하는 게이트가 처리를 정한다 —
해석 못 하는 명령은 미리 막지 않고 실행 뒤 대상 파일 상태를 대조한다(gates.opaque_watch).
heredoc(`<<TAG` … `TAG`) 본문은 명령이 아니라 데이터다 — 셸(bash·sh…)이 읽는 본문만 명령으로 해석한다.
"""

import os
import re
import shlex

from vmc_harness.core import config

SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "|&", ";;"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>", ">&"}
NULL_TARGETS = {"/dev/null", "nul", "$null", "/dev/stdout", "/dev/stderr"}
PREFIX_CMDS = {"sudo", "command", "env", "nohup", "time", "exec", "nice", "builtin", "call", "&", "."}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
POWERSHELLS = {"powershell", "pwsh"}
INTERPRETERS = {"python", "python3", "py", "node", "perl", "ruby", "php", "deno", "bun", "osascript"}
DELETE_CMDS = {"rm", "rmdir", "unlink", "shred", "del", "erase", "rd", "remove-item", "ri"}
TOUCH_CMDS = {"touch", "mkdir", "truncate", "ln", "new-item", "ni", "md"}
MODE_FIRST = {"chmod", "chown", "chgrp"}
COPY_CMDS = {"cp", "install", "rsync", "scp", "copy", "xcopy", "robocopy", "copy-item", "cpi", "ditto"}
MOVE_CMDS = {"mv", "move", "move-item", "mi", "rename-item", "ren", "rename"}
PS_WRITE_CMDS = {"set-content", "sc", "add-content", "ac", "out-file", "clear-content", "clc", "export-csv", "set-item"}
PS_PATH_PARAMS = {"-path", "-literalpath", "-destination", "-filepath", "-file", "-target"}
OPAQUE_CMDS = {"xargs", "patch", "eval", "source", "make", "npx", "pnpm", "npm", "yarn"}
OPAQUE_GIT = {"reset", "clean", "stash", "apply", "am", "merge", "rebase", "pull", "cherry-pick", "revert", "switch"}


def _tokens(text):
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&|<>()")
    lex.whitespace_split = True
    lex.escape = ""          # Windows 경로의 \ 를 그대로 둔다
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return text.split()


HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
INPUT_REDIRECTS = {"<", "<<", "<<<", "<&"}


def _segments(command):
    """[토큰 목록] — heredoc 본문 줄은 명령이 아니므로 빠진다(verify_timing·prod_config도 이걸 쓴다)."""
    return [toks for toks, _ in _segments_heredoc(command)]


def _segments_heredoc(command):
    """[(토큰 목록, heredoc 본문)] — `<<TAG`로 연 본문(다음 줄부터 TAG 줄까지)은 명령 줄로 읽지 않고 그 segment에 붙인다."""
    return [(sg[0], sg[1]) for sg in _segments_full(command)]


def _segments_full(command):
    """[[토큰 목록, heredoc 본문, 앞 구분자 목록, 뒤 구분자]] — 구분자는 cd가 뒤 명령에 미치는지 가르는 데 쓴다(줄바꿈 = "\n")."""
    segs = []
    lines = (command or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    pending = []
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        first = len(segs)
        cur = []
        for t in _tokens(line):
            if t in SEPARATORS:
                if cur:
                    segs.append([cur, "", pending, t])
                    pending = []
                elif segs and segs[-1][3] is None:
                    segs[-1][3] = t
                pending.append(t)
                cur = []
            else:
                cur.append(t)
        if cur:
            segs.append([cur, "", pending, "\n"])
            pending = []
        for m in HEREDOC.finditer(line):
            body = []
            while i < len(lines):
                ln = lines[i]
                i += 1
                if ln.strip() == m.group(2):
                    break
                body.append(ln)
            owner = next((sg for sg in segs[first:] if "<<" in sg[0]), segs[-1] if len(segs) > first else None)
            if owner is not None:
                owner[1] += "\n".join(body) + "\n"
    return segs


def _base(tok):
    b = os.path.basename(tok.replace("\\", "/")).lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if b.endswith(ext):
            b = b[: -len(ext)]
    return b


_WIN_SWITCH = re.compile(r"^/[A-Za-z]{1,3}[:]?\S*$")
CMD_BUILTINS = {"copy", "move", "del", "erase", "rd", "xcopy", "robocopy", "ren", "rename", "md"}


def _nonopt(args, cmd=None):
    out = []
    for a in args:
        if a.startswith("-"):
            continue
        if cmd in CMD_BUILTINS and _WIN_SWITCH.match(a) and "/" not in a[1:]:
            continue
        out.append(a)
    return out


def analyze(command):
    """{'writes': [경로 문자열], 'deletes': [...], 'opaque': bool, 'commands': [명령 이름]}"""
    res = {"writes": [], "deletes": [], "opaque": False, "commands": [], "opaque_cmds": []}
    _analyze(command, res, depth=0)
    return res


def _opaque(res, cmd):
    res["opaque"] = True
    if cmd not in res["opaque_cmds"]:
        res["opaque_cmds"].append(cmd)


CD_CMDS = {"cd", "pushd", "chdir", "set-location", "sl"}
SUBSHELL_SEPS = {"|", "&", "|&"}
_UNKNOWN = object()


def _cd_target(args):
    """cd 인자 → 옮겨 갈 폴더 문자열, 알 수 없으면 _UNKNOWN(변수·`cd -`·여러 인자 등)."""
    na = [a for a in args if a.lower() not in ("/d", "-l", "-p", "-literalpath", "-path", "--")]
    if not na:
        return "~"
    if len(na) > 1 or na[0] == "-" or any(c in na[0] for c in "$`*?"):
        return _UNKNOWN
    return na[0]


def _join(cd, path):
    """cd한 폴더 기준 경로. 이미 절대·홈·변수 경로면 그대로."""
    if cd is None or not path:
        return path
    p = path.replace("\\", "/")
    if p.startswith(("/", "~", "$")) or (len(p) > 1 and p[1] == ":"):
        return path
    return cd.rstrip("/\\") + "/" + path


def _analyze(command, res, depth):
    if depth > int(config.get("gates.shell_max_nest", profile="standard")):
        _opaque(res, "(nest)")
        return
    # 이 명령 줄의 현재 폴더(None = 훅 cwd). 괄호 subshell 안의 cd는 괄호를 나오면 돌아간다.
    # cd가 `&&`로 이어질 때만 확실(sure) — `;`·줄바꿈·`||` 뒤는 cd가 실패해도 원래 폴더에서 계속 돌므로 두 경로를 다 대상으로 본다
    cd, sure, stack, pushed = None, True, [], []
    for seg, heredoc, before, after in _segments_full(command):
        for sep in before:
            if sep == "(":
                stack.append((cd, sure))
            elif sep == ")" and stack:
                cd, sure = stack.pop()
        nw, nd = len(res["writes"]), len(res["deletes"])
        seg = _redirects(seg, res)
        while seg and ("=" in seg[0] and not seg[0].startswith("-") and seg[0].split("=")[0].replace("_", "").isalnum()):
            seg = seg[1:]
        while seg and _base(seg[0]) in PREFIX_CMDS:
            seg = seg[1:]
            while seg and seg[0].startswith("-"):
                seg = seg[1:]
        if seg:
            cmd = _base(seg[0])
            args = seg[1:]
            res["commands"].append(cmd)
            piped = after in SUBSHELL_SEPS or any(b in SUBSHELL_SEPS for b in before[-1:])
            if cmd in CD_CMDS or cmd == "popd":
                if piped:
                    pass
                elif cmd == "popd":
                    if pushed:
                        cd, sure = pushed.pop()
                    else:
                        cd = None
                        _opaque(res, "(cd)")
                else:
                    t = _cd_target(args)
                    if cmd == "pushd":
                        pushed.append((cd, sure))
                    if t is _UNKNOWN:
                        cd = None
                        _opaque(res, "(cd)")   # 어디로 갔는지 모름 — 실행 뒤 대조가 레포 변경을 본다
                    else:
                        cd = _join(cd, t)
                        sure = sure and after == "&&"
            else:
                _command(cmd, args, res, depth, heredoc)
        if cd is not None:
            for k, n in (("writes", nw), ("deletes", nd)):
                new = res[k][n:]
                joined = [_join(cd, x) for x in new]
                res[k][n:] = joined if sure else joined + [x for x in new if x not in joined]


def _redirects(seg, res):
    out, i = [], 0
    while i < len(seg):
        t = seg[i]
        if t in REDIRECTS or (t[:-1].isdigit() and t.endswith(">")):
            nxt = seg[i + 1] if i + 1 < len(seg) else None
            if t == ">&" and nxt and nxt.isdigit():
                i += 2
                continue
            if nxt and nxt.lower() not in NULL_TARGETS and not nxt.startswith("&"):
                res["writes"].append(nxt)
            i += 2
            continue
        if t in INPUT_REDIRECTS:   # 입력(<, <<TAG, <<<) — 쓰기가 아니다. 뒤 단어(파일·태그·문자열)도 인자가 아니다
            i += 2
            continue
        if t.isdigit() and i + 1 < len(seg) and seg[i + 1] in REDIRECTS:
            i += 1
            continue
        out.append(t)
        i += 1
    return out


def _command(cmd, args, res, depth, heredoc=""):
    if cmd in SHELLS:
        inner = _flag_value(args, ("-c", "-lc", "-ic", "-xc"))
        if inner is not None:
            _analyze(inner, res, depth + 1)
        elif _nonopt(args):
            _opaque(res, cmd)   # 스크립트 파일 실행
        elif heredoc.strip():
            _analyze(heredoc, res, depth + 1)   # bash <<EOF — 본문이 명령이다
        else:
            _opaque(res, cmd)   # 표준 입력(파이프)으로 받은 스크립트
        return
    if cmd == "cmd":
        for i, a in enumerate(args):
            if a.lower() in ("/c", "/k"):
                _analyze(" ".join(args[i + 1:]), res, depth + 1)
                return
        return
    if cmd in POWERSHELLS:
        inner = _flag_value(args, ("-command", "-c"), ci=True)
        if inner is not None:
            _analyze(inner, res, depth + 1)
        else:
            _opaque(res, cmd)
        return
    if cmd in INTERPRETERS:
        _opaque(res, cmd)   # 인라인 코드·표준 입력·스크립트 — 무엇을 쓰는지 모른다
        if _flag_value(args, ("-c", "-e", "--eval", "-p", "-i", "-pi", "-ne", "-pe")) is not None or any(a.startswith("-i") for a in args):
            if cmd in ("perl", "ruby") and any(a.startswith("-i") or a.startswith("-pi") for a in args):
                res["writes"].extend(_nonopt(args)[1:])
        return
    if cmd in ("vmc-harness",):
        return
    if cmd == "git":
        _git(args, res)
        return
    if cmd in OPAQUE_CMDS:
        _opaque(res, cmd)
        return
    if cmd == "tee":
        res["writes"].extend(_nonopt(args))
        return
    if cmd in DELETE_CMDS:
        res["deletes"].extend(_ps_paths(args) or _nonopt(args, cmd))
        return
    if cmd in TOUCH_CMDS:
        vals = _ps_paths(args) or _nonopt(_skip_valued(args, ("-s", "--size", "-m", "-ItemType", "-itemtype", "-Value", "-value")))
        res["writes"].extend(vals)
        return
    if cmd in MODE_FIRST:
        res["writes"].extend(_nonopt(args)[1:])
        return
    if cmd in COPY_CMDS:
        t = _flag_value(args, ("-t", "--target-directory"))
        ps = _ps_param(args, ("-destination",))
        if ps:
            res["writes"].append(ps)
        elif t:
            res["writes"].append(t)
        else:
            na = _nonopt(args, cmd)
            if cmd == "robocopy" and len(na) >= 2:
                res["writes"].append(na[1])
            elif na:
                res["writes"].append(na[-1])
        return
    if cmd in MOVE_CMDS:
        ps = _ps_paths(args)
        na = _nonopt(args, cmd)
        res["writes"].extend(ps or na)
        if not ps:
            res["deletes"].extend(na[:-1])
        return
    if cmd in PS_WRITE_CMDS:
        ps = _ps_paths(args)
        if ps:
            res["writes"].extend(ps)
        else:
            na = _nonopt(args)
            if na:
                res["writes"].append(na[0])
        return
    if cmd in ("sed", "gsed"):
        if any(a == "-i" or a.startswith("-i") or a.startswith("--in-place") or (a.startswith("-") and not a.startswith("--") and "i" in a[1:]) for a in args):
            has_script_flag = any(a in ("-e", "-f", "--expression", "--file") for a in args)
            # BSD sed `-i ''`(빈 백업 접미사)의 빈 인자는 파일이 아니다
            files = [a for a in _nonopt(_skip_valued(args, ("-e", "-f", "--expression", "--file"))) if a != ""]
            res["writes"].extend(files if has_script_flag else files[1:])
        return
    if cmd == "dd":
        for a in args:
            if a.startswith("of="):
                res["writes"].append(a[len("of="):])
        return
    if cmd == "find":
        if any(a in ("-delete", "-exec", "-execdir", "-ok") for a in args):
            na = []
            for a in args:
                if a.startswith("-") or a in ("(", ")", "!"):
                    break
                na.append(a)
            res["deletes"].extend(na or ["."])
            _opaque(res, cmd)
        return


def _git(args, res):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c") else 1
    if i >= len(args):
        return
    sub, rest = args[i], args[i + 1:]
    if sub == "rm":
        res["deletes"].extend(_nonopt(rest))
    elif sub == "mv":
        na = _nonopt(rest)
        res["writes"].extend(na)
        res["deletes"].extend(na[:-1])
    elif sub in ("checkout", "restore"):
        if "--" in rest:
            res["writes"].extend(rest[rest.index("--") + 1:])
        elif sub == "restore":
            res["writes"].extend(_nonopt(rest))
        else:
            _opaque(res, "git")
    elif sub in OPAQUE_GIT:
        _opaque(res, "git")


def _flag_value(args, flags, ci=False):
    for i, a in enumerate(args):
        k = a.lower() if ci else a
        if k in flags and i + 1 < len(args):
            return args[i + 1]
    return None


def _skip_valued(args, flags):
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in flags:
            skip = True
            continue
        out.append(a)
    return out


def _ps_param(args, names):
    for i, a in enumerate(args):
        if a.lower() in names and i + 1 < len(args):
            return args[i + 1]
    return None


def _ps_paths(args):
    out = []
    for i, a in enumerate(args):
        if a.lower() in PS_PATH_PARAMS and i + 1 < len(args):
            out.append(args[i + 1])
    return out


def all_targets(command):
    a = analyze(command)
    return a["writes"] + a["deletes"], a["opaque"]
