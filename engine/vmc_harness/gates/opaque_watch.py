"""해석 못 한 셸 명령(opaque)의 실행 뒤 대조 (spec 02 — 엔진 쓰기 보호·보호 영역·경로형 생성 게이트, 안전).

python heredoc·스크립트 실행처럼 무엇을 쓰는지 해석할 수 없는 명령은 명령 문자열에 경로가 보인다는 이유로 미리 막지 않는다
(읽기만 하는 명령이 막히고, 문자열에 안 보이는 쓰기는 놓친다). 대신:
- PreToolUse(`take`): 대상 파일만 상태를 적어 둔다 — 장부 `.harness/ledger/`·증거 `.harness/cache/evidence/`·
  보호 영역 glob·경로형 생성 게이트 glob. 레포 전체를 훑지 않는다(git ls-files를 그 경로로 한정).
  · git 추적 + 변경 없음: 아무것도 적지 않는다 — 실행 뒤 git이 변경으로 보이면 index에서 되돌린다
  · 그 밖(변경됨·미추적): 크기·수정 시각 + 사본(파일당 gates.opaque_backup_max_bytes 이하)
  · 증거는 사본 없이 — 바뀌거나 새로 생긴 증거 파일은 지운다(무효화. 완료 게이트가 새 증거를 요구한다)
- PostToolUse·PostToolUseFailure(`check`): 다시 적어 비교 → 바뀐 파일을 게이트별로 판정(엔진 쓰기 보호는 항상,
  보호 영역은 승인 카드 판정 protected.check, 경로형 생성 게이트는 generated.evaluate) → 막을 것이면 되돌리고 차단으로 기록·에이전트에 알린다.
- 같은 시간에 엔진 프로세스(vmc-harness 명령·백그라운드 판정)가 쓴 장부·증거는 core.jsonio 쓰기 일지로 가려 되돌리지 않는다.
- git 명령만으로 opaque인 경우(pull·merge·reset·stash 등)는 대조하지 않는다 — 이력에서 온 변경이다.
- 기록을 못 하면(git 실패·대상 과다) `take`가 False — 엔진 쓰기 보호가 옛 방식(문자열에 장부 경로가 보이면 미리 막기)으로 돌아간다.
"""

import base64
import hashlib
import os
import time
from pathlib import Path

from vmc_harness.core import config, paths
from vmc_harness.core.gitutil import git, git_bytes
from vmc_harness.core.jsonio import engine_writes_log, read_json, write_json
from vmc_harness.gates import globs

LEDGER = ".harness/ledger/"
EVIDENCE = ".harness/cache/evidence/"


def needed(ctx):
    """대조가 필요한 호출인가 — 해석 못 한 셸 명령이고, git만으로 opaque가 아니다."""
    if not (ctx.command and ctx.state.get("shell_opaque")):
        return False
    cmds = ctx.state.get("shell_opaque_cmds") or []
    return not cmds or any(c != "git" for c in cmds)


def _dir(root):
    return paths.cache_dir(root) / "gates" / "opaque"


def _key(ctx):
    tid = (ctx.raw or {}).get("tool_use_id") or (ctx.raw or {}).get("call_id")
    body = "%s|%s|%s" % (ctx.session_id or "", tid or "", "" if tid else (ctx.command or ""))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]


def _record_file(root, key):
    return _dir(root) / ("%s.json" % key)


def _watch_globs(root):
    from vmc_harness.gates import generated, protected
    from vmc_harness.gates.common import verification_globs
    prot = [e["glob"] for e in protected.entries(root)]
    try:
        gen = generated.path_trigger_globs(root)
    except Exception:
        gen = []
    tg, eg = verification_globs(root)
    return prot, gen, list(tg) + list(eg)


def _pathspec(g):
    g = globs.norm(g)
    if g.endswith("/"):
        g += "**"
    if "*" in g or "?" in g or "[" in g:
        return (":(glob,icase)" if os.name == "nt" else ":(glob)") + g
    return g


