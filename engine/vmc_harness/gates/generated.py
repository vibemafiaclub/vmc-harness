"""생성 게이트 로더 (L1 — spec 02 §생성 게이트, fail-open).

파일: `.harness/gates/<gate_id>.json`(메타) + `.harness/gates/<gate_id>.py`(본체 — gates/api.py만 사용)
메타 필드:
    gate_id, title, basis{type: explicit_request|machine_fact, quote, source{session,date,area} | command, value},
    trigger{events[], paths[] glob, sizes[] small|mid, change_kinds[] add|modify|delete},
    targets[] (게이트가 가리키는 파일 — 사라지면 자동 수정 또는 끔), cost{avg_ms, runs}, status trial|kept|reverted,
    enabled, disabled_reason, human_requested, human_criterion(사람을 부르는 게이트면 1|2|3), action_id, created_at
- 발동 조건에 안 걸리면 실행하지 않는다. 노출(ledger.exposure.record) = 조건(paths·change_kinds)이 해당한 때,
  조건 없는 게이트는 본체가 위반을 돌려준 때만
- 본체 오류·형식 위반은 막지 않고 통과(fail-open) + events·꼭 알릴 일(하네스 오류, 세션당 1회)
- 세션 시작: 대상 파일 확인(git 이름 변경·같은 이름 파일로 자동 수정, 못 찾으면 끄고 알림), 비용 이동 평균을 메타에 반영,
  기계 사실 규칙 재확인(rules.refresh_machine_facts)
"""

import ast
import os
import re
import time
from pathlib import Path

from vmc_harness.core import config, ledger, paths
from vmc_harness import KERNEL_VERSION
from vmc_harness.core.clock import now_iso
from vmc_harness.core.gitutil import git
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.gates import globs, state
from vmc_harness.gates.api import ALLOWED_IMPORTS, FORBIDDEN_NAMES, GateAPI
from vmc_harness.gates.common import _write_text, block, log_event, notify

NAME = "generated"
EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd")
SIZES = ("small", "mid")
CHANGE_KINDS = ("add", "modify", "delete")


class GateScriptError(ValueError):
    pass


def gates_dir(root):
    return paths.harness_dir(root) / "gates"


def load_all(root):
    d = gates_dir(root)
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        m = read_json(f, None)
        if isinstance(m, dict):
            m.setdefault("gate_id", f.stem)
            out.append(m)
    return out


def get(root, gate_id):
    return read_json(gates_dir(root) / ("%s.json" % gate_id), None)


def save(root, meta):
    write_json(gates_dir(root) / ("%s.json" % meta["gate_id"]), meta)


def active(meta):
    return meta.get("enabled", True) and meta.get("status") not in ("reverted", "disabled")


# ---- 본체 검사·실행 ----

def validate_script(text):
    """엔진 API만 쓰는지(import·금지 이름) 정적 검사. 어기면 GateScriptError."""
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise GateScriptError("게이트 본체 문법 오류: %s" % e)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_IMPORTS:
                    raise GateScriptError("게이트 본체가 엔진 API 밖 모듈을 import: %s" % a.name)
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "") not in ALLOWED_IMPORTS or node.level:
                raise GateScriptError("게이트 본체가 엔진 API 밖 모듈을 import: %s" % node.module)
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise GateScriptError("게이트 본체가 금지된 이름을 씀: %s" % node.id)
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise GateScriptError("게이트 본체가 내부 속성에 접근: %s" % node.attr)
    if not any(isinstance(n, ast.FunctionDef) and n.name == "check" for n in tree.body):
        raise GateScriptError("게이트 본체에 check(api) 없음")
    return tree


def _load_check(root, gate_id):
    p = gates_dir(root) / ("%s.py" % gate_id)
    text = p.read_text(encoding="utf-8")
    validate_script(text)
    import builtins
    safe_builtins = {k: getattr(builtins, k) for k in dir(builtins) if k not in FORBIDDEN_NAMES and not k.startswith("_")}
    safe_builtins["__import__"] = _guarded_import
    ns = {"__builtins__": safe_builtins, "__name__": "vmc_gate_%s" % re.sub(r"\W", "_", gate_id)}
    exec(compile(text, str(p), "exec"), ns)
    return ns["check"]


