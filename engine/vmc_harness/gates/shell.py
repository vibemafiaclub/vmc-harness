"""셸 명령이 쓰는(만들기·고치기·지우기) 파일 판정 — bash·zsh·cmd·PowerShell 공통(spec 02 엔진 쓰기 보호·검증물 변조).

완벽한 셸 해석이 아니라 안전 게이트용 보수적 판정이다. 판정할 수 없는 쓰기(인라인 스크립트·xargs·git reset 등)는
`opaque=True`로 알리고(`opaque_cmds`에 그렇게 만든 명령 이름), 호출하는 게이트가 처리를 정한다 —
해석 못 하는 명령은 미리 막지 않고 실행 뒤 대상 파일 상태를 대조한다(gates.opaque_watch).
heredoc(`<<TAG` … `TAG`) 본문은 명령이 아니라 데이터다 — 셸(bash·sh…)이 읽는 본문만 명령으로 해석한다.
명령 전체를 한 번에 읽는다(줄 단위로 자르지 않는다) — 따옴표·명령 치환 안의 줄바꿈·`>`·`;`는 글자다.
같은 명령 안 변수 대입은 풀고, 풀 수 없는 변수로 시작하는 경로는 opaque(`(var)`)로 넘긴다.
"""

import os
import re

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


class Op(str):
    """셸 연산자(구분자·리다이렉트) — 따옴표 안 글자(`">"`)와 구분한다. heredoc 연산자(`<<`)는 body에 본문을 단다."""
    body = None


class Word(str):
    """따옴표를 벗긴 단어. parts = [(종류, 글자)] — lit(글자 그대로)·var(변수 이름)·sub(명령 치환 본문)·arith.
    문자열 값은 따옴표만 벗기고 확장은 원문 모양(`$d`·`$(…)`) 그대로 — 변수를 풀지 않는 소비자(verify_timing 등)는 이걸 쓴다."""
    parts = ()


_OPS = (";;", "&&", "||", "|&", "&>>", "&>", ">>", ">|", ">&", "<<<", "<<-", "<<", "<&", "<>",
        ";", "&", "|", "(", ")", ">", "<")
_OP_START = set(";&|()<>")
_NAME_CH = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_SPECIAL_VARS = set("@*#?$!-0123456789")
# 따옴표 밖 `\` 뒤에서만 이스케이프로 본다 — 그 밖의 `\`는 Windows 경로 구분자로 남긴다(C:\Users\x)
_ESCAPABLE = set(" \t\"'$`\\;&|<>()*?[]#~={}%!")


def _lex(text, i=0, close=None):
    """(토큰 목록, 끝 위치). close=")"이면 짝이 맞는 `)`에서 멈춘다(명령 치환 본문)."""
    toks, parts, buf = [], [], []
    started = False
    pending = []          # 줄 끝에서 본문을 읽을 heredoc [(연산자, 끝 표시, 탭 제거)]
    depth = 0
    n = len(text)

    def flush_lit():
        if buf:
            parts.append(("lit", "".join(buf)))
            del buf[:]

    def end_word():
        nonlocal started
        flush_lit()
        if started:
            w = Word("".join(_render(p) for p in parts))
            w.parts = tuple(parts)
            toks.append(w)
        del parts[:]
        started = False

    while i < n:
        c = text[i]
        if c == "\n":
            end_word()
            toks.append(Op("\n"))
            i += 1
            for op, tag, strip_tabs in pending:
                body = []
                while i < n:
                    j = text.find("\n", i)
                    line = text[i:] if j < 0 else text[i:j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if strip_tabs else line).strip() == tag:
                        break
                    body.append(line)
                op.body = "\n".join(body) + "\n"
            pending = []
            continue
        if c in " \t":
            end_word()
            i += 1
            continue
        if c == "#" and not started:
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "\\":
            nx = text[i + 1] if i + 1 < n else ""
            if nx == "\n":
                i += 2
                continue
            started = True
            if nx and nx in _ESCAPABLE:
                buf.append(nx)
                i += 2
            else:
                buf.append(c)
                i += 1
            continue
        if c == "'":
            started = True
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            buf.append(text[i + 1:j])
            i = j + 1
            continue
        if c == '"':
            started = True
            i = _dquote(text, i + 1, buf, parts, flush_lit)
            continue
        if c == "$" or c == "`":
            started = True
            i = _dollar(text, i, buf, parts, flush_lit, quoted=False)
            continue
        if c in "<>" and i + 1 < n and text[i + 1] == "(":   # 프로세스 치환 <(…)·>(…) — 단어로 본다
            started = True
            flush_lit()
            inner, j = _lex(text, i + 2, close=")")
            parts.append(("sub", text[i + 2:j]))
            i = j + 1
            continue
        op = next((o for o in _OPS if text.startswith(o, i)), None) if c in _OP_START else None
        if op:
            if close == ")" and op == ")" and depth == 0:
                end_word()
                return toks, i
            # fd 번호(2>·2>>·1>&) — 바로 앞 단어가 숫자뿐이면 연산자에 붙인다
            prefix = ""
            if c in "<>" and started and not parts and buf and "".join(buf).isdigit():
                prefix = "".join(buf)
                del buf[:]
                started = False
            else:
                end_word()
            if op == "(":
                depth += 1
            elif op == ")":
                depth -= 1
            o = Op(prefix + op)
            toks.append(o)
            i += len(op)
            if op in ("<<", "<<-"):
                # 끝 표시 단어(따옴표 벗김)
                while i < n and text[i] in " \t":
                    i += 1
                sub, j = _lex_word(text, i)
                if sub:
                    pending.append((o, sub, op == "<<-"))
                    toks.append(Word(sub))
                    i = j
            continue
        started = True
        buf.append(c)
        i += 1
    end_word()
    for op, _, _ in pending:
        op.body = ""
    return toks, n


