"""`vmc-harness init` (spec 07) — 프로젝트 부모 폴더에서 프로젝트당 1회. 이미 있으면 갱신.

설치 프로그램의 실행 순서(모델에게 주는 절차가 아니다):
 1 project_id·`.harness/config.json`(profile 기본 standard, source_roots 자동)·`.harness/.gitignore`(cache/, solo는 ledger/·lexicon.json도)
 2 훅·MCP 등록(gates·brain 영역, Codex 훅 신뢰 포함), commit hook(task 영역), QA 서브에이전트(qa 영역), mod 패널(render 영역)
 3 Claude Code cleanupPeriodDays(사용자 `~/.claude/settings.json` — 더 크면 그대로, 바꾸면 한 줄 알림)
 4 OS 스케줄러(loop 영역)
 5 부트스트랩: 기계 사실·구조 지도 → 규칙 블록(AGENTS.md 표시 블록, CLAUDE.md 없으면 `@AGENTS.md`) → 기준선
   → 학습 후보는 분리된 백그라운드(`learn-bg`, 세션당 약 22초라 기다리지 않는다 — 끝나면 notices에 결과)
 6 시니어식 진단 → 진단 리포트 HTML·PDF
`--dry-system`: 이 PC 사용자 설정(~/.claude/settings.json·플러그인, OS 스케줄러, ~/.codex·Codex 신뢰)을 바꾸지 않는다(리허설·테스트용).
"""

import json
import os
import platform
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from vmc_harness.bootstrap import util
from vmc_harness.core import config, paths
from vmc_harness.core.clock import now_iso
from vmc_harness.core.ids import new_uuid
from vmc_harness.core.jsonio import read_json, write_json

CLAUDE_IMPORT_NOTE = "CLAUDE.md가 AGENTS.md를 불러오지 않아 Claude Code가 하네스 규칙을 못 읽는 구조였어요 — CLAUDE.md 끝에 `@AGENTS.md` 한 줄을 덧붙였어요(기존 내용·줄끝은 그대로)"


def claude_user_settings():
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home() / ".claude") / "settings.json"


def ensure_cleanup_period(root, dry=False):
    """(바꿨나, 한 줄 알림). 기존 값이 더 크면 그대로."""
    want = config.get("claude.cleanup_period_days", root)
    p = claude_user_settings()
    try:
        data = read_json(p, {}) or {}
    except ValueError:
        return False, "Claude Code 설정 파일을 읽지 못해 세션 보관 기간을 그대로 뒀어요(%s)" % p
    cur = data.get("cleanupPeriodDays")
    if isinstance(cur, (int, float)) and cur >= want:
        return False, None
    if dry:
        return False, "(--dry-system) Claude Code 세션 보관 기간을 %s일 → %d일로 늘릴 예정이었어요" % (cur or 30, want)
    data["cleanupPeriodDays"] = want
    write_json(p, data)
    return True, "Claude Code 세션 기록 보관 기간을 %s일 → %d일로 늘렸어요(기본 30일이면 하네스가 배울 기록이 지워져요)" % (cur or 30, want)


@contextmanager
def _sandbox_codex_home(dry, root):
    """--dry-system일 때 Codex 사용자 설정(~/.codex)을 건드리지 않게 임시 폴더로 돌린다."""
    if not dry:
        yield
        return
    old = os.environ.get("CODEX_HOME")
    from vmc_harness.core.project import local_dir
    os.environ["CODEX_HOME"] = str(paths.ensure(local_dir(root) / "dry-codex-home"))
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = old


def write_config(root, profile=None, source_roots=None):
    from vmc_harness.bootstrap import facts as bfacts
    hd = paths.ensure(paths.harness_dir(root))
    cp = hd / "config.json"
    cfg = read_json(cp, {}) or {}
    created = not cfg.get("project_id")
    cfg.setdefault("project_id", new_uuid())
    if profile:
        cfg["profile"] = profile
    cfg.setdefault("profile", "standard")
    if source_roots:
        cfg["source_roots"] = list(source_roots)
    elif not cfg.get("source_roots"):
        cfg["source_roots"], _ = bfacts.detect_source_roots(root)
    cfg.setdefault("created_at", now_iso())
    cfg["updated_at"] = now_iso()
    cfg.setdefault("created_files", [])
    write_json(cp, cfg)
    from vmc_harness.bootstrap import gitignore
    gitignore.ensure(root)   # cache/ — solo면 ledger/·lexicon.json도(spec 09)
    return cfg, created