def _guarded_import(name, globals_=None, locals_=None, fromlist=(), level=0):
    if name not in ALLOWED_IMPORTS or level:
        raise ImportError(name)
    return __import__(name, globals_, locals_, fromlist, level)


# ---- 발동 조건 ----

def _change_kind(root, o):
    if o["op"] == "delete":
        return "delete"
    if o["op"] == "write" and not Path(o["abs"]).exists():
        return "add"
    return "modify"


def _stop_changes(root):
    rc, out, _ = git(root, "status", "--porcelain", "--untracked-files=all")
    res = {}
    if rc != 0:
        return res
    for line in out.splitlines():
        code, path = line[:2], line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ")[-1]
        if path.startswith(".harness/cache/"):
            continue
        res[path] = "delete" if "D" in code else ("add" if code in ("??", "A ") or "A" in code else "modify")
    return res


def trigger_match(meta, ctx, task):
    """발동하면 (paths, change_kinds) 아니면 None."""
    tr = meta.get("trigger") or {}
    evs = tr.get("events") or ["PreToolUse"]
    if ctx.event not in evs:
        return None
    sizes = tr.get("sizes")
    if sizes:
        size = (task or {}).get("size") or "mid"
        if size not in sizes:
            return None
    if ctx.event == "Stop":
        changes = _stop_changes(ctx.root)
    elif ctx.state.get("opaque_changes") is not None:
        changes = dict(ctx.state["opaque_changes"])   # 해석 못 한 셸 명령이 실제로 바꾼 파일(gates.opaque_watch)
    else:
        changes = {}
        for o in ctx.state.get("file_ops") or []:
            if o.get("rel"):
                changes[o["rel"]] = _change_kind(ctx.root, o)
    pats = tr.get("paths")
    kinds = tr.get("change_kinds")
    hit = {}
    for rel, k in changes.items():
        if pats and not globs.match_any(rel, pats):
            continue
        if kinds and k not in kinds:
            continue
        hit[rel] = k
    if (pats or kinds) and not hit:
        return None
    return hit


def run_gate(meta, ctx, task, hit):
    """(violation or None, ms). 예외는 호출자가 처리."""
    from vmc_harness.gates import meta as meta_gate
    proposed = {}
    if ctx.event == "PreToolUse":
        for rel in hit:
            v = meta_gate.simulate(ctx, rel)
            if v is not None:
                proposed[rel] = v
    api = GateAPI(ctx.root, ctx.event, paths=sorted(hit), all_paths=list(ctx.write_paths or hit),
                  tool_name=ctx.tool_name, command=ctx.command, proposed=proposed, change_kinds=hit,
                  size=(task or {}).get("size"))
    fn = _load_check(ctx.root, meta["gate_id"])
    t0 = time.time()
    res = fn(api)
    ms = (time.time() - t0) * 1000.0
    return (str(res) if res else None), ms


def _record_cost(root, gate_id, ms):
    p = paths.cache_dir(root) / "gates" / "cost.json"
    data = read_json(p, {}) or {}
    a = float(config.get("gates.cost_ema_alpha", root))
    c = data.get(gate_id) or {"avg_ms": ms, "runs": 0}
    c["avg_ms"] = ms if not c.get("runs") else (a * ms + (1 - a) * float(c["avg_ms"]))
    c["runs"] = int(c.get("runs") or 0) + 1
    data[gate_id] = c
    write_json(p, data)


def _exposure(root, meta):
    try:
        from vmc_harness.ledger import exposure
        exposure.record(str(root), action_id=meta.get("action_id"), gate_id=meta["gate_id"])
    except (ImportError, AttributeError):
        pass
    except Exception:
        pass