def _lex_word(text, i):
    """heredoc 끝 표시 한 단어(따옴표 벗김)와 끝 위치."""
    out, n = [], len(text)
    while i < n and text[i] not in " \t\n;&|<>()":
        c = text[i]
        if c in "'\"":
            j = text.find(c, i + 1)
            j = n if j < 0 else j
            out.append(text[i + 1:j])
            i = j + 1
        elif c == "\\" and i + 1 < n:
            out.append(text[i + 1])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out), i


def _dquote(text, i, buf, parts, flush_lit):
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            return i + 1
        if c == "\\" and i + 1 < n and text[i + 1] in '$`"\\\n':
            if text[i + 1] != "\n":
                buf.append(text[i + 1])
            i += 2
            continue
        if c in "$`":
            i = _dollar(text, i, buf, parts, flush_lit, quoted=True)
            continue
        buf.append(c)
        i += 1
    return n


def _dollar(text, i, buf, parts, flush_lit, quoted):
    """`$…`·`` `…` `` 확장 하나를 parts에 넣고 다음 위치를 돌려준다."""
    n = len(text)
    c = text[i]
    if c == "`":
        j = i + 1
        while j < n and text[j] != "`":
            j += 2 if text[j] == "\\" else 1
        flush_lit()
        parts.append(("sub", text[i + 1:min(j, n)]))
        return j + 1
    nx = text[i + 1] if i + 1 < n else ""
    if nx == "(":
        if text.startswith("((", i + 1):
            j = text.find("))", i + 3)
            j = n if j < 0 else j
            flush_lit()
            parts.append(("arith", text[i + 3:j]))
            return j + len("))")
        _, j = _lex(text, i + 2, close=")")
        flush_lit()
        parts.append(("sub", text[i + 2:j]))
        return j + 1
    if nx == "{":
        j = text.find("}", i + 2)
        j = n if j < 0 else j
        inner = text[i + 2:j]
        name = ""
        for ch in inner.lstrip("!#"):
            if ch not in _NAME_CH:
                break
            name += ch
        flush_lit()
        parts.append(("var", name or inner))
        return j + 1
    if nx == "'" and not quoted:   # $'…' ANSI-C 따옴표 — 글자로 본다
        j = i + len("$'")
        out = []
        while j < n and text[j] != "'":
            if text[j] == "\\" and j + 1 < n:
                out.append(text[j:j + len("\\'")])
                j += 2
                continue
            out.append(text[j])
            j += 1
        buf.append("".join(out))
        return j + 1
    if nx and (nx in _NAME_CH and not nx.isdigit()):
        j = i + 1
        while j < n and text[j] in _NAME_CH:
            j += 1
        flush_lit()
        parts.append(("var", text[i + 1:j]))
        return j
    if nx and nx in _SPECIAL_VARS:
        flush_lit()
        parts.append(("var", nx))
        return i + 2
    buf.append(c)
    return i + 1


