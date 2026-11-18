"""`vmc-harness why <task|file> [--without <id>]` (spec 04 §의도와 다른 지침 막기).

그 작업에 영향을 준 것을 3분 안에 읽히게: 로드된 규칙(원문·출처 — events의 rules_loaded로), 넣어 준 카드,
발동한 게이트, PM 의견, 그 영역의 최근 자동 변경·요청 밖 변경(git trailer).
후보마다 id가 붙는다 → 사람이 "이거야" 지목 = `finding --blame <id>` → finding(blame) + 즉시 원복(blame()).
`--without`: 별도 git worktree에서 그 지침 하나만 끄고 같은 작업 지시를 `claude -p`로 다시 돌려 diff를 비교한다
(비용이 커서 사람이 원할 때만, 확정 도구가 아니라 판단 근거).
"""

import datetime as _dt
import os
import re
import shutil
from pathlib import Path

from vmc_harness.core import config, events, paths
from vmc_harness.core import ledger as core_ledger
from vmc_harness.core.clock import now
from vmc_harness.core.gitutil import git
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.ledger.exposure import in_area
from vmc_harness.ledger.revert import commits_with_trailer

TRAILER_RE = re.compile(r"^(Harness-(?:Task|Action|OutOfScope)):\s*(.+?)\s*$", re.M)


# ---- 재료 ----

def find_task(root, task_id):
    for t in reversed(core_ledger.read_tasks(root)):
        if t.get("task_id") == task_id:
            return t
    try:
        from vmc_harness.task import state
        return state.by_id(root, task_id)
    except (ImportError, AttributeError):
        return None


def load_rules(root):
    data = read_json(paths.harness_dir(root) / "rules.json", None)
    rules = (data.get("rules") if isinstance(data, dict) else data) or []
    return [r for r in rules if isinstance(r, dict)]


def _rule_id(r):
    return r.get("id") or r.get("rule_id")


def _rule_text(r):
    """gates.rules 형식: 명시 요청은 basis.quote(원문), 기계 사실은 text."""
    t = r.get("text") or (r.get("basis") or {}).get("quote") or r.get("quote") or r.get("rule") or ""
    return t.replace("{value}", str(r.get("value", ""))) if "{value}" in t else t


def rule_active(r):
    return r.get("enabled", True) is not False and r.get("status") not in ("reverted", "disabled")


def rules_loaded(root, session_id, evs):
    """세션에 로드된 규칙 — rules_loaded 이벤트의 rule_ids·hashes(규칙 원문 sha256)·block_hash로 찾는다."""
    rules = load_rules(root)
    ids, hashes, block = set(), set(), False
    for e in evs:
        if e.get("kind") != "rules_loaded" or (session_id and e.get("session_id") not in (None, session_id)):
            continue
        ids.update(e.get("rule_ids") or [])
        hashes.update(e.get("hashes") or [])
        if e.get("hash"):
            hashes.add(e["hash"])
        if e.get("block_hash"):
            block = True
    out = []
    for r in rules:
        if _rule_id(r) in ids or core_ledger.sha(_rule_text(r)) in hashes or (block and rule_active(r)):
            out.append(r)
    return out


def rules_active_at(root, at):
    """rules_loaded 기록이 없는 세션의 대체: 그 시각에 켜져 있던 규칙(만든 시각 ≤ at)."""
    return [r for r in load_rules(root) if rule_active(r) and (not at or (r.get("created_at") or "") <= at)]


def _commit_rows(root, args):
    rc, out, _ = git(root, "log", "--format=%H%x00%ad%x00%s%x00%B%x1e", "--date=short", *args)
    rows = []
    if rc != 0:
        return rows
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if chunk.count("\x00") < 3:
            continue
        sha, date, subj, body = chunk.split("\x00", 3)
        tr = {}
        for k, v in TRAILER_RE.findall(body):
            tr.setdefault(k, v)
        rows.append({"commit": sha.strip()[:12], "full": sha.strip(), "date": date, "subject": subj, "trailers": tr})
    return rows