def _mark_created(root, rel):
    cp = paths.harness_dir(root) / "config.json"
    cfg = read_json(cp, {}) or {}
    files = cfg.setdefault("created_files", [])
    if rel not in files:
        files.append(rel)
        write_json(cp, cfg)


def render_rules_block(root):
    """AGENTS.md 표시 블록 — gates 영역 렌더 함수가 있으면 그것(CLAUDE.md `@AGENTS.md`도 그쪽이 보장).
    CLAUDE.md가 따로 있는데 `@AGENTS.md`를 안 불러오면 한 줄 덧붙이고(줄끝 유지) config에 남긴다 — init 출력·리포트가 알린다(B5)."""
    claude_existed = (Path(root) / "CLAUDE.md").exists()
    agents_existed = (Path(root) / "AGENTS.md").exists()
    imp = None
    if claude_existed:
        st_i, imp = util.optional_call([("vmc_harness.gates.rules", "ensure_claude_import")], str(root))
        if st_i == "ok" and imp == "added":
            cp = paths.harness_dir(root) / "config.json"
            cfg = read_json(cp, {}) or {}
            cfg["claude_import_added_at"] = now_iso()
            write_json(cp, cfg)
    st, res = util.optional_call([("vmc_harness.gates.rules", "render")], str(root))
    if st != "ok":
        res = _fallback_block(root)
        st = "fallback" if st == "skipped" else st
    if not claude_existed and (Path(root) / "CLAUDE.md").exists():
        _mark_created(root, "CLAUDE.md")
    if not agents_existed and (Path(root) / "AGENTS.md").exists():
        _mark_created(root, "AGENTS.md")
    if imp == "added" and isinstance(res, dict):
        res["claude_import_added"] = True
    return st, res


BEGIN = "<!-- vmc-harness:begin -->"
END = "<!-- vmc-harness:end -->"


def _fallback_block(root):
    from vmc_harness.bootstrap import facts as bfacts
    data = bfacts.read_rules(root)
    lines = [BEGIN, "## 하네스 규칙 (vmc-harness가 관리 — 이 블록 밖 내용은 그대로)", "", "### 환경 사실"]
    for r in data.get("rules", []):
        if (r.get("basis") or {}).get("type") == "machine_fact":
            lines.append("- %s" % (r.get("text") or "").replace("{value}", str(r.get("value", ""))))
    lines.append(END)
    block = "\n".join(lines) + "\n"
    from vmc_harness.core import textfile
    ap = Path(root) / "AGENTS.md"
    old, fmt = textfile.read(ap)
    block = textfile.to_eol(block, fmt["eol"])
    if old and BEGIN in old and END in old:
        new = old[:old.index(BEGIN)] + block + old[old.index(END) + len(END):].lstrip("\r\n")
    else:
        new = (old.rstrip("\r\n") + fmt["eol"] * 2 if old else "") + block
    textfile.write(ap, new, fmt)  # 고객 파일 — 원래 줄끝·BOM 유지
    cl = Path(root) / "CLAUDE.md"
    if not cl.exists():
        with cl.open("w", encoding="utf-8", newline="\n") as f:
            f.write("@AGENTS.md\n")
    return {"path": str(ap), "fallback": True}


def learning_path(root):
    """학습 후보 백그라운드 작업의 진행·완료 기록(로컬 원본)."""
    from vmc_harness.core.project import local_dir
    return local_dir(root) / "learning.json"


def _learning_sessions(root):
    """학습 대상: init이 쓴 세션 색인(마스킹본) 중 사람이 연 세션·발화 2개 이상·아직 해석 안 한 것."""
    from vmc_harness.bootstrap import diagnose
    from vmc_harness.ledger import selfcheck
    return [s for s in diagnose.read_sessions_index(root)
            if s.get("kind") == "main" and len(s.get("human_prompts") or []) >= 2
            and not selfcheck._is_done(root, s.get("session_id"))]