def _render(p):
    kind, s = p
    if kind == "lit":
        return s
    if kind == "var":
        return "$" + s if s and all(ch in _NAME_CH for ch in s) else "${%s}" % s
    if kind == "arith":
        return "$((%s))" % s
    return "$(%s)" % s


HEREDOC_OPS = ("<<", "<<-")
INPUT_REDIRECTS = {"<", "<<", "<<-", "<<<", "<&", "<>"}
# 줄머리 예약어 — 뒤 단어가 명령이다(`do cp a b`·`then rm x`). 짝 닫는 말(done·fi·})은 명령 없음으로 남긴다(`done > out.txt` 리다이렉트는 산다)
RESERVED_LEAD = {"do", "then", "else", "elif", "if", "while", "until", "!", "{"}


def _segments(command):
    """[토큰 목록] — heredoc 본문 줄은 명령이 아니므로 빠진다(verify_timing·prod_config도 이걸 쓴다)."""
    return [sg[0] for sg in _segments_full(command)]


def _segments_heredoc(command):
    """[(토큰 목록, heredoc 본문)]."""
    return [(sg[0], sg[1]) for sg in _segments_full(command)]


def _segments_full(command):
    """[[토큰 목록, heredoc 본문, 앞 구분자 목록, 뒤 구분자]] — 구분자는 cd가 뒤 명령에 미치는지 가르는 데 쓴다(줄바꿈 = "\n").
    따옴표·명령 치환 안의 줄바꿈·`;`·`>`는 글자다(여러 줄 커밋 메시지·`python3 -c "…"`). heredoc 본문은 그 연산자에 붙는다."""
    text = (command or "").replace("\r\n", "\n").replace("\r", "\n")
    toks, _ = _lex(text)
    segs, cur, pending = [], [], []
    for t in toks + [Op("\n")]:
        if isinstance(t, Op) and (t in SEPARATORS or t == "\n"):
            while cur and not isinstance(cur[0], Op) and cur[0] in RESERVED_LEAD:
                cur = cur[1:]
            if cur:
                body = "".join(o.body or "" for o in cur if isinstance(o, Op) and o.lstrip("0123456789") in HEREDOC_OPS)
                segs.append([cur, body, pending, t])
                pending = []
            pending.append(t)
            cur = []
        else:
            cur.append(t)
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


def analyze(command, cwd=None):
    """{'writes': [경로 문자열], 'deletes': [...], 'opaque': bool, 'commands': [명령 이름]}
    cwd(훅 cwd)를 주면 `cd 폴더;`의 폴더가 있는지 보고 cd 성공을 확신한다. 같은 명령 안 변수 대입(`D=…`·`d=$(mktemp -d)`·
    `for f in a b`)은 풀어서 경로로 쓴다. 풀 수 없는 변수로 시작하는 경로(`$W/x`)는 어디인지 모르므로 경로로 내지 않고
    opaque(`(var)`)로 알린다 — 안전 게이트는 실행 뒤 대조(opaque_watch)로 본다."""
    res = {"writes": [], "deletes": [], "opaque": False, "commands": [], "opaque_cmds": []}
    _analyze(command, res, 0, {}, cwd)
    for k in ("writes", "deletes"):
        keep = []
        for x in res[k]:
            if UNK in x:
                _opaque(res, "(var)")
            elif OUTSIDE not in x:
                keep.append(x)
        res[k] = keep
    return res


def _opaque(res, cmd):
    res["opaque"] = True
    if cmd not in res["opaque_cmds"]:
        res["opaque_cmds"].append(cmd)