def _files_of(root, sha):
    rc, out, _ = git(root, "show", "--name-only", "--format=", sha)
    return [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []


def area_changes(root, area_paths):
    """그 영역의 최근 자동 변경(Harness-Action)·요청 밖 변경(Harness-OutOfScope)."""
    if not area_paths:
        return []
    since = (now() - _dt.timedelta(days=config.get("why.recent_days", root))).strftime("%Y-%m-%d")
    rows = _commit_rows(root, ["--since", since, "-n", str(config.get("why.max_commits", root)), "--"] + sorted(area_paths))
    out = []
    for r in rows:
        tr = r["trailers"]
        if "Harness-Action" in tr or "Harness-OutOfScope" in tr:
            kind = "자동 변경" if "Harness-Action" in tr else "요청 밖 변경"
            out.append({"id": r["commit"], "kind": "commit", "label": kind, "date": r["date"], "title": r["subject"],
                        "action_id": tr.get("Harness-Action"), "task_id": tr.get("Harness-Task"),
                        "out_of_scope": tr.get("Harness-OutOfScope")})
    return out


def _gate_title(root, gid):
    meta = read_json(paths.harness_dir(root) / "gates" / ("%s.json" % gid), None) or {}
    return meta.get("title") or meta.get("name") or gid, meta


GATE_KINDS = {"gate_fire": "fired", "gate_block": "blocked", "proceed_anyway": "proceed"}


def _gate_event_of_task(e, task_id, sid):
    """작업 번호가 적힌 게이트 이벤트는 그 번호로, 없으면(선언 전·옛 기록) 세션으로 고른다."""
    if e.get("kind") not in GATE_KINDS or not e.get("gate_id"):
        return False
    if e.get("task_id"):
        return e["task_id"] == task_id
    return bool(sid) and e.get("session_id") == sid


def gate_candidates(root, gate_events):
    """게이트마다 막은 횟수와 가장 최근 사유·파일 — 횟수만으로는 무엇을 막았는지 알 수 없었다(현장 결함 C18)."""
    gates = {}
    for e in gate_events:
        g = gates.setdefault(e["gate_id"], {"fired": 0, "blocked": 0, "proceed": 0, "reason": None, "paths": [],
                                            "task": None})
        g[GATE_KINDS[e["kind"]]] += 1
        if e["kind"] == "gate_block":
            g["reason"] = e.get("reason") or g["reason"]
            g["task"] = e.get("task_id") or g["task"]
        for rel in e.get("paths") or []:
            if isinstance(rel, str) and rel not in g["paths"]:
                g["paths"].append(rel)
    cap = int(config.get("gates.reason_max_files", root))
    out = []
    for gid, g in gates.items():
        title, meta = _gate_title(root, gid)
        counts = (["발동 %d" % g["fired"]] if g["fired"] else []) + ["막음 %d" % g["blocked"], "그냥 진행 %d" % g["proceed"]]
        detail = " · ".join(counts)
        if g["reason"]:
            detail += " — 최근 사유: %s" % g["reason"]
        if g["paths"] and not all(rel in (g["reason"] or "") for rel in g["paths"][:cap]):
            more = len(g["paths"]) - cap
            detail += " (파일: %s%s)" % (", ".join(g["paths"][:cap]), " 외 %d" % more if more > 0 else "")
        out.append({"id": gid, "kind": "gate", "title": title, "detail": detail, "task_id": g["task"],
                    "basis": (meta.get("basis") or {}).get("type")})
    return out


def collect_task(root, task_id, evs=None):
    evs = events.read(root) if evs is None else evs
    task = find_task(root, task_id) or {}
    sid = task.get("session_id")
    mine = [e for e in evs if e.get("task_id") == task_id or (sid and e.get("session_id") == sid)]
    cands = []
    loaded = rules_loaded(root, sid, mine)
    if not any(e.get("kind") == "rules_loaded" for e in mine):
        loaded = rules_active_at(root, task.get("started_at"))
    for r in loaded:
        src = r.get("source") or (r.get("basis") or {}).get("source") or {}
        if not isinstance(src, dict):   # 기계 사실 규칙은 출처가 문자열("bootstrap") — gates.rules 형식
            src = {"area": str(src)}
        cands.append({"id": _rule_id(r), "kind": "rule", "title": _rule_text(r),
                      "source": ("%s %s" % (src.get("date") or "", src.get("session") or src.get("area") or "")).strip(),
                      "action_id": r.get("action_id"), "status": r.get("status")})
    card_ids = []
    for e in mine:
        if e.get("kind") == "card_search":
            card_ids += [c for c in (e.get("ids") or e.get("cards") or []) if c and c not in card_ids]
        elif e.get("kind") == "exposure" and e.get("card") and e["card"] not in card_ids:
            card_ids.append(e["card"])
    if card_ids:
        try:
            from vmc_harness.brain import store
            for cid in card_ids:
                c = store.get_card(cid) or {"id": cid, "title": "(지워진 카드)"}
                cands.append({"id": cid, "kind": "card", "title": c.get("title"), "detail": c.get("judgment"),
                              "status": c.get("status")})
        except ImportError:
            pass
    cands += gate_candidates(root, [e for e in evs if _gate_event_of_task(e, task_id, sid)])
    pm = task.get("pm") or {}
    if pm.get("called") and (pm.get("opinion") or pm.get("wish")):
        cands.append({"id": "pm:%s" % task_id, "kind": "pm", "title": "PM 의견: %s" % (pm.get("opinion") or ""),
                      "detail": pm.get("wish")})
    commits = commits_with_trailer(root, "Harness-Task", task_id)
    files = set()
    for c in commits:
        files.update(_files_of(root, c))
    scope = set((task.get("declare") or {}).get("scope") or [])
    area = sorted(files | scope)
    changes = [c for c in area_changes(root, area) if c.get("task_id") != task_id or c["label"] == "요청 밖 변경"]
    cands += changes
    for a in core_ledger.list_actions(root):
        t = (a or {}).get("target") or {}
        if a and a.get("status") in ("trial", "extended", "kept") and t.get("area") and any(in_area(f, t["area"]) for f in area):
            cands.append({"id": a["action_id"], "kind": "action", "title": a.get("hypothesis"), "status": a.get("status")})
    return {"target": task_id, "task": {"task_id": task_id, "intent": (task.get("declare") or {}).get("intent"),
                                        "started_at": task.get("started_at"), "session_id": sid,
                                        "out_of_scope": task.get("out_of_scope") or []},
            "commits": [c[:12] for c in commits], "area": area, "candidates": cands}


def collect_file(root, rel):
    rel = rel.replace("\\", "/")
    rows = _commit_rows(root, ["-n", str(config.get("why.max_commits", root)), "--", rel])
    tasks = []
    for r in rows:
        t = r["trailers"].get("Harness-Task")
        if t and t not in tasks:
            tasks.append(t)
    evs = events.read(root)
    cands, seen = [], set()
    for tid in tasks[:3]:
        for c in collect_task(root, tid, evs)["candidates"]:
            key = (c["kind"], c["id"])
            if key not in seen:
                seen.add(key)
                c = dict(c, via_task=tid)
                cands.append(c)
    # 막힌 편집은 커밋으로 남지 않는다 — 그 파일을 막은 게이트 이벤트를 직접 찾는다
    hits = [e for e in evs if e.get("kind") in GATE_KINDS and e.get("gate_id") and rel in (e.get("paths") or [])]
    for c in gate_candidates(root, hits):
        if ("gate", c["id"]) not in seen:
            seen.add(("gate", c["id"]))
            cands.append(dict(c, via_task=c.get("task_id")) if c.get("task_id") else c)
    for c in area_changes(root, [rel]):
        if ("commit", c["id"]) not in seen:
            seen.add(("commit", c["id"]))
            cands.append(c)
    stale = None
    try:
        from vmc_harness.ledger import stale_docs
        stale = stale_docs.is_stale(root, rel)
    except Exception:
        pass
    return {"target": rel, "tasks": tasks[:3], "candidates": cands, "stale_doc": stale}


def collect(root, target):
    if re.match(r"^T-\d{6}-\w+$", target):
        return collect_task(root, target)
    return collect_file(root, target)


# ---- 표시 ----

KIND_LABEL = {"rule": "규칙", "card": "카드", "gate": "게이트", "pm": "PM", "commit": "변경", "action": "하네스 조치"}


def plain_text(res):
    lines = ["%s에 영향을 준 것" % res["target"]]
    t = res.get("task") or {}
    if t.get("intent"):
        lines.append("작업: %s" % t["intent"])
    if not res["candidates"]:
        lines.append("하네스가 넣은 규칙·카드·게이트·자동 변경 없음")
    for c in res["candidates"]:
        label = c.get("label") or KIND_LABEL.get(c["kind"], c["kind"])
        extra = " — %s" % c["detail"] if c.get("detail") else ""
        src = " (%s)" % c["source"] if c.get("source") else ""
        lines.append("[%s] %s: %s%s%s" % (c["id"], label, c.get("title") or "", extra, src))
    if res.get("stale_doc"):
        lines.append("이 문서는 낡았을 수 있음(가리키는 경로·심볼이 코드에 없음)")
    lines.append("원인으로 지목: vmc-harness finding --blame <id>")
    return "\n".join(lines)


def render_text(root, res):
    try:
        from vmc_harness.render import engine
        out = engine.render(root, None, res, draft=plain_text(res), kind="why")
        if out and out.get("text"):
            return out["text"]
    except Exception:
        pass
    return plain_text(res)


# ---- 원인 지목 ----

def _open_actions_for(root, key, value):
    return [a for a in core_ledger.list_actions(root)
            if a and (a.get("target") or {}).get(key) == value and a.get("status") in ("trial", "extended", "kept")]


def blame(root, ident):
    """사람이 why 후보 중 "이거야" 지목 → finding(blame) + 즉시 원복."""
    from vmc_harness.ledger import revert
    reason = "사람이 원인으로 지목"
    if ident.startswith("A-"):
        core_ledger.create_finding(root, "blame", {"action_id": ident})
        return {"finding": "blame", "action": revert.revert_action(root, ident, reason, by_human=True, make_finding=False)}
    if ident.startswith("T-"):
        shas = commits_with_trailer(root, "Harness-Task", ident)
        if not shas:
            raise KeyError(ident)
        core_ledger.create_finding(root, "blame", {"commit": shas[0], "task_id": ident})
        return {"finding": "blame", "task": revert.revert_task(root, ident, reason, by_human=False)}
    if ident.startswith("R-") or any(_rule_id(r) == ident for r in load_rules(root)):
        rule = next((r for r in load_rules(root) if _rule_id(r) == ident), None)
        if not rule or not rule.get("action_id"):
            raise ValueError("하네스 조치로 넣은 규칙이 아님(사람이 쓴 규칙은 원복 대상이 아님): %s" % ident)
        core_ledger.create_finding(root, "blame", {"action_id": rule["action_id"], "rule_id": ident})
        return {"finding": "blame", "action": revert.revert_action(root, rule["action_id"], reason, by_human=True,
                                                                    make_finding=False)}
    if ident.startswith("K-"):
        core_ledger.create_finding(root, "blame", {"card": ident})
        acts = _open_actions_for(root, "card", ident)
        for a in acts:
            revert.revert_action(root, a["action_id"], reason, by_human=True, make_finding=False)
        if not acts:
            from vmc_harness.brain import store
            store.set_status(ident, "disabled", reason=reason)
        return {"finding": "blame", "card": ident, "actions": [a["action_id"] for a in acts]}
    if ident.startswith("pm:"):
        raise ValueError("PM 의견은 원복할 조치가 아님")
    if revert.SHA_RE.match(ident) and git(root, "cat-file", "-e", ident + "^{commit}")[0] == 0:
        res = revert.revert_commit(root, ident, reason, by_human=True, finding_kind="blame")
        for a in core_ledger.list_actions(root):
            if a and a.get("status") != "reverted" and any(c.startswith(ident) or ident.startswith(c) for c in a.get("commits") or []):
                core_ledger.update_action(root, a["action_id"], status="reverted", revert_reason=reason, reverted_by="human")
        return {"finding": "blame", "commit": res}
    # 게이트 id
    core_ledger.create_finding(root, "blame", {"gate_id": ident})
    acts = _open_actions_for(root, "gate_id", ident)
    for a in acts:
        revert.revert_action(root, a["action_id"], reason, by_human=True, make_finding=False)
    return {"finding": "blame", "gate_id": ident, "actions": [a["action_id"] for a in acts]}


# ---- --without ----

def _instruction(root, task):
    sid = task.get("session_id")
    if sid:
        try:
            from vmc_harness import sessions
            for agent, p in sessions.find(root, limit=None):
                if sid in p.stem:
                    s = sessions.parse(p, agent)
                    if s.human_prompts:
                        return s.human_prompts[0].get("text")
        except (ImportError, AttributeError, OSError):
            pass
    return (task.get("declare") or {}).get("intent")


def _disable_in_tree(tree, without):
    """worktree 사본에서만 그 지침을 끈다(고객 원본 파일은 건드리지 않는다)."""
    env = {"VMC_HARNESS_WITHOUT": without}
    h = Path(tree) / ".harness"
    rp = h / "rules.json"
    data = read_json(rp, None)
    if data is not None:
        rules = (data.get("rules") if isinstance(data, dict) else data) or []
        for r in rules:
            if isinstance(r, dict) and _rule_id(r) == without:
                r["status"] = "disabled"
                r["enabled"] = False   # gates.rules 형식(렌더가 보는 칸)
                text = _rule_text(r)
                for name in ("AGENTS.md", "CLAUDE.md"):
                    f = Path(tree) / name
                    if text and f.is_file():
                        body = f.read_text(encoding="utf-8")
                        f.write_text("\n".join(l for l in body.splitlines() if text not in l) + "\n", encoding="utf-8")
        write_json(rp, data)
    gp = h / "gates" / ("%s.json" % without)
    if gp.is_file():
        meta = read_json(gp, {}) or {}
        meta["status"] = "disabled"
        write_json(gp, meta)
    write_json(h / "cache" / "without.json", {"without": [without]})
    return env


def _diff_stat(root, args):
    rc, out, _ = git(root, *args)
    return out.strip() if rc == 0 else ""


def run_without(root, task_id, without, timeout=None):
    task = find_task(root, task_id)
    if not task:
        raise KeyError(task_id)
    instruction = _instruction(root, task)
    if not instruction:
        raise ValueError("작업 지시 원문을 찾을 수 없음")
    commits = commits_with_trailer(root, "Harness-Task", task_id)
    if commits:
        oldest = commits[-1]
        base = git(root, "rev-parse", oldest + "^")[1].strip() or oldest
        actual = _diff_stat(root, ["diff", "--stat", base, commits[0]])
    else:
        base = git(root, "rev-parse", "HEAD")[1].strip()
        actual = ""
    tree = paths.cache_dir(root) / "why-without" / ("%s-%s" % (task_id, re.sub(r"[^\w.-]", "_", without)))
    if tree.exists():
        git(root, "worktree", "remove", "--force", str(tree))
        shutil.rmtree(str(tree), ignore_errors=True)
    tree.parent.mkdir(parents=True, exist_ok=True)
    rc, _, err = git(root, "worktree", "add", "--detach", str(tree), base, timeout=120)
    if rc != 0:
        raise RuntimeError("worktree 생성 실패: %s" % err.strip()[-200:])
    try:
        extra_env = _disable_in_tree(tree, without)
        from vmc_harness.core.proc import run
        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError("claude 없음")
        env = dict(os.environ, **extra_env)
        env["VMC_HARNESS_NESTED"] = "1"  # 재실행 기록이 장부·노출을 오염시키지 않게
        args = [exe, "-p", "--output-format", "json", "--no-session-persistence",
                "--permission-mode", "acceptEdits", "--allowedTools", "Read,Edit,Write,Glob,Grep"]
        rc, out, err = run(args, cwd=str(tree), input_text=instruction, env=env,
                           timeout=timeout or config.get("why.without_timeout_s", root))
        git(tree, "add", "-A")
        rerun = _diff_stat(tree, ["diff", "--cached", "--stat"])
        full = _diff_stat(tree, ["diff", "--cached"])
        report = paths.cache_dir(root) / "reports" / ("why-without-%s-%s.diff" % (task_id, re.sub(r"[^\w.-]", "_", without)))
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(full + "\n", encoding="utf-8")
        return {"task_id": task_id, "without": without, "base": base[:12], "ran": rc == 0,
                "timeout": rc == -9, "actual_stat": actual, "rerun_stat": rerun, "diff_file": str(report)}
    finally:
        git(root, "worktree", "remove", "--force", str(tree))
        shutil.rmtree(str(tree), ignore_errors=True)
        git(root, "worktree", "prune")