def _learning_alive(st, root):
    """running 기록이 살아 있는 작업인가 — 세션 하나 끝날 때마다 updated_at을 갱신하므로, LLM 상한 두 번을 넘게 멈췄으면 죽은 것."""
    from vmc_harness.core.clock import now, parse_iso
    if not st or st.get("status") != "running":
        return False
    at = parse_iso(st.get("updated_at") or st.get("started_at"))
    if at is None or at.tzinfo is None:
        return False
    return (now() - at).total_seconds() < 2 * config.get("selfcheck.timeout_s", root)


def start_learning(root, use_llm=True):
    """학습 후보를 분리된 백그라운드 프로세스(`vmc-harness learn-bg`)로 시작한다 — 세션당 약 22초라 init 안에서 기다리지 않는다.
    진행·완료는 learning.json·events, 끝나면 notices에 결과 숫자. 반환: {"status": async|sync|running|nothing|skipped, ...}"""
    try:
        from vmc_harness.ledger import selfcheck  # noqa: F401
    except ImportError:
        return {"status": "skipped", "reason": "ledger 영역 selfcheck 없음"}
    if not use_llm:
        return {"status": "skipped", "reason": "--no-llm"}
    from vmc_harness.core import events, llm
    if not llm.available():
        return {"status": "skipped", "reason": "claude CLI 없음"}
    cur = read_json(learning_path(root), None)
    if _learning_alive(cur, root):
        return {"status": "running", "total": cur.get("total"), "done": cur.get("done"), "path": str(learning_path(root))}
    todo = _learning_sessions(root)
    if not todo:
        return {"status": "nothing", "reason": "새로 읽을 세션 없음"}
    now = now_iso()
    write_json(learning_path(root), {"status": "running", "started_at": now, "updated_at": now, "total": len(todo), "done": 0})
    events.log(root, "learning_started", sessions=len(todo))
    from vmc_harness.core.proc import spawn_detached
    env = dict(os.environ)
    eng = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = eng + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        spawn_detached([sys.executable, "-m", "vmc_harness", "learn-bg", "--root", str(root)], cwd=str(root), env=env)
        return {"status": "async", "total": len(todo), "path": str(learning_path(root))}
    except Exception:
        res = learning_job(root)
        return dict(res, status="sync")


def learning_job(root):
    """백그라운드 본체(`learn-bg`). 세션 색인에서 남은 세션을 해석하고, 끝나면 events·notices에 결과를 남긴다."""
    from vmc_harness.core import events, notices
    root = Path(root)
    p = learning_path(root)
    st = read_json(p, {}) or {}
    st.update(status="running", pid=os.getpid(), updated_at=now_iso())
    write_json(p, st)

    def progress(done, total, res):
        st.update(done=done, total=total, updated_at=now_iso(), result={k: res[k] for k in ("sessions", "learnings", "cards", "failed")})
        write_json(p, st)

    try:
        res = run_learning(root, _learning_sessions(root), out=lambda *_: None, progress=progress)
    except Exception as e:
        st.update(status="error", error=str(e)[:300], updated_at=now_iso(), ended_at=now_iso())
        write_json(p, st)
        events.log(root, "learning_error", error=str(e)[:300])
        notices.add(root, "learning_done", "flow", "지난 세션에서 배운 판단을 찾다가 멈췄어요(%s). `vmc-harness init`을 다시 돌리면 남은 세션부터 이어 해요" % str(e)[:120])
        return st
    st.update(status="done", result=res, updated_at=now_iso(), ended_at=now_iso())
    write_json(p, st)
    events.log(root, "learning_done", **{k: res.get(k) for k in ("sessions", "learnings", "cards", "rejected", "failed", "tokens", "sec")})
    text = "지난 세션 %d개를 읽고 배운 판단 %d개를 카드로 만들었어요(후보 %d건 — 같은 정정이 한 번 더 나오면 카드가 돼요)." % (
        res["sessions"], res["cards"], res["learnings"])
    if res["failed"]:
        text += " %d개 세션은 해석하지 못했어요(로그인·시간 초과) — `vmc-harness init`을 다시 돌리면 그 세션만 다시 읽어요." % res["failed"]
    text += " 진단에 반영하려면 `vmc-harness diagnose`."
    notices.add(root, "learning_done", "flow", text, refs={"learning": str(p)})
    return st