def _harness_error(root, session_id, meta, err):
    try:
        log_event(root, "gate_error", gate_id=meta.get("gate_id"), error=str(err)[:300])
        if state.noticed_once(root, session_id, "gate_error:%s" % meta.get("gate_id")):
            notify(root, "harness_error", "must", "'%s' 게이트 실행 오류 — 막지 않고 통과시킴" % (meta.get("title") or meta.get("gate_id")),
                        refs={"gate_id": meta.get("gate_id")})
    except Exception:
        pass


def path_trigger_globs(root):
    """경로형(trigger.paths가 있는) 활성 PreToolUse 게이트의 glob — 해석 못 한 셸 명령의 실행 뒤 대조 대상."""
    out = []
    for meta in load_all(root):
        tr = meta.get("trigger") or {}
        if active(meta) and tr.get("paths") and "PreToolUse" in (tr.get("events") or ["PreToolUse"]):
            out.extend(g for g in tr["paths"] if g not in out)
    return out


def evaluate(ctx, task=None, path_only=False):
    """모든 활성 생성 게이트 실행 → 첫 위반 HookResult 또는 None. path_only면 경로형 게이트만."""
    if task is None:
        task = state.current_task(ctx.root, ctx.session_id)
    for meta in load_all(ctx.root):
        if not active(meta) or (path_only and not (meta.get("trigger") or {}).get("paths")):
            continue
        try:
            hit = trigger_match(meta, ctx, task)
        except Exception as e:
            _harness_error(ctx.root, ctx.session_id, meta, e)
            continue
        if hit is None:
            continue
        # 노출 = 게이트 조건이 해당한 횟수(spec 04). 경로·변경 종류 조건이 있으면 그게 해당한 때,
        # 조건 없는 게이트는 본체가 위반을 찾은 때만 — 모든 도구 호출을 노출로 세지 않는다
        conditioned = bool((meta.get("trigger") or {}).get("paths") or (meta.get("trigger") or {}).get("change_kinds"))
        if conditioned:
            _exposure(ctx.root, meta)
        try:
            violation, ms = run_gate(meta, ctx, task, hit)
        except Exception as e:   # fail-open (01 P8)
            _harness_error(ctx.root, ctx.session_id, meta, e)
            continue
        if violation and not conditioned:
            _exposure(ctx.root, meta)
        _record_cost(ctx.root, meta["gate_id"], ms)
        ctx.state["generated_ms"] = float(ctx.state.get("generated_ms") or 0) + ms
        log_event(ctx.root, "gate_fire", gate_id=meta["gate_id"], event=ctx.event, blocked=bool(violation))
        log_event(ctx.root, "gate_time", gate_id=meta["gate_id"], sec=round(ms / 1000.0, 4))
        if violation:
            if state.consume_bypass(ctx.root, ctx, NAME, meta["gate_id"]):
                continue
            title = meta.get("title") or meta["gate_id"]
            r = block("게이트 '%s' 위반: %s" % (title, violation), "게이트 '%s' 위반" % title)
            ctx.state["blocked_gate_id"] = meta["gate_id"]
            ctx.state["blocked_hit"] = sorted(hit)
            state.record_block(ctx.root, ctx, NAME, r.reason, gate_id=meta["gate_id"])
            log_event(ctx.root, "gate_block", gate=NAME, gate_id=meta["gate_id"], event=ctx.event, tool=ctx.tool_name)
            return r
    return None


# ---- 훅 핸들러 (contract.HANDLER_MODULES: safety=False) ----

def on_pre_tool(ctx):
    return evaluate(ctx)


def on_post_tool(ctx):
    return evaluate(ctx)


def on_stop(ctx):
    return evaluate(ctx)


def on_user_prompt(ctx):
    return evaluate(ctx)


def enable(root, gate_id):
    """[켜기] — 꺼진 게이트를 다시 켠다(사람 행동)."""
    return set_enabled(root, gate_id, True, by_human=True)