def _git_list(root, specs):
    """{rel: 'dirty'|'untracked'} — 경로 한정 git ls-files 한 번(변경 없는 추적 파일은 나오지 않는다). 실패하면 None.
    -m은 수정 시각만 바뀐 파일은 내용까지 비교해 거른다(줄끝 변환 포함)."""
    rc, out, _ = git(root, "-c", "core.quotepath=off", "ls-files", "-z", "-t", "-o", "-m",
                     "--exclude-standard", "--", *specs, timeout=float(config.get("gates.opaque_git_timeout_s", root)))
    if rc != 0:
        return None
    res = {}
    for item in out.split("\0"):
        if len(item) < len("C x") or item[1] != " ":
            continue
        res[globs.norm(item[2:])] = "untracked" if item[0] == "?" else "dirty"
    return res


def _walk(root, rel_dir):
    base = Path(root) / rel_dir
    out = []
    if not base.is_dir():
        return out
    for dp, _, fns in os.walk(str(base)):
        for fn in fns:
            out.append(globs.norm(os.path.relpath(os.path.join(dp, fn), str(root))))
    return out


def _cats(rel, prot, gen, ver=()):
    c = []
    if rel.startswith(LEDGER):
        c.append("ledger")
    if rel.startswith(EVIDENCE):
        c.append("evidence")
    if prot and globs.match_any(rel, prot):
        c.append("protected")
    if gen and globs.match_any(rel, gen):
        c.append("generated")
    if ver and globs.match_any(rel, ver):
        c.append("verification")
    return c


def _sig(root, rel):
    try:
        st = os.stat(os.path.join(str(root), rel))
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def _listing(root, prot, gen, ver=()):
    """{rel: (상태, 분류)} — 변경된 추적 파일·미추적 파일·증거만(변경 없는 추적 파일은 git index가 기억한다). 실패면 None."""
    specs = [LEDGER.rstrip("/")] + [_pathspec(g) for g in list(prot) + list(gen) + list(ver)]
    st = _git_list(root, specs)
    if st is None:
        return None
    for rel in _walk(root, EVIDENCE):
        st.setdefault(rel, "untracked")
    out = {}
    for rel, status in st.items():
        c = _cats(rel, prot, gen, ver)
        if c:
            out[rel] = (status, c)
    return out