def run_learning(root, sessions, out=print, use_llm=True, progress=None):
    """학습 후보: 세션 정정 해석 → ledger 영역 selfcheck에 위임. 2회 이상이면 카드(trial). finding은 만들지 않는다.
    해석한 세션은 selfcheck 완료 표시를 남겨 다시 돌려도(init 재실행·SessionEnd 점검) 두 번 세지 않는다."""
    try:
        from vmc_harness.ledger import selfcheck
    except ImportError:
        return {"status": "skipped", "reason": "ledger 영역 selfcheck 없음"}
    if not use_llm:
        return {"status": "skipped", "reason": "--no-llm"}
    from vmc_harness.core import ledger
    from vmc_harness.core.mask import mask
    t0 = time.time()
    res = {"status": "ok", "sessions": 0, "learnings": 0, "cards": 0, "rejected": 0, "failed": 0, "tokens": 0}
    main = [s for s in sessions if s.get("kind") == "main" and len(s.get("human_prompts") or []) >= 2]
    for i, s in enumerate(main, 1):
        if i == 1 or i % 20 == 0:
            out("    학습 후보 %d/%d 세션" % (i, len(main)))
        prompts = [mask(p.get("text") or "")[:config.get("selfcheck.prompt_chars", root)]
                   for p in s["human_prompts"]][-config.get("selfcheck.max_prompts", root):]
        try:
            data, call = selfcheck.extract(root, prompts)
        except Exception:
            data, call = None, None
        res["tokens"] += int((call or {}).get("tokens") or 0)  # 실패한 호출도 쓴 토큰은 센다
        if data is None:
            res["failed"] += 1
        else:
            _, learnings, _ = selfcheck.validate(data, prompts)  # 명시 요청은 지난 기록이라 여기서 조치하지 않는다
            res["sessions"] += 1
            for l in learnings:
                try:
                    r = selfcheck.apply_learning(root, l, s.get("session_id"), s.get("ended_at"))
                    res["learnings"] += 1
                    if r.get("card"):
                        res["cards"] += 1
                except ledger.MetaGateError:
                    res["rejected"] += 1
            if s.get("session_id"):
                selfcheck._mark_done(root, s["session_id"])
        if progress:
            progress(i, len(main), res)
    res["sec"] = round(time.time() - t0, 1)
    return res