def keep(root, gate_id):
    """[유지] — 지금 상태 그대로(기본값). 기록만 남긴다."""
    meta = get(root, gate_id)
    if not meta:
        raise KeyError(gate_id)
    log_event(root, "gate_keep", gate_id=gate_id)
    return meta


def on_session_start(ctx):
    try:
        check_targets(ctx.root)
    except Exception as e:
        log_event(ctx.root, "gate_error", gate_id="-", error="check_targets: %s" % str(e)[:200])
    try:
        sync_costs(ctx.root)
    except Exception:
        pass
    try:
        # 팀원 PC 등 이 PC에서 Codex 훅 신뢰 등록이 없으면 백그라운드로 한 번(세션 시작을 기다리게 하지 않는다)
        from vmc_harness.hooks import install
        if install.trust_needed(ctx.root):
            import sys
            from vmc_harness.core.proc import spawn_detached
            spawn_detached([sys.executable, "-m", "vmc_harness", "gate", "codex-trust", "--root", str(ctx.root)],
                           cwd=str(ctx.root))
    except Exception:
        pass
    try:
        from vmc_harness.gates import rules
        rules.refresh_machine_facts(ctx.root)
    except Exception as e:
        log_event(ctx.root, "gate_error", gate_id="rules", error=str(e)[:200])
    return None


# ---- 대상 파일 추적 ----

def _git_rename(root, old):
    for args in (["diff", "-M", "--name-status", "--diff-filter=R", "HEAD"],
                 ["diff", "-M", "--name-status", "--diff-filter=R", "--cached"],
                 ["log", "-M", "--diff-filter=R", "--name-status", "--format=", "-n", "500"]):
        rc, out, _ = git(root, *args)
        if rc != 0:
            continue
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].startswith("R") and globs.norm(parts[1]) == old:
                if (Path(root) / parts[2]).exists():
                    return globs.norm(parts[2])
    return None


def _same_name(root, old):
    rc, out, _ = git(root, "ls-files", "--cached", "--others", "--exclude-standard")
    if rc != 0:
        return None
    base = os.path.basename(old)
    cands = [l.strip() for l in out.splitlines() if l.strip() and os.path.basename(l.strip()) == base]
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        try:
            from vmc_harness.bootstrap import structmap
            om = structmap.module_of(str(root), old)
            same = [c for c in cands if om and structmap.module_of(str(root), c) == om]
            if len(same) == 1:
                return same[0]
        except Exception:
            pass
    return None


def find_new_location(root, old):
    return _git_rename(root, old) or _same_name(root, old)


def check_targets(root):
    """사라진 대상 파일 처리 — [(gate_id, 'moved'|'disabled', new)]."""
    out = []
    for meta in load_all(root):
        if not active(meta):
            continue
        tg = meta.get("targets") or []
        missing = [t for t in tg if not (Path(root) / t).exists()]
        if not missing:
            continue
        moved, lost = {}, []
        for t in missing:
            n = find_new_location(root, t)
            if n:
                moved[t] = n
            else:
                lost.append(t)
        title = meta.get("title") or meta["gate_id"]
        if lost:
            meta["enabled"] = False
            meta["disabled_reason"] = "target_missing"
            meta["disabled_at"] = now_iso()
            meta["missing_targets"] = lost
            save(root, meta)
            buttons = ["켜기", "유지"] if meta.get("human_requested") else ["자세히", "알림 끄기"]
            try:
                notify(root, "gate_disabled", "must", "'%s' 게이트가 가리키던 파일이 없어져서 껐어요" % title,
                            area=os.path.dirname(lost[0]) or None, refs={"gate_id": meta["gate_id"]}, buttons=buttons)
            except Exception:
                pass
            log_event(root, "gate_disabled", gate_id=meta["gate_id"], reason="target_missing")
            out.append((meta["gate_id"], "disabled", None))
            continue
        meta["targets"] = [moved.get(t, t) for t in tg]
        tr = meta.get("trigger") or {}
        if tr.get("paths"):
            newp = []
            for p in tr["paths"]:
                for o, n in moved.items():
                    if p == o:
                        p = n
                    elif p.startswith(os.path.dirname(o) + "/") and os.path.dirname(o) != os.path.dirname(n) and os.path.dirname(o):
                        p = os.path.dirname(n) + p[len(os.path.dirname(o)):]
                newp.append(p)
            tr["paths"] = newp
            meta["trigger"] = tr
        meta["retargeted_at"] = now_iso()
        save(root, meta)
        log_event(root, "gate_retarget", gate_id=meta["gate_id"], moved=moved)
        try:
            notify(root, "gate_retarget", "flow", "'%s' 게이트 대상 파일이 옮겨져 새 위치로 고쳤어요" % title,
                        refs={"gate_id": meta["gate_id"]})
        except Exception:
            pass
        out.append((meta["gate_id"], "moved", moved))
    return out