def _trim_log(root):
    log = engine_writes_log(root)
    try:
        if log.stat().st_size > int(config.get("gates.engine_writes_log_max_bytes", root)):
            data = log.read_bytes()
            keep = data[len(data) // 2:]
            keep = keep[keep.find(b"\n") + 1:]
            log.write_bytes(keep)
    except OSError:
        pass


def _drop_stale(root):
    d = _dir(root)
    if not d.is_dir():
        return
    limit = float(config.get("gates.opaque_stale_min", root)) * 60
    for f in d.glob("*.json"):
        try:
            if time.time() - f.stat().st_mtime > limit:
                f.unlink()
        except OSError:
            pass


def take(ctx):
    """실행 전 기록. 기록했으면 True, 대조할 수 없으면 False(호출자가 옛 방식으로)."""
    root = ctx.root
    try:
        _drop_stale(root)
        _trim_log(root)
        prot, gen, ver = _watch_globs(root)
        lst = _listing(root, prot, gen, ver)
        if lst is None or len(lst) > int(config.get("gates.opaque_watch_max_files", root)):
            return False
        cap = int(config.get("gates.opaque_backup_max_bytes", root))
        files, backup = {}, {}
        for rel, (status, cats) in lst.items():
            sig = _sig(root, rel)
            files[rel] = {"sig": sig, "status": status, "cats": cats}
            if sig and "evidence" not in cats and sig[0] <= cap:
                try:
                    backup[rel] = base64.b64encode((Path(root) / rel).read_bytes()).decode("ascii")
                except OSError:
                    pass
        write_json(_record_file(root, _key(ctx)), {
            "at_ns": time.time_ns(), "session_id": ctx.session_id, "command": (ctx.command or "")[:500],
            "protected_globs": prot, "generated_globs": gen, "verification_globs": ver, "files": files, "backup": backup})
        return True
    except Exception:
        return False


def _engine_written(root, since_ns):
    log = engine_writes_log(root)
    out = set()
    try:
        with log.open("r", encoding="utf-8") as f:
            for line in f:
                t, _, rel = line.rstrip("\n").partition("\t")
                if t.isdigit() and int(t) >= since_ns and rel:
                    out.add(globs.norm(rel))
    except OSError:
        pass
    return out


def _changes(root, rec):
    """{rel: {'kind': add|modify|delete, 'cats': [...]}} — 실제로 바뀐 파일만.
    실행 전 목록에 없던 파일이 지금 '변경됨'이면 실행 전에는 변경 없는 추적 파일이었다(index에서 되돌린다 — rec["files"]에 표시)."""
    prot, gen = rec.get("protected_globs") or [], rec.get("generated_globs") or []
    now = _listing(root, prot, gen, rec.get("verification_globs") or [])
    if now is None:
        now = {}
    out = {}
    for rel in set(rec["files"]) | set(now):
        pre = rec["files"].get(rel)
        sig = _sig(root, rel)
        if pre is None:
            status, cats = now[rel]
            if status == "untracked":
                out[rel] = {"kind": "add", "cats": cats}
            else:
                rec["files"][rel] = {"sig": True, "status": "clean", "cats": cats}
                out[rel] = {"kind": "modify" if sig else "delete", "cats": cats}
            continue
        if sig == pre.get("sig"):
            continue
        cats = pre["cats"]
        if not pre.get("sig"):
            out[rel] = {"kind": "add", "cats": cats}
            continue
        if not sig:
            out[rel] = {"kind": "delete", "cats": cats}
            continue
        b = rec["backup"].get(rel)
        if b is not None:
            try:
                if (Path(root) / rel).read_bytes() == base64.b64decode(b):
                    continue
            except OSError:
                pass
        out[rel] = {"kind": "modify", "cats": cats}
    return out


def _revert(root, rec, rels):
    """되돌린다. 되돌리지 못한 파일 목록."""
    failed, from_index = [], []
    for rel in rels:
        pre = rec["files"].get(rel)
        p = Path(root) / rel
        try:
            if not pre or not pre.get("sig"):
                if p.is_file():
                    p.unlink()          # 이번 명령이 만든 파일
                continue
            if "evidence" in pre["cats"]:
                if p.is_file():
                    p.unlink()          # 바뀐 증거는 무효화(완료 게이트가 새 증거를 요구)
                continue
            if pre["status"] == "clean":
                if "ledger" in pre["cats"]:
                    # 하네스가 쓴 장부는 git 원본 바이트 그대로(Windows autocrlf가 LF→CRLF로 바꾸지 않게).
                    # 고객 파일은 아래 checkout-index — 그 PC의 git이 체크아웃하는 모양 그대로
                    rc, blob = git_bytes(root, "cat-file", "blob", ":" + rel)
                    if rc == 0:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(blob)
                        continue
                from_index.append(rel)
                continue
            b = rec["backup"].get(rel)
            if b is None:
                failed.append(rel)
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode(b))
        except OSError:
            failed.append(rel)
    if from_index:
        rc, _, _ = git(root, "checkout-index", "-f", "-q", "--", *from_index)
        if rc != 0:
            failed.extend(from_index)
    return failed