CD_CMDS = {"cd", "pushd", "chdir", "set-location", "sl"}
SUBSHELL_SEPS = {"|", "&", "|&"}
_UNKNOWN = object()
UNK = "\x00?"             # 풀 수 없는 변수로 시작하는 경로 표시 — analyze 끝에서 빼고 opaque로 알린다
OUTSIDE = "\x00tmp"       # mktemp가 만든 임시 폴더 — 레포 밖이다. analyze 끝에서 뺀다
DECLARE_CMDS = {"export", "local", "declare", "readonly", "typeset"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _cd_target(args):
    """cd 인자 → 옮겨 갈 폴더 문자열, 알 수 없으면 _UNKNOWN(풀 수 없는 변수·`cd -`·여러 인자 등)."""
    na = [a for a in args if a.lower() not in ("/d", "-l", "-p", "-literalpath", "-path", "--")]
    if not na:
        return "~"
    t = na[0]
    if len(na) > 1 or t == "-" or UNK in t or any(c in t for c in "`*?") or "$" in os.path.expandvars(t):
        return _UNKNOWN
    return t


def _join(cd, path):
    """cd한 폴더 기준 경로. 이미 절대·홈·변수 경로면 그대로."""
    if cd is None or not path:
        return path
    p = path.replace("\\", "/")
    if p.startswith(("/", "~", "$", UNK)) or (len(p) > 1 and p[1] == ":"):
        return path
    return cd.rstrip("/\\") + "/" + path


def _absolute(path):
    p = os.path.expandvars(path).replace("\\", "/")
    return p.startswith(("/", "~", OUTSIDE)) or (len(p) > 1 and p[1] == ":")


def _dir_exists(path, cwd):
    """cd 대상 폴더가 지금 있는가(있으면 cd가 성공한다고 본다). mktemp 폴더는 있다."""
    if OUTSIDE in path:
        return True
    p = os.path.expanduser(os.path.expandvars(path))
    if not os.path.isabs(p):
        if not cwd:
            return False
        p = os.path.join(cwd, p)
    return os.path.isdir(p)


def _assignment(w):
    """`NAME=값` 단어면 (이름, 값 parts), 아니면 None."""
    parts = getattr(w, "parts", ()) or (("lit", str(w)),)
    if not parts or parts[0][0] != "lit" or "=" not in parts[0][1]:
        return None
    name, _, rest = parts[0][1].partition("=")
    if not _IDENT.match(name):
        return None
    return name, ((("lit", rest),) if rest else ()) + tuple(parts[1:])


def _sub_value(body, cd):
    """명령 치환 값 — 아는 것만(mktemp = 임시 폴더, pwd = 지금 폴더). 모르면 None."""
    toks = body.split()
    if not toks:
        return None
    if _base(toks[0]) == "mktemp":
        return OUTSIDE
    if toks == ["pwd"]:
        return cd if cd is not None else "."
    return None


def _values(parts, env, cd):
    """parts → 가능한 값 목록. 첫 조각부터 알 수 없으면 None(어디인지 모름). 중간부터 모르면 원문 모양으로 둔다(폴더는 안다)."""
    outs = [""]
    for kind, s in parts:
        vals = None
        if kind == "lit":
            vals = [s]
        elif kind == "var":
            if s in env:
                vals = env[s]
            elif s == "PWD":
                vals = [cd if cd is not None else "."]
            elif s in os.environ:
                vals = ["$" + s]   # 어댑터가 환경 변수로 푼다(HOME·TMPDIR 등)
        elif kind == "sub":
            v = _sub_value(s, cd)
            vals = [v] if v is not None else None
        if vals is None:
            if all(o == "" for o in outs):
                return None
            vals = [_render((kind, s))]
        outs = [o + v for o in outs for v in vals]
    return outs


def _expand(seg, env, cd):
    """단어를 같은 명령 안 변수로 푼다. 첫 조각부터 모르는 단어는 UNK 표시를 붙여 남긴다(인자 위치는 지킨다)."""
    out = []
    for t in seg:
        if isinstance(t, Op) or not getattr(t, "parts", ()):
            out.append(t)
            continue
        if not any(k != "lit" for k, _ in t.parts):
            out.append(t)
            continue
        if str(t).lower() in NULL_TARGETS:
            out.append(t)
            continue
        vals = _values(t.parts, env, cd)
        out.extend([UNK + str(t)] if vals is None else vals)
    return out


def _subs(seg, res, depth, cwd):
    """단어 안 명령 치환 `$(…)`·`` `…` ``의 쓰기도 본다(명령 이름 목록에는 넣지 않는다)."""
    for t in seg:
        for kind, body in getattr(t, "parts", ()) or ():
            if kind == "sub" and body.strip():
                inner = {"writes": [], "deletes": [], "opaque": False, "commands": [], "opaque_cmds": []}
                _analyze(body, inner, depth + 1, {}, cwd)
                res["writes"].extend(inner["writes"])
                res["deletes"].extend(inner["deletes"])
                for c in inner["opaque_cmds"]:
                    _opaque(res, c)


def _assign_all(words, env, cd):
    for w in words:
        a = _assignment(w)
        if a:
            env[a[0]] = _values(a[1], env, cd) if a[1] else [""]


def _analyze(command, res, depth, env=None, cwd=None):
    if depth > int(config.get("gates.shell_max_nest", profile="standard")):
        _opaque(res, "(nest)")
        return
    env = {} if env is None else env
    # 이 명령 줄의 현재 폴더(None = 훅 cwd). 괄호 subshell 안의 cd는 괄호를 나오면 돌아간다.
    # cd 성공이 확실할 때(sure)만 뒤 명령의 상대 경로를 그 폴더 기준으로만 본다 — `&&`로 이어지거나 대상 폴더가 지금 있으면 확실.
    # 아니면(`;`·줄바꿈·`||` 뒤인데 폴더가 없음) cd가 실패해도 원래 폴더에서 계속 돌므로 두 경로를 다 대상으로 본다
    cd, sure, stack, pushed = None, True, [], []
    for seg, heredoc, before, after in _segments_full(command):
        for sep in before:
            if sep == "(":
                stack.append((cd, sure))
            elif sep == ")" and stack:
                cd, sure = stack.pop()
        nw, nd = len(res["writes"]), len(res["deletes"])
        _subs(seg, res, depth, cwd)
        lead = 0
        while lead < len(seg) and not isinstance(seg[lead], Op) and _assignment(seg[lead]):
            lead += 1
        rest = [t for t in seg[lead:]]
        if lead and not [t for t in rest if not isinstance(t, Op)]:
            _assign_all(seg[:lead], env, cd)   # 대입만 있는 명령 — 뒤 명령에 이어진다
        seg = _expand(_redirects(rest, res, env, cd), env, cd)
        while seg and _base(seg[0]) in PREFIX_CMDS:
            seg = seg[1:]
            while seg and seg[0].startswith("-"):
                seg = seg[1:]
            while seg and _assignment(seg[0]):
                seg = seg[1:]
        if seg:
            cmd = _base(seg[0])
            args = seg[1:]
            res["commands"].append(cmd)
            piped = after in SUBSHELL_SEPS or any(b in SUBSHELL_SEPS for b in before[-1:])
            if cmd in ("for", "select"):
                name = str(args[0]) if args else ""
                if _IDENT.match(name):
                    words = args[len(["in", name]):] if len(args) > 1 and args[1] == "in" else None
                    env[name] = None if words is None or any(UNK in w for w in words) else [str(w) for w in words]
            elif cmd == "read":
                for a in _nonopt(args):
                    if _IDENT.match(str(a)):
                        env[str(a)] = None
            elif cmd in DECLARE_CMDS:
                _assign_all([t for t in rest[1:] if not isinstance(t, Op)], env, cd)
            elif cmd in CD_CMDS or cmd == "popd":
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
                        absolute = _absolute(t)
                        cd = _join(cd, t)
                        ok = after == "&&" or _dir_exists(cd, cwd)
                        sure = ok if absolute else (sure and ok)
            else:
                _command(cmd, args, res, depth, heredoc)
        if cd is not None:
            for k, n in (("writes", nw), ("deletes", nd)):
                new = res[k][n:]
                joined = [_join(cd, x) for x in new]
                res[k][n:] = joined if sure else joined + [x for x in new if x not in joined]


def _redirects(seg, res, env=None, cd=None):
    out, i = [], 0
    while i < len(seg):
        t = seg[i]
        op = t.lstrip("0123456789") if isinstance(t, Op) else None
        if op in REDIRECTS:
            nxt = seg[i + 1] if i + 1 < len(seg) else None
            if op == ">&" and nxt and (nxt.isdigit() or nxt == "-"):
                i += 2
                continue
            if nxt is not None and not isinstance(nxt, Op) and nxt.lower() not in NULL_TARGETS and not nxt.startswith("&"):
                res["writes"].extend(_expand([nxt], env or {}, cd))
            i += 1 if isinstance(nxt, Op) else 2
            continue
        if op in INPUT_REDIRECTS:   # 입력(<, <<TAG, <<<) — 쓰기가 아니다. 뒤 단어(파일·태그·문자열)도 인자가 아니다
            i += 2
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