def sync_costs(root):
    data = read_json(paths.cache_dir(root) / "gates" / "cost.json", {}) or {}
    for meta in load_all(root):
        c = data.get(meta["gate_id"])
        if not c:
            continue
        cur = meta.get("cost") or {}
        new = {"avg_ms": round(float(c["avg_ms"]), 1), "runs": int(cur.get("runs") or 0) + int(c.get("runs") or 0)}
        if new != cur:
            meta["cost"] = new
            save(root, meta)
    p = paths.cache_dir(root) / "gates" / "cost.json"
    if p.exists():
        for k in list(data):
            data[k]["runs"] = 0
        write_json(p, data)


# ---- 만들기·상태 바꾸기 (명령 `gate`·진단 [추가]가 부른다) ----

def _new_gate_id(title):
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return "g-%s%s" % ((slug + "-") if slug else "", os.urandom(3).hex())


def budget_status(root, extra_ms=0.0):
    total = sum(float((m.get("cost") or {}).get("avg_ms") or 0) for m in load_all(root) if active(m)) + extra_ms
    limit = float(config.get("budget.gate_time_per_task_s", root)) * 1000.0
    return total, limit, total > limit


def create(root, title, basis, trigger, script_text, targets=None, human_criterion=None, calls_human=False,
           hypothesis=None, gate_id=None, measure=True, requires=None):
    """게이트 생성. 근거 타입은 사람 명시 요청(원문+출처)·기계 사실(읽은 명령)·진단 자동 반영(기계 검사 전부 통과 — spec 07)만
    (01 P3 — core.ledger 메타 게이트가 강제). 반환: {'meta','cost_line','over_budget'}"""
    btype = (basis or {}).get("type")
    if btype not in ("explicit_request", "machine_fact", "diagnosis_auto"):
        raise ledger.MetaGateError("게이트 근거는 사람 명시 요청·기계 사실·진단 자동 반영뿐")
    if btype == "explicit_request" and not (basis.get("source") or {}).get("date"):
        raise ledger.MetaGateError("명시 요청 근거에 출처(세션·날짜·작업 영역) 없음")
    tr = dict(trigger or {})
    for k, allowed in (("events", EVENTS), ("sizes", SIZES), ("change_kinds", CHANGE_KINDS)):
        bad = [x for x in tr.get(k) or [] if x not in allowed]
        if bad:
            raise ledger.MetaGateError("발동 조건 %s 값 오류: %s" % (k, bad))
    if not tr.get("events"):
        tr["events"] = ["PreToolUse"]
    validate_script(script_text)
    gid = gate_id or _new_gate_id(title)
    if (gates_dir(root) / ("%s.json" % gid)).exists():
        raise ledger.MetaGateError("같은 gate_id가 이미 있음: %s" % gid)
    action = ledger.create_action(
        root, basis=dict(basis, ref=gid), layer="gate",
        hypothesis=hypothesis or ("'%s' 게이트로 같은 실수가 다시 나지 않는다" % title),
        human_requested=(btype == "explicit_request"), human_criterion=human_criterion,
        calls_human=calls_human, target={"gate_id": gid})
    meta = {"gate_id": gid, "title": title, "basis": basis, "trigger": tr, "targets": list(targets or []),
            "cost": {"avg_ms": 0.0, "runs": 0}, "status": "trial", "enabled": True,
            "human_requested": btype == "explicit_request", "human_criterion": human_criterion,
            "action_id": action["action_id"], "created_at": now_iso(), "kernel_version": KERNEL_VERSION}
    if requires:  # 전제조건(bootstrap.requires) — 꺼진 채 만들고 조건이 맞으면 밤 루프가 켠다
        meta.update(enabled=False, disabled_reason="requires", requires=list(requires))
    paths.ensure(gates_dir(root))
    _write_text(gates_dir(root) / ("%s.py" % gid), script_text)
    save(root, meta)
    ms = measure_cost(root, meta) if measure else 0.0
    if ms:
        meta["cost"] = {"avg_ms": round(ms, 1), "runs": 1}
        save(root, meta)
    total, limit, over = budget_status(root)
    where = ", ".join(tr.get("paths") or []) or "모든"
    line = "이거 넣으면 %s 작업마다 +%.1f초예요. 넣을게요." % (where, ms / 1000.0)
    if over:
        line += " (게이트 시간 예산 넘음 — 편의 장치부터 빠집니다)"
        try:
            notify(root, "budget_over", "must", "게이트 시간 예산 넘음: '%s' 추가" % title, refs={"gate_id": gid})
        except Exception:
            pass
    log_event(root, "gate_created", gate_id=gid, basis_type=btype, ms=round(ms, 1), over_budget=over)
    return {"meta": meta, "cost_line": line, "over_budget": over}