def check(ctx, task):
    """실행 뒤 대조. (막을 결과 HookResult 또는 None, 되돌린 파일, 되돌리지 못한 파일)"""
    from vmc_harness.gates import generated, protected
    from vmc_harness.gates.common import block, short_list
    root = ctx.root
    f = _record_file(root, _key(ctx))
    rec = read_json(f, None)
    if not rec:
        return None, [], []
    try:
        f.unlink()
    except OSError:
        pass
    ch = _changes(root, rec)
    if not ch:
        return None, [], []
    engine_ok = _engine_written(root, int(rec.get("at_ns") or 0))
    revert, results = set(), []
    eng = sorted(r for r, c in ch.items() if ({"ledger", "evidence"} & set(c["cats"])) and r not in engine_ok)
    if eng:
        revert |= set(eng)
        label = "장부(L2)" if any(r.startswith(LEDGER) for r in eng) else "증거"
        results.append(("engine_write_guard", block(
            "엔진 쓰기 보호: 해석 못 한 셸 명령이 %s 파일을 바꿔 되돌림 (%s)" % (label, short_list(eng)),
            "엔진 쓰기 보호: 해석 못 한 셸 명령이 %s 파일을 바꿔 되돌림" % label)))
    prot = sorted(r for r, c in ch.items() if "protected" in c["cats"])
    if prot:
        c2 = _copy(ctx, write_paths=prot)
        r = protected.check(c2, task)
        if r:
            ctx.state["approval_created"] = c2.state.get("approval_created")
            revert |= set(prot)
            r.reason = _after(r.reason)
            results.append((protected.NAME, r))
    ver = sorted(r for r, c in ch.items() if "verification" in c["cats"] and c["kind"] != "add")
    if ver:   # 검증물 변조(현장 결함 C3·C4와 같은 부류 — 문자열 짐작 대신 실행 뒤 대조). 새 파일 추가는 막지 않는다
        from vmc_harness.gates import tamper
        existed = tamper._existing(root, ctx, task)
        hit = [r for r in ver if existed(r)]
        bad = [r for r in hit if not tamper._declared(task, r)]
        if hit and not bad:
            from vmc_harness.gates.common import log_event
            log_event(root, "test_change_used", task_id=(task or {}).get("task_id"), files=hit)
        if bad:
            revert |= set(bad)
            results.append((tamper.NAME, block(
                "검증물 변조 금지: 해석 못 한 셸 명령이 기존 검증물을 바꿔 되돌림 (%s)" % short_list(bad),
                "검증물 변조 금지: 해석 못 한 셸 명령이 기존 테스트·기대값을 바꿔 되돌림")))
    gen = {r: c["kind"] for r, c in ch.items() if "generated" in c["cats"]}
    if gen:
        c3 = _copy(ctx, event="PreToolUse")
        c3.state["opaque_changes"] = gen
        r = generated.evaluate(c3, task, path_only=True)
        if r:
            revert |= set(c3.state.get("blocked_hit") or gen)
            ctx.state["blocked_gate_id"] = c3.state.get("blocked_gate_id")
            r.reason = _after(r.reason)
            results.append((generated.NAME, r))
    if not revert:
        return None, [], []
    failed = _revert(root, rec, sorted(revert))
    gate, res = results[0]
    if failed:
        from vmc_harness.core import inject
        extra = " / 되돌리지 못함: %s" % short_list(failed)
        m = inject.message("gate_violation", (res.reason or "").split("] ", 1)[-1] + extra)
        res.reason = m or res.reason
    ctx.state["opaque_gate"] = gate
    ctx.state["write_paths_after"] = sorted(revert)
    return res, sorted(set(revert) - set(failed)), failed


def _after(reason):
    """미리 막는 문구를 실행 뒤 되돌림 문구로."""
    from vmc_harness.core import inject
    body = (reason or "").split("] ", 1)[-1]
    return inject.message("gate_violation", "실행 뒤 감지·되돌림 — " + body) or reason


def _copy(ctx, **over):
    from dataclasses import replace
    c = replace(ctx, **over)
    c.state = dict(ctx.state)
    return c