def run(root, profile=None, dry_system=False, sessions_dir=None, analysis_dir=None, run_tests=True, use_llm=True,
        do_diagnose=True, learning=True, open_report=True, report_out=None, source_roots=None, out=print):
    """설치·부트스트랩·진단. 결과 요약 dict."""
    from vmc_harness.bootstrap import baseline, diagnose, facts as bfacts, report, sessions_input, structmap
    root = Path(root).resolve()
    t_all = time.time()
    summary = {"root": str(root), "steps": {}}

    def step(n, label):
        out("[%d/7] %s" % (n, label))

    step(1, "프로젝트 설정")
    cfg, created = write_config(root, profile, source_roots)
    summary["project_id"] = cfg["project_id"]
    out("    %s project_id %s · 프로파일 %s · 소스 루트 %s" % ("새로 만든" if created else "기존", cfg["project_id"][:8],
                                                         cfg["profile"], ", ".join(cfg["source_roots"])))

    step(2, "훅·MCP·커밋 trailer 등록")
    with _sandbox_codex_home(dry_system, root):
        st_h, r_h = util.optional_call([("vmc_harness.hooks.install", "register")], str(root), agents=("claude", "codex"),
                                       trust=not dry_system)
        st_m, r_m = util.optional_call([("vmc_harness.brain.install", "register_mcp")], str(root))
    st_c, r_c = util.optional_call([("vmc_harness.task.trailers", "install_commit_hook")], str(root))
    st_q, r_q = util.optional_call([("vmc_harness.qa.install", "register")], str(root), agents=("claude", "codex"))
    if dry_system:
        # mod 설치는 `claude plugin marketplace add`가 사용자 ~/.claude에 기록한다 — 리허설에서는 건너뛴다
        st_d, r_d = "dry", None
    else:
        st_d, r_d = util.optional_call([("vmc_harness.render.mod_install", "register")], str(root))
        if st_d == "ok" and isinstance(r_d, dict) and not r_d.get("ok"):
            st_d = "unavailable"  # claude 없음 등 — mod 없이 brief·HTML로 본다
    for name, st, r in (("훅", st_h, r_h), ("브레인 MCP", st_m, r_m), ("commit hook", st_c, r_c),
                        ("QA 서브에이전트", st_q, r_q), ("mod 패널", st_d, r_d)):
        if name == "훅" and st == "ok" and isinstance(r, dict) and isinstance(r.get("codex_trust"), dict):
            r_t = r["codex_trust"].get("status")
            out("    훅: 등록 (Codex 신뢰 %s)" % {"ok": "등록", "no_codex": "건너뜀(Codex 없음)"}.get(r_t, "실패 — Codex `/hooks`에서 한 번 신뢰"))
            continue
        out("    %s: %s" % (name, {"ok": "등록", "skipped": "건너뜀(해당 영역 없음)", "dry": "건너뜀(--dry-system)",
                                  "unavailable": "건너뜀 — %s" % ((r or {}).get("reason") if isinstance(r, dict) else r)}
                         .get(st, "오류 — %s" % r)))
    summary["steps"].update(hooks=st_h, mcp=st_m, commit_hook=st_c, qa_agent=st_q, mod=st_d)

    step(3, "Claude Code 세션 보관 기간")
    changed, note = ensure_cleanup_period(root, dry=dry_system)
    out("    " + (note or "이미 %d일 이상" % config.get("claude.cleanup_period_days", root)))
    summary["steps"]["cleanup_period"] = "changed" if changed else ("dry" if dry_system else "kept")

    step(4, "퇴근 뒤 루프 스케줄러")
    if dry_system:
        out("    건너뜀(--dry-system)")
        summary["steps"]["scheduler"] = "dry"
    else:
        st, r = util.optional_call([("vmc_harness.loop.scheduler", "register")], str(root))
        msg = "건너뜀(해당 영역 없음)" if st == "skipped" else (
            ("등록(%s)" % r.get("method")) if st == "ok" and isinstance(r, dict) and r.get("ok") else
            "등록 못 함 — `vmc-harness start-loop`로 수동 (%s)" % ((r or {}).get("reason") if isinstance(r, dict) else r))
        out("    " + msg)
        summary["steps"]["scheduler"] = st
        util.optional_call([("vmc_harness.loop.power", "check_sleep")], str(root))  # 절전이 루프보다 먼저 오면 알림(spec 06)

    step(5, "부트스트랩 — 기계 사실·구조 지도·기준선")
    t0 = time.time()
    data = bfacts.collect(root, run_tests=run_tests, use_llm=use_llm, progress=lambda m: out("    " + m))
    f = bfacts.load(root)
    # SQL 패치 폴더 관례 → db 영역 설정(db.patch_dir). 사람이 이미 정한 값은 그대로
    sp = (f.get("sql_patch_dir") or {}).get("suggested")
    if sp and f.get("db_kind"):
        cfgp = paths.harness_dir(root) / "config.json"
        c = read_json(cfgp, {}) or {}
        db = c.get("db") if isinstance(c.get("db"), dict) else {}
        if not db.get("patch_dir") or (db.get("patch_dir_basis") or {}).get("type") == "machine_fact":
            db["patch_dir"] = sp
            db["patch_dir_basis"] = {"type": "machine_fact", "command": (f.get("_commands") or {}).get("sql_patch_dir")}
            c["db"] = db
            write_json(cfgp, c)
    rules = bfacts.write_machine_fact_rules(root, f)
    st_r, r_r = render_rules_block(root)
    out("    규칙 블록: 환경 사실 %d줄 → AGENTS.md 표시 블록 (%s)" % (len(rules), {"ok": "gates 렌더", "fallback": "기본 렌더"}.get(st_r, "오류 — %s" % r_r)))
    if isinstance(r_r, dict) and r_r.get("claude_import_added"):
        out("    " + CLAUDE_IMPORT_NOTE)
    tb = f.get("test_baseline") or {}
    for r in tb.get("runs") or []:
        if r.get("runner") == "pytest" and r.get("collected") is not None:
            out("    테스트(%s): 수집 %s · 수집 오류 %d · 실패 %d (기준선)" % (r["dir"], r["collected"], len(r.get("collection_errors") or []),
                                                                len(r.get("failed") or [])))
        else:
            out("    테스트(%s): %s %s" % (r.get("dir"), r.get("status"), r.get("reason", "")))
    out("    구조 지도: 모듈 %d개 · 관례 %d개 · 보호 영역 후보 %d곳" % (
        (f.get("structure_map") or {}).get("modules", 0), len(f.get("conventions") or []), len(f.get("protected_candidates") or [])))
    sessions, info = sessions_input.load(root, sessions_dir, analysis_dir)
    tags = sessions_input.load_analysis_tags(analysis_dir) if analysis_dir else None
    out("    세션 기록: %s — 사람이 연 세션 %s개%s" % (info.get("source"), info.get("main", 0),
                                              " (상한 %d로 잘림)" % info["limit"] if info.get("truncated") else ""))
    diagnose.write_sessions_index(root, sessions)
    if sessions:
        bl = baseline.build(root, sessions, info, tags)
        b = bl["before"]
        out("    기준선: 세션당 사람 개입 중위 %s · 완료 후 재확인 %s" % (
            (b.get("interventions_per_session") or {}).get("median"),
            "%.1f%%" % (100 * b["recheck_after_done_ratio"]["value"]) if (b.get("recheck_after_done_ratio") or {}).get("value") is not None else "-"))
    else:
        out("    기준선: 세션 기록 없음 — 코드베이스·브레인에서 출발")
    # 독자 프로필(렌더링 엔진이 읽는 사람 말투·어휘) — 방금 읽은 세션의 사람 프롬프트로 초기 계산
    prompts = [hp.get("text") for s in sessions if s.get("kind", "main") == "main"
               for hp in (s.get("human_prompts") or []) if isinstance(hp, dict) and hp.get("text")]
    st_p, r_p = util.optional_call([("vmc_harness.render.profile", "compute")], str(root), prompts=prompts)
    out("    독자 프로필: %s" % ({"ok": "프롬프트 %d개로 계산" % len(prompts), "skipped": "건너뜀(해당 영역 없음)"}
                             .get(st_p, "오류 — %s" % r_p)))
    summary["steps"]["reader_profile"] = st_p
    summary["steps"]["bootstrap_sec"] = round(time.time() - t0, 1)
    if learning and sessions:
        lr = start_learning(root, use_llm=use_llm)
        st_l = lr.get("status")
        if st_l == "async":
            msg = "백그라운드로 시작 — 세션 %d개, 끝나면 알림에 결과(진행: %s)" % (lr["total"], lr["path"])
        elif st_l == "running":
            msg = "이미 백그라운드에서 진행 중 — %s/%s 세션(진행: %s)" % (lr.get("done"), lr.get("total"), lr["path"])
        elif st_l == "sync":
            r = lr.get("result") or {}
            msg = "세션 %s개 · 후보 %s · 카드 %s (백그라운드 실행 실패로 여기서 돌림)" % (r.get("sessions"), r.get("learnings"), r.get("cards"))
        else:
            msg = "건너뜀 — %s" % lr.get("reason")
        out("    학습 후보(세션 정정 해석): " + msg)
        summary["steps"]["learning"] = lr

    step(6, "시니어식 진단")
    if do_diagnose:
        t0 = time.time()
        d = diagnose.run(root, sessions=None, analysis_dir=analysis_dir, progress=lambda m: out("    " + m), use_llm=use_llm)
        out("    %s (%.0f초)" % ("제안 %d개" % len(d.get("proposals") or []) if d.get("status") == "ok" else
                               "제안 없음 — %s" % (d.get("reason") or "; ".join(d["checks"].get("format_errors") or [])), time.time() - t0))
        summary["steps"]["diagnose"] = d.get("status")
    else:
        out("    건너뜀(--no-diagnose)")

    step(7, "진단 리포트")
    rep = report.build(root, open_browser=open_report, out_dir=report_out)
    out("    HTML: %s" % rep.get("html_copy", rep["html"]))
    out("    PDF: %s" % (rep.get("pdf_copy") or rep.get("pdf") or rep.get("pdf_error")))
    summary["report"] = rep
    summary["sec"] = round(time.time() - t_all, 1)
    out("완료 (%.0f초)" % summary["sec"])
    return summary