def measure_cost(root, meta):
    """지금 작업 트리의 바뀐 파일(없으면 대상 파일)로 한 번 돌려 실행 시간(ms)을 잰다. 실패해도 0."""
    try:
        changes = _stop_changes(root)
        pats = (meta.get("trigger") or {}).get("paths")
        hit = {r: k for r, k in changes.items() if not pats or globs.match_any(r, pats)}
        if not hit:
            hit = {t: "modify" for t in meta.get("targets") or []}
        api = GateAPI(root, "PreToolUse", paths=sorted(hit), change_kinds=hit)
        fn = _load_check(root, meta["gate_id"])
        t0 = time.time()
        fn(api)
        return (time.time() - t0) * 1000.0
    except Exception:
        return 0.0


def set_enabled(root, gate_id, enabled, by_human=True):
    meta = get(root, gate_id)
    if not meta:
        raise KeyError(gate_id)
    meta["enabled"] = bool(enabled)
    if enabled:
        meta.pop("disabled_reason", None)
        meta.pop("missing_targets", None)
    else:
        meta["disabled_reason"] = meta.get("disabled_reason") or ("human" if by_human else "harness")
    save(root, meta)
    log_event(root, "gate_toggle", gate_id=gate_id, enabled=bool(enabled), by_human=by_human)
    return meta


def delete(root, gate_id):
    """사람의 "그 게이트 지워"·[삭제] = finding(button=delete) → 끄고 상태 reverted(조치 원복은 ledger 영역)."""
    meta = get(root, gate_id)
    if not meta:
        raise KeyError(gate_id)
    f = ledger.create_finding(root, "button", {"gate_id": gate_id, "action_id": meta.get("action_id")}, button="delete")
    reverted = False
    if meta.get("action_id"):
        try:
            from vmc_harness.ledger import revert
            revert.revert_action(str(root), meta["action_id"], "사람이 게이트 삭제", by_human=True)
            reverted = True
        except (ImportError, AttributeError):
            pass
        except Exception:
            pass
        if not reverted:
            try:
                ledger.update_action(root, meta["action_id"], status="reverted")
            except Exception:
                pass
    meta = get(root, gate_id) or meta
    meta["enabled"] = False
    meta["status"] = "reverted"
    meta["disabled_reason"] = "human_delete"
    save(root, meta)
    return {"finding": f, "meta": meta}
