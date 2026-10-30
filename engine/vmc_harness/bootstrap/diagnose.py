"""시니어식 진단 (spec 07 — 01 P6: 절차 없이 목표·참고·제약·출력 형식만).

- `claude -p` 도구 허용·읽기 전용(쓰기 도구 없음, dontAsk — 허용 목록 밖 명령은 자동 거부)으로 실행
- 출력 JSON을 기계 검사(형식이 안 맞으면 1회 다시 묻기). 내용 품질은 판정하지 않는다(정답지·수용률로 잰다)
- 제약 검사: 실행 전후 작업 트리 비교(쓰기 0), 보호 영역 안 코드 변경 제안 제외, 시간·토큰 예산
- 제안마다 백테스트를 하네스가 다시 돌려(결정적) 리포트의 "이 장치가 있었다면"에 쓴다
- `diagnose accept 1 3` = 명시 요청 → 게이트 생성(gates 영역 위임)·보호 영역 추가·규칙. 안 고르면 아무 일 없음, 다시 묻지 않음
- structure 제안은 [추가] 없이 루프 티켓으로 등록된다(loop.tickets — 테스트만 바뀌면 자동 반영, 원본 코드면 제안). [제외]가 취소
  gate·rule·protected는 자동으로 태우지 않는다 — 게이트·규칙 근거는 사람 명시 요청이어야 하고(01 P2·P3) 보호 영역은 사람 몫(00 기준 2)
- 제안 → 장치 변환은 기계 검사로 뜻을 지킨다(B1~B3): rule은 `rule_text`(에이전트가 읽을 규칙 문장 자체 — 할 일 문장이면 거름),
  무엇을 실행하게 하는 gate 제안("~전에 검사 돌리기")은 막는 장치가 아니라 structure로 바꾼다,
  "~뒤에 켠다" 같은 전제가 있으면 `requires`(기계 확인 — bootstrap.requires)가 있어야 하고, 그 장치는 꺼진 채 만들어 밤 루프가 켠다
- 참고 자료에 이 PC의 사용자 단위 설정(user_setup — 이름·설명·사용 횟수·훅 시간)을 넣고, 결과 `user_level`은 제안만([추가] 대상 아님)
- 두 번째 진단부터 설치 이후 실측(remeasure)을 넣고 결과 `since_init`에 남긴다. 정기 자동 재진단(기본 하루 1회)은 weekly.py
"""

import json
import os
import re
import shutil
import sys
from pathlib import Path

from vmc_harness import KERNEL_VERSION
from vmc_harness.bootstrap import backtest, requires, util
from vmc_harness.core import config, gitutil, ledger, paths
from vmc_harness.core.clock import now_iso
from vmc_harness.core.jsonio import append_jsonl, read_json, write_json
from vmc_harness.core.mask import mask
from vmc_harness.core.project import local_dir

KINDS = ("gate", "protected", "structure", "rule")
KIND_ALIASES = {"block": "gate"}  # 막기 = gate
REQUIRED = ("title", "kind", "evidence", "blocks", "scope", "cost")
_HAS_NUM_OR_QUOTE = re.compile(r"(\d|[\"“”'‘’「」『』])")
# 무엇을 실행하게 하는 제안(B1 "push 직전에 검사를 자동으로 돌리기") — 막는 장치가 아니라 구조 티켓
_EXEC = re.compile(r"(돌리기|돌린다|돌려\s*(보|두)|실행하기|실행한다|실행되게|자동\s*실행|자동으로\s*(돌|실행|검사|확인)|수행하기|\brun\b)", re.I)
_BLOCK_WORD = re.compile(r"(막|금지|차단|거부|못\s*하게|않게|하지\s*않|block|forbid|deny|prevent)", re.I)
# 켜는 전제가 문장에만 있는 제안(B2 "위 명령이 생긴 뒤 켭니다") — requires가 있어야 한다
_COND_ENABLE = re.compile(r"((생긴|생기|만든|만들|반영|추가|설치|도입|갖춘|준비)\S*\s*(뒤|후|다음)\S*\s*(켜|켭|활성|적용)|켜\s*십시오|"
                          r"\b(once|after)\b.*\b(exists?|added|installed)\b)", re.I)
# 규칙 문장이 아니라 할 일 문장(B3 "~를 AGENTS.md 규칙으로 올리기", "~절로 옮기기")
_TODO_END = re.compile(r"(기|하자|할\s*것을\s*제안)\s*[.)\]」』'\"”’]*\s*$")
_TODO_META = re.compile(r"(AGENTS\.md|CLAUDE\.md|규칙|절|문서|메모|지침)\S*\s*(으로|로|에)\s*(옮기|올리|추가하|넣|승격|정리하)")
_TODO_EN = re.compile(r"^\s*(move|add|promote|put|document|write)\b", re.I)


def rule_text_error(text):
    """규칙 문장 기계 검사 — 에이전트가 그대로 읽을 규칙("~한다", "~하지 않는다")인가. 오류 문장 또는 None."""
    t = (text or "").strip()
    if not t:
        return "rule인데 rule_text(규칙 문장)가 없음"
    if len(t) > config.get("diagnose.rule_text_max_chars"):
        return "rule_text가 너무 김 — 규칙 문장 하나로"
    if _TODO_META.search(t) or _TODO_END.search(t) or _TODO_EN.search(t):
        return "rule_text가 규칙이 아니라 할 일 문장(~로 옮기기·~를 규칙으로 올리기) — 에이전트가 읽을 규칙 문장 자체로"
    return None


def _is_exec_proposal(p):
    t = str(p.get("title") or "")
    return bool(_EXEC.search(t)) and not _BLOCK_WORD.search(t)


def diagnosis_path(root):
    return paths.cache_dir(root) / "diagnosis.json"


def history_path(root):
    return local_dir(root) / "diagnose-history.json"


def input_dir(root):
    return local_dir(root) / "diagnose-input"


def harness_cmd(root=None):
    """에이전트·도구가 부를 하네스 명령. 설치된 CLI가 없으면 로컬 폴더에 `vmc-harness` 대리 실행 파일을 두고 PATH 앞에 붙인다
    (진단 모델이 허용 목록 `Bash(vmc-harness backtest:*)` 그대로 부를 수 있게)."""
    if shutil.which("vmc-harness"):
        return ["vmc-harness"], {}
    engine = str(Path(__file__).resolve().parents[2])
    env = {"PYTHONPATH": engine + os.pathsep + os.environ.get("PYTHONPATH", "")}
    if root is None:
        return [sys.executable, "-m", "vmc_harness"], env
    bindir = paths.ensure(local_dir(root) / "bin")
    if os.name == "nt":
        (bindir / "vmc-harness.cmd").write_text('@"%s" -m vmc_harness %%*\r\n' % sys.executable, encoding="utf-8")
    else:
        sh = bindir / "vmc-harness"
        sh.write_text('#!/bin/sh\nexec "%s" -m vmc_harness "$@"\n' % sys.executable, encoding="utf-8")
        sh.chmod(0o755)
    env["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")
    return ["vmc-harness"], env


# ---------------------------------------------------------------- 참고 자료 묶음 (로컬 원본 폴더 — 원문은 PC 밖으로 안 나간다)

def sessions_index_path(root):
    return local_dir(root) / "sessions-index.jsonl"


def write_sessions_index(root, sessions):
    """백테스트·진단이 읽는 세션 색인(로컬). 원문은 마스킹·길이 상한."""
    p = sessions_index_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    lim = config.get("diagnose.max_prompt_chars", root)
    with p.open("w", encoding="utf-8", newline="\n") as f:
        for s in sessions:
            rec = {k: s.get(k) for k in ("session_id", "agent", "kind", "started_at", "ended_at", "human_turns", "tokens")}
            rec["human_prompts"] = [{"at": x.get("at"), "text": util.scrub_user(mask((x.get("text") or "")[:lim]))}
                                    for x in s.get("human_prompts") or []]
            rec["tool_calls"] = [dict({"at": t.get("at"), "name": t.get("name"), "write": t.get("write"),
                                       "paths": t.get("paths"),
                                       "commands": [util.scrub_user(mask(c[:600])) for c in t.get("commands") or []]},
                                      **({"skill": t["skill"]} if t.get("skill") else {}))
                                 for t in s.get("tool_calls") or []]
            if s.get("hook_runs"):
                rec["hook_runs"] = s["hook_runs"]  # key는 user_setup._cmd_key가 이미 마스킹했다
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return p


def read_sessions_index(root):
    p = sessions_index_path(root)
    out = []
    if p.is_file():
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def prepare_inputs(root, sessions=None, analysis_dir=None, measured=None):
    """진단 모델이 읽을 참고 자료. 경로 dict. measured = init 이후 실측(두 번째 진단부터, remeasure.collect)."""
    from vmc_harness.bootstrap import facts as bfacts, user_setup
    d = input_dir(root)
    if d.exists():
        shutil.rmtree(str(d), ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    if sessions is not None:
        write_sessions_index(root, sessions)
    sess = read_sessions_index(root)
    with (d / "prompts.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for s in sess:
            if s.get("kind") != "main":
                continue
            for i, p in enumerate(s.get("human_prompts") or []):
                f.write(json.dumps({"session": (s.get("session_id") or "")[:8], "agent": s.get("agent"),
                                    "i": i, "at": p.get("at"), "text": p.get("text")}, ensure_ascii=False) + "\n")
    with (d / "tool_calls.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for s in sess:
            for t in s.get("tool_calls") or []:
                if not (t.get("paths") or t.get("commands")):
                    continue
                f.write(json.dumps({"session": (s.get("session_id") or "")[:8], "kind": s.get("kind"),
                                    "at": t.get("at"), "tool": t.get("name"), "write": t.get("write"),
                                    "paths": t.get("paths"), "commands": t.get("commands")},
                                   ensure_ascii=False) + "\n")
    f = bfacts.load(root)
    write_json(d / "facts.json", f)
    write_json(d / "baseline.json", ledger.read_baseline(root) or {})
    acts = ledger.list_actions(root)
    write_json(d / "ledger.json", {
        "actions": [{k: a.get(k) for k in ("action_id", "layer", "status", "hypothesis", "target")} for a in acts if a],
        "findings": len(ledger.list_findings(root)), "candidates": len(ledger.list_candidates(root))})
    rc, out, _ = gitutil.git(root, "log", "-n", "300", "--stat", "--format=%n== %h %cI %s", timeout=60)
    (d / "git_log.txt").write_text(out if rc == 0 else "(git 이력 없음)", encoding="utf-8")
    rc, n, _ = gitutil.git(root, "rev-list", "--count", "HEAD")
    try:
        us = user_setup.collect(root, sess)
    except Exception as e:  # 편의 자료 — 못 읽어도 진단은 돈다
        us = {"summary": {"error": str(e)[:200]}, "docs_text": ""}
    write_json(d / "user_setup.json", us["summary"])
    (d / "user_setup_docs.md").write_text(us["docs_text"] or "(사용자 단위 지침 문서 없음)", encoding="utf-8")
    if measured:
        write_json(d / "measured.json", measured)
    return {"dir": d, "commits": int(n.strip()) if rc == 0 and n.strip().isdigit() else 0,
            "sessions": sum(1 for s in sess if s.get("kind") == "main"), "analysis_dir": analysis_dir,
            "user_setup": us["summary"], "measured": bool(measured)}


# ---------------------------------------------------------------- 지시문 (목표·참고·제약·출력 형식만)

def build_prompt(root, inputs, shown_titles, user_shown_titles=()):
    top_k = config.get("diagnose.top_k", root)
    hc = "vmc-harness"
    d = inputs["dir"]
    refs = [
        "- 코드: 지금 폴더(프로젝트 루트)",
        "- git 이력: `git log`, `git show` (커밋 %d개 — 짧으면 세션 기록이 주 근거)" % inputs["commits"],
        "- 세션 기록(사람이 연 세션 %d개): %s/prompts.jsonl(사람 프롬프트), %s/tool_calls.jsonl(도구 호출 — 경로·명령)" % (inputs["sessions"], d, d),
        "- 기계 사실(명령·테스트 기준선·DB·SQL 패치 폴더·기존 관례·보호 영역 후보): %s/facts.json" % d,
        "- 구조 지도: .harness/cache/map.md (기계용 map.json)",
        "- 기준선(과거 세션 지표와 정의): %s/baseline.json" % d,
        "- 장부 요약: %s/ledger.json" % d,
        "- git 변경 요약: %s/git_log.txt" % d,
        "- 이 PC의 사용자 단위 설정(프로젝트 밖 ~/.claude·~/.codex — 스킬·플러그인·훅 이름과 설명, 지난 AI 대화에서 불린 수·훅이 걸린 시간): %s/user_setup.json, "
        "지침 원문(사용자 단위 CLAUDE.md·AGENTS.md·스킬 설명): %s/user_setup_docs.md" % (d, d),
    ]
    if inputs.get("measured"):
        refs.append("- 하네스 설치 이후 실측(넣은 장치·판정·노출, 작업 예측 대비 실측, 장치별 발동·막음·헛막기·시간, "
                    "하네스 전 기준선과 같은 정의로 다시 잰 개입 수, 지난 제안의 [추가]·[제외]): %s/measured.json" % d)
    if inputs.get("analysis_dir"):
        refs.append("- 사람이 프롬프트를 전수 태깅한 분석(A 기획 결손 / B 개발 결정 / C 절차 / D AI 결함 복구): %s (tag_*.md, stats.json)" % inputs["analysis_dir"])
    refs += [
        "- 도구(쓸지는 판단): 백테스트 실행기 `%s backtest --condition '<JSON>' --json` — 후보 게이트 조건을 지난 커밋·세션 도구 호출에 돌려 막았을 횟수·실행 시간을 준다. "
        "조건 type: path_write{globs} / write_outside{ext,allowed} / command{regex} / db_direct{} / test_modify{globs?} / diff_regex{regex,globs?}" % hc,
        "- 도구: 테스트 실행 `%s diagnose tool-test [--dir <폴더>]`, DB 조회 `%s db query \"<SELECT>\"`(등록돼 있을 때만)" % (hc, hc),
        "- 셸 도구는 위 명령과 git 읽기 명령만 허용된다. 한 줄에 명령 하나로 부른다(cd·파이프·리디렉션·&& 없이 — 붙이면 거부된다). 지금 폴더가 프로젝트 루트다.",
    ]
    shown = "\n".join("- " + t for t in shown_titles) or "(없음)"
    user_shown = "\n".join("- " + t for t in user_shown_titles) or "(없음)"
    goal = ["목표: 이 프로젝트에서 사람 개입과 결함을 줄일 게이트·구조 개선을, 레거시 코드베이스를 처음 맡은 시니어 개발자처럼 진단하고 제안한다.",
            "사용자 단위(이 PC의 ~/.claude·~/.codex)에 걸린 지침·스킬·플러그인·훅 중 과한 검증, 이 프로젝트 의도와 다른 지침, 작업을 느리게 하는 게이트가 있는지도 진단 대상이다."]
    if inputs.get("measured"):
        goal.append("이번은 다시 하는 진단이다. 이미 넣은 장치가 실제로 막았는지·헛막았는지·개입이 줄었는지를 실측으로 보고, 지난 진단 이후 달라진 점을 함께 판단한다.")
    return "\n".join(goal + [
        "",
        "참고할 수 있는 것:",
    ] + refs + [
        "",
        "제약:",
        "- 제안만 한다. 파일을 만들거나 고치지 않는다.",
        "- 시간 %d초·토큰 %d 안에서 끝낸다." % (config.get("diagnose.budget_sec", root), config.get("diagnose.budget_tokens", root)),
        "- 보호 영역(.harness/protected.json — 없으면 아직 없음) 안의 코드를 바꾸는 제안은 내지 않는다. facts.json protected_candidates는 후보일 뿐이고, 보호 영역으로 지정하자는 제안은 된다.",
        "- 게이트는 사람 명시 요청이나 코드·설정에서 결정적으로 읽은 사실에 근거한다. 취향(문체·디자인 감각)처럼 기계로 판정할 수 없는 것은 게이트로 내지 않는다.",
        "- 결함의 주어는 도구·과정이다. 사람 이름을 쓰지 않는다.",
        "- 독자는 이 프로젝트 개발자·팀장·경영진이다. VMC 컨설턴트가 이 팀에게 직접 설명하듯 존댓말(~습니다)로, 짧은 문장과 쉬운 말로 쓴다. 하네스 내부 용어(게이트·장부·finding·백테스트·L0 등)와 세션·커밋 식별자는 쓰지 않는다 — '세션'은 'AI 대화'로. 코드·경로·명령·원문 인용은 그대로 둔다.",
        "- 사용자 단위 설정은 하네스가 고치지 않는다 — user_level에 제안만 낸다. 고객 PC의 사적 설정이라 원문을 인용하지 않고 이름과 요지만 쓴다.",
        "- 이미 보인 제안은 다시 내지 않는다:",
        shown,
        "- 이미 보인 사용자 단위 제안:",
        user_shown,
        "",
        "출력 형식: JSON 객체 하나만(설명 글 없이).",
        '{"summary": {"headline": "결론 한 문장(경영진이 이것만 읽어도 되게)",',
        '             "observations": ["반복된 문제 하나를 한두 문장으로 — 3개 이내"],',
        '             "recommendation": "무엇부터 할지 한두 문장"%s},' % (
            ',\n              "since_last": "지난 진단 이후 달라진 점 한두 문장(실측 숫자로)"' if inputs.get("measured") else ""),
        ' "proposals": [{"title": "무엇을 넣나 한 줄",',
        '   "kind": "gate | protected | structure | rule",',
        '   "rule_text": "에이전트가 그대로 읽을 규칙 문장 하나(~한다·~하지 않는다)" — rule이면 필수,',
        '   "requires": [{"type": "file_exists", "path": "상대 경로"} | {"type": "npm_script", "name": "스크립트 이름", "dir": "."}]'
        ' — 장치를 켜기 전에 있어야 하는 것이 있을 때만,',
        '   "evidence": "근거 — 수치 또는 원문 인용(따옴표)",',
        '   "blocks": "막는 것",',
        '   "scope": "발동 범위(경로·명령·작업 종류)",',
        '   "cost": "비용 추정(작업당 초·유지 부담)",',
        '   "condition": {백테스트 조건 — gate면 필수},',
        '   "protected_globs": ["보호할 경로 glob" — protected면 필수],',
        '   "targets": ["바꾸게 될 파일" — structure면],',
        '   "backtest": {"would_block": 숫자, "problem_changes": 숫자} — 돌려 봤으면}],',
        ' "user_level": [{"title": "무엇을 바꾸면 좋은가 한 줄",',
        '   "setting": "대상 스킬·플러그인·훅·지침 파일 이름",',
        '   "issue": "과한 검증 | 의도와 다른 지침 | 느린 게이트 | 기타",',
        '   "evidence": "근거 — 호출 수·걸린 시간 같은 수치 또는 요지(원문 인용 없이)",',
        '   "suggestion": "사람이 바꾼다면 무엇을"}] — 없으면 빈 목록}',
        "kind: gate = 조건에 걸리는 AI 동작을 막기만 하는 장치(무엇을 실행하지 않는다) / rule = 에이전트가 매번 읽을 규칙 — "
        "rule_text에 규칙 문장 자체를 쓴다(\"~로 옮기기\", \"~를 규칙으로 올리기\" 같은 할 일은 안 된다) / "
        "structure = 코드·설정·스크립트를 바꾸거나 만드는 일 — \"push 전에 검사를 돌리기\"처럼 무엇을 실행하게 하는 제안은 structure다 / "
        "protected = 사람 승인 없이 바꾸면 안 되는 경로.",
        "\"~가 생긴 뒤 켠다\"처럼 전제가 있으면 문장으로 쓰지 말고 requires에 넣는다(하네스가 꺼 두었다가 전제가 맞으면 켠다).",
        "proposals는 중요한 순서로 %d개 안팎." % top_k,
    ])


# ---------------------------------------------------------------- 출력 기계 검사

def extract_json(text):
    if not text:
        return None
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S):
        try:
            d = json.loads(m.group(1))
            if isinstance(d, dict):
                return d
        except ValueError:
            pass
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        try:
            d = json.loads(text[s:e + 1])
            return d if isinstance(d, dict) else None
        except ValueError:
            return None
    return None


def validate(data, root=None):
    """(정상 제안 목록, 오류 목록). 형식만 본다."""
    errors, ok = [], []
    if not isinstance(data, dict):
        return [], ["JSON 객체가 아님"]
    props = data.get("proposals")
    if not isinstance(props, list) or not props:
        return [], ["proposals 목록이 비었거나 없음"]
    for i, p in enumerate(props, 1):
        if not isinstance(p, dict):
            errors.append("%d번: 객체가 아님" % i)
            continue
        miss = [k for k in REQUIRED if not str(p.get(k) or "").strip()]
        if miss:
            errors.append("%d번: 빈 필드 %s" % (i, ", ".join(miss)))
            continue
        p["kind"] = KIND_ALIASES.get(p["kind"], p["kind"])
        if p["kind"] not in KINDS:
            errors.append("%d번: kind가 %s 중 하나가 아님" % (i, "|".join(KINDS)))
            continue
        if not _HAS_NUM_OR_QUOTE.search(str(p["evidence"])):
            errors.append("%d번: 근거에 수치나 원문 인용이 없음" % i)
            continue
        if p["kind"] == "gate" and _is_exec_proposal(p):
            # 실행형 제안을 막는 게이트로 만들면 뜻이 뒤집힌다(B1) — 구조 티켓으로
            p["kind"], p["converted_from"] = "structure", "gate"
            p.pop("condition", None)
        if p["kind"] == "gate":
            c = p.get("condition")
            if not isinstance(c, dict) or c.get("type") not in backtest.TYPES:
                errors.append("%d번: gate인데 condition.type이 %s 중 하나가 아님" % (i, "|".join(backtest.TYPES)))
                continue
        if p["kind"] == "rule":
            err = rule_text_error(p.get("rule_text"))
            if err:
                errors.append("%d번: %s" % (i, err))
                continue
            p["rule_text"] = p["rule_text"].strip()
        err = requires.validate(p.get("requires"))
        if err:
            errors.append("%d번: %s" % (i, err))
            continue
        if not p.get("requires") and p["kind"] in ("gate", "rule") and any(
                _COND_ENABLE.search(str(p.get(k) or "")) for k in ("title", "scope", "blocks", "cost")):
            errors.append("%d번: 켜는 전제가 문장에만 있음 — requires로(기계로 확인할 파일·npm 스크립트)" % i)
            continue
        if p["kind"] == "protected":
            g = p.get("protected_globs")
            if not isinstance(g, list) or not g or not all(isinstance(x, str) and x.strip() for x in g):
                errors.append("%d번: protected인데 protected_globs가 없음" % i)
                continue
        ok.append(p)
    return ok, errors


USER_REQUIRED = ("title", "setting", "evidence", "suggestion")


def validate_user_level(data):
    """사용자 단위 제안(제안만 — [추가] 대상 아님). (정상 목록, 뺀 이유). 형식만 본다 — 형식이 틀린 항목은 빼고 다시 묻지 않는다."""
    items = (data or {}).get("user_level") if isinstance(data, dict) else None
    ok, bad = [], []
    for i, u in enumerate(items if isinstance(items, list) else [], 1):
        if not isinstance(u, dict):
            bad.append("사용자 단위 %d번: 객체가 아님" % i)
            continue
        miss = [k for k in USER_REQUIRED if not str(u.get(k) or "").strip()]
        if miss:
            bad.append("사용자 단위 %d번: 빈 필드 %s" % (i, ", ".join(miss)))
            continue
        ok.append({k: str(u.get(k) or "").strip() for k in USER_REQUIRED + ("issue",)})
    return ok, bad


def _protected_globs(root):
    """확정된 보호 영역(.harness/protected.json)만 — 후보는 사람이 [추가]하기 전엔 보호 영역이 아니다."""
    gl = []
    data = read_json(paths.harness_dir(root) / "protected.json", None) or {}
    for it in (data.get("paths", []) if isinstance(data, dict) else data) or []:
        gl.append(it if isinstance(it, str) else it.get("glob"))
    return [g for g in gl if g]


def constraint_check(root, proposals):
    """보호 영역 비접촉 — structure 제안이 보호 경로 안 코드를 바꾸자고 하면 뺀다. (남은 것, 뺀 이유들)"""
    prot = _protected_globs(root)
    keep, dropped = [], []
    for p in proposals:
        if p["kind"] == "structure":
            # 바꾸겠다고 적은 파일(targets)만 본다 — 범위 설명에 보호 경로가 나오는 것(테스트 추가 등)은 비접촉이다
            targets = [t for t in (p.get("targets") or []) if isinstance(t, str)]
            hit = [t for t in targets if util.any_glob(t.rstrip("/"), prot)]
            if hit:
                dropped.append("'%s' — 보호 영역 안 코드 변경(%s)" % (p["title"], ", ".join(hit[:3])))
                continue
        keep.append(p)
    return keep, dropped


def _tree_state(root):
    rc, out, _ = gitutil.git(root, "status", "--porcelain", "--untracked-files=all")
    if rc != 0:
        return None
    return sorted(l for l in out.splitlines() if " .harness/" not in l)  # 하네스 자신의 기록은 뺀다


def _key(p):
    return re.sub(r"\s+", " ", (p.get("title") or "").strip().lower())


# ---------------------------------------------------------------- 실행

def run(root, sessions=None, analysis_dir=None, progress=None, use_llm=True, trigger="manual"):
    """진단 → `.harness/cache/diagnosis.json`. dict를 돌려준다.
    두 번째 진단부터(직전 진단이 실제로 돌았으면) init 이후 실측을 입력에 더하고 결과 `since_init`에 남긴다(리포트 "지난 진단 이후")."""
    from vmc_harness.bootstrap import remeasure
    from vmc_harness.core import llm
    say = progress or (lambda *_: None)
    root = Path(root)
    hist = read_json(history_path(root), {"shown": [], "excluded": [], "accepted": []}) or {}
    prev = load(root)
    measured = None
    if isinstance(prev, dict) and prev.get("status") in ("ok", "no_proposals"):
        measured = remeasure.collect(root, sessions if sessions is not None else read_sessions_index(root), prev)
    inputs = prepare_inputs(root, sessions, analysis_dir, measured=measured)
    result = {"generated_at": now_iso(), "model": config.get("diagnose.model", root), "proposals": [],
              "summary": "", "checks": {"format_errors": [], "retries": 0, "dropped": [], "tree_changed": False,
                                       "over_budget": False}, "status": None, "trigger": trigger,
              "user_setup": inputs.get("user_setup"), "user_level": []}
    if measured:
        result["since_init"] = measured
    if not use_llm or not llm.available() or llm.nested():
        result["status"] = "skipped"
        result["reason"] = "claude CLI 없음" if not llm.available() else ("--no-llm" if not use_llm else "중첩 호출")
        write_json(diagnosis_path(root), result)
        return result
    shown = [h.get("title") for h in hist.get("shown", []) if h.get("title")]
    user_shown = [h.get("title") for h in hist.get("user_shown", []) if isinstance(h, dict) and h.get("title")]
    prompt = build_prompt(root, inputs, shown, user_shown)
    _, env_extra = harness_cmd(root)
    allowed = ["Read", "Grep", "Glob", "Bash(git log:*)", "Bash(git show:*)", "Bash(git blame:*)",
               "Bash(git diff:*)", "Bash(git ls-files:*)",
               "Bash(vmc-harness backtest:*)", "Bash(vmc-harness diagnose tool-test:*)", "Bash(vmc-harness db query:*)"]
    add_dirs = [str(inputs["dir"])] + ([str(analysis_dir)] if analysis_dir else [])
    before = _tree_state(root)
    budget_sec = config.get("diagnose.budget_sec", root)
    tokens_total, sec_total = 0, 0.0
    tries = 1 + config.get("diagnose.retry", root)
    data, errors, raw = None, [], None
    for attempt in range(tries):
        say("진단 모델 실행%s (상한 %d초)" % (" — 형식 오류로 다시" if attempt else "", budget_sec))
        p = prompt if attempt == 0 else (
            prompt + "\n\n앞선 출력이 형식 검사를 통과하지 못했다: " + "; ".join(errors[:8]) +
            "\n같은 진단 내용으로 출력 형식만 맞춰 JSON 하나로 다시 낸다.\n앞선 출력:\n" + (raw or "")[:12000])
        res = llm.claude_agent(p, model=config.get("diagnose.model", root), timeout=budget_sec, allowed=allowed,
                               cwd=str(root), add_dirs=add_dirs, env_extra=dict(env_extra, VMC_HARNESS_DIAGNOSE="1"))
        if not res:
            errors = ["모델 호출 실패(로그인·시간 초과·오류)"]
            break
        tokens_total += int(res.get("tokens") or 0)
        sec_total += float(res.get("sec") or 0)
        raw = res.get("text") or ""
        data = extract_json(raw)
        ok, errors = validate(data, root)
        if ok and not errors:
            break
        if attempt + 1 < tries:
            result["checks"]["retries"] += 1
    after = _tree_state(root)
    result["checks"]["tree_changed"] = before is not None and after is not None and before != after
    result["checks"]["format_errors"] = errors
    result["tokens"] = tokens_total
    result["sec"] = round(sec_total, 1)
    result["checks"]["over_budget"] = tokens_total > config.get("diagnose.budget_tokens", root) or sec_total > budget_sec
    ok, _ = validate(data, root) if data else ([], [])
    ok, dropped = constraint_check(root, ok)
    result["checks"]["dropped"] = dropped
    shown_keys = {_key(h) for h in hist.get("shown", [])} | {_key({"title": t}) for t in hist.get("excluded", [])}
    ok = [p for p in ok if _key(p) not in shown_keys]  # 다시 묻지 않는다
    top_k = config.get("diagnose.top_k", root)
    if len(ok) > top_k:
        result["checks"]["truncated"] = len(ok) - top_k
        ok = ok[:top_k]
    say("제안 %d개 — 백테스트 다시 실행(결정적)" % len(ok))
    sess = read_sessions_index(root)
    for i, p in enumerate(ok, 1):
        p["no"] = i
        p["model_backtest"] = p.pop("backtest", None)
        cond = p.get("condition") if p["kind"] == "gate" else None
        if p["kind"] == "protected":
            cond = {"type": "path_write", "globs": p["protected_globs"]}
        if cond:
            try:
                p["backtest"] = backtest.run(root, cond, sess)
            except Exception as e:  # 편의 경로 — 조건 오류는 표시만
                p["backtest"] = {"error": str(e)}
        p["display"] = render_proposal(root, p)
    result["proposals"] = ok
    result["summary"] = (data or {}).get("summary") or ""
    ul, ul_bad = validate_user_level(data)
    seen_ul = {_key(h) for h in hist.get("user_shown", []) if isinstance(h, dict)}
    result["user_level"] = [u for u in ul if _key(u) not in seen_ul][:top_k]
    if ul_bad:
        result["checks"]["user_level_dropped"] = ul_bad
    result["status"] = "ok" if ok else "no_proposals"
    write_json(diagnosis_path(root), result)
    # 루프가 할 수 있는 제안(structure)은 [추가] 없이 루프 티켓으로 — [제외]로 취소(사후 원클릭, 승인 게이트 아님)
    util.optional_call([("vmc_harness.loop.tickets", "sync_diagnosis")], str(root), result)
    # 루프 티켓은 사람이 고른 것이 아니라 수용률(보인 제안 중 [추가])에서 뺀다 — ticket 표시
    hist.setdefault("shown", []).extend({"title": p["title"], "at": result["generated_at"], "ticket": bool(p.get("ticket_id"))}
                                        for p in ok)
    hist.setdefault("user_shown", []).extend({"title": u["title"], "at": result["generated_at"]} for u in result["user_level"])
    write_json(history_path(root), hist)
    return result


def render_proposal(root, p):
    """제안 4줄을 렌더링 엔진으로(독자 말로). 엔진이 없거나 실패하면 원문 그대로."""
    facts = {"제안": p["title"], "근거": p["evidence"], "막는 것": p["blocks"], "발동 범위": p["scope"], "비용": p["cost"]}
    bt = p.get("backtest") or {}
    if isinstance(bt.get("would_block"), int):
        facts["막았을 횟수"] = "최소 %d건" % bt["would_block"]
    st, res = util.optional_call([("vmc_harness.render.engine", "render")], str(root), "team", facts,
                                 draft=None, kind="diagnosis")
    out = {k: p[k] for k in ("title", "evidence", "blocks", "scope", "cost")}
    if st == "ok" and isinstance(res, dict) and res.get("text"):
        out["rendered"] = res["text"]
        out["render_checks"] = res.get("checks")
    return out


def load(root):
    return read_json(diagnosis_path(root))


# ---------------------------------------------------------------- [추가]·[제외]

def _source(scope):
    return {"session": None, "date": now_iso()[:10], "area": str(scope or "")[:120], "via": "diagnose accept"}


def _add_protected(root, p, quote):
    pf = paths.harness_dir(root) / "protected.json"
    data = read_json(pf, None) or {"paths": []}
    items = data.setdefault("paths", []) if isinstance(data, dict) else data
    have = {(x if isinstance(x, str) else x.get("glob")) for x in items}
    kinds = {c["glob"]: c["category"] for c in _candidates(root)}
    kind_map = {"approval": "approval_flow", "generated_source": "generated", "seed": "db_write"}
    a = ledger.create_action(root, {"type": "explicit_request", "quote": quote, "ref": "diagnose"}, "gate",
                             "보호 영역 '%s'에 닿는 변경이 사람 승인 없이 들어가지 않는다" % ", ".join(p["protected_globs"][:3]),
                             human_requested=True, human_criterion=2, calls_human=True,
                             target={"protected": sorted(p["protected_globs"])})
    added = []
    for g in p["protected_globs"]:
        if g in have:
            continue
        k = kinds.get(g)
        items.append({"glob": g, "kind": kind_map.get(k, k), "action_id": a["action_id"], "added_at": now_iso(),
                      "basis": {"type": "explicit_request", "quote": quote, "source": _source(p.get("scope"))}})
        added.append(g)
    write_json(pf, data if isinstance(data, dict) else {"paths": items})
    return {"kind": "protected", "added": added, "action_id": a["action_id"]}


def _candidates(root):
    from vmc_harness.bootstrap import facts as bfacts
    return bfacts.load(root).get("protected_candidates") or []


def _set_patch_dir(root, cond):
    """SQL 패치 폴더 게이트 → config.json db.patch_dir(db 영역 안전장치가 이것으로 켜진다)."""
    allowed = [g for g in cond.get("allowed") or [] if g]
    if not allowed or ".sql" not in [e.lower() for e in cond.get("ext") or []]:
        return None
    d = allowed[0].replace("\\", "/").split("*")[0].rstrip("/")
    if not d:
        return None
    cfgp = paths.harness_dir(root) / "config.json"
    cfg = read_json(cfgp, {}) or {}
    db = cfg.get("db") if isinstance(cfg.get("db"), dict) else {}
    db["patch_dir"] = d
    cfg["db"] = db
    write_json(cfgp, cfg)
    return d


def gate_script(cond, title):
    """백테스트 조건 → 생성 게이트 본체(gates/api.py만 쓴다). 메시지는 무엇을 어겼는지만(P6)."""
    t = cond["type"]
    lit = json.dumps
    if t == "path_write":
        body = ("def check(api):\n"
                "    if api.paths:\n"
                "        return '정해 둔 경로 변경: ' + ', '.join(api.paths[:3])\n"
                "    return None\n")
    elif t == "write_outside":
        body = ("ALLOWED = %s\n"
                "EXT = %s\n"
                "def check(api):\n"
                "    bad = [p for p in api.paths if (not EXT or any(p.lower().endswith(e) for e in EXT))\n"
                "           and not any(api.match(p, g) for g in ALLOWED)]\n"
                "    if bad:\n"
                "        return '정해 둔 폴더 밖 파일 쓰기: ' + ', '.join(bad[:3])\n"
                "    return None\n") % (lit(cond.get("allowed") or []), lit([e.lower() for e in cond.get("ext") or []]))
    elif t == "command":
        body = ("import re\n"
                "RX = re.compile(%s, re.I | re.M)\n"
                "def check(api):\n"
                "    if api.command and RX.search(api.command):\n"
                "        return '정해 둔 금지 명령 패턴'\n"
                "    return None\n") % lit(cond["regex"])
    elif t == "diff_regex":
        body = ("import re\n"
                "RX = re.compile(%s, re.I | re.M)\n"
                "def check(api):\n"
                "    for p in api.paths:\n"
                "        old = set((api.head(p) or '').splitlines())\n"
                "        new = [l for l in (api.proposed(p) or '').splitlines() if l not in old]\n"
                "        if any(RX.search(l) for l in new):\n"
                "            return '정해 둔 금지 패턴이 새로 들어감: ' + p\n"
                "    return None\n") % lit(cond["regex"])
    else:
        return None
    return '"""%s (진단 제안 [추가] — 사람 명시 요청)"""\n%s' % (title.replace('"""', "'"), body)


def _trigger(cond):
    t = cond["type"]
    if t == "path_write":
        return {"events": ["PreToolUse"], "paths": cond.get("globs") or []}
    if t == "write_outside":
        exts = cond.get("ext") or []
        return {"events": ["PreToolUse"], "paths": ["**/*%s" % e for e in exts] or ["**"]}
    if t == "diff_regex":
        return {"events": ["PreToolUse"], "paths": cond.get("globs") or ["**"]}
    return {"events": ["PreToolUse"]}


def _create_gate(root, p, quote):
    """게이트 생성은 gates 영역에 위임: 조건이 백테스트 형식이면 본체를 조건 그대로 만들어 generated.create,
    아니면 generate.from_request(LLM이 본체를 쓴다). 범위는 제안 문구 그대로."""
    cond = p["condition"]
    src = _source(p.get("scope"))
    basis = {"type": "explicit_request", "quote": quote, "source": src}
    extra = {}
    if cond["type"] == "write_outside":
        d = _set_patch_dir(root, cond)
        if d:
            extra["db_patch_dir"] = d
    if cond["type"] in ("db_direct", "test_modify"):
        # 커널 안전 게이트(DB 직접 접속 차단·검증물 변조 금지)가 이미 막는다 — 요청 기록만(같은 걸 두 번 만들지 않는다)
        a = ledger.create_action(root, {"type": "explicit_request", "quote": quote, "ref": "diagnose"}, "gate",
                                 p["blocks"], human_requested=True, target={"kernel_gate": cond["type"]})
        return dict(extra, kind="gate", kernel=cond["type"], action_id=a["action_id"])
    script = gate_script(cond, p["title"])
    if script:
        kw = {"requires": p["requires"]} if p.get("requires") else {}
        st, r = util.optional_call([("vmc_harness.gates.generated", "create")], str(root), p["title"], basis,
                                   _trigger(cond), script, hypothesis=p["blocks"], **kw)
        if st == "ok":
            return dict(extra, kind="gate", gate_id=r["meta"]["gate_id"], action_id=r["meta"]["action_id"],
                        cost_line=r.get("cost_line"), **_waiting(root, p))
        if st == "error":
            return dict(extra, kind="gate", error=r)
    st, r = util.optional_call([("vmc_harness.gates.generate", "from_request")], str(root), quote, src)
    if st == "ok" and r:
        return dict(extra, kind="gate", gate_id=r["gate_id"], action_id=r["action_id"], cost_line=r.get("cost_line"))
    if st == "ok":
        # 기계로 판정할 수 없는 요청(None) — 제안에 규칙 문장이 있을 때만 규칙으로(제목은 할 일 문장일 수 있다 — B3)
        if rule_text_error(p.get("rule_text")):
            return dict(extra, kind="gate", error="기계로 막을 수 있는 형태가 아니고 규칙 문장(rule_text)도 없음 — 넣지 않음")
        r = _add_rule(root, dict(p, why_not_higher="기계로 막을 수 있는 형태가 아니어서 규칙으로 둔다"), quote)
        return dict(extra, **r)
    return dict(extra, kind="gate", error="게이트 생성 함수 없음" if st != "error" else r)


def _add_rule(root, p, quote):
    """명시 요청 규칙 — ledger.selfcheck.add_rule 하나로(gates.rules 형식, 조치 target {"rule_id"} — revert가 찾는다)."""
    from vmc_harness.ledger import selfcheck
    src = _source(p.get("scope"))
    err = rule_text_error(p.get("rule_text"))
    if err:  # 옛 진단 결과(rule_text 칸 없음)를 [추가]하는 경우 — 할 일 제목을 규칙으로 넣지 않는다(B3)
        return {"kind": "rule", "error": err + " — `vmc-harness diagnose`로 다시 진단하면 규칙 문장으로 제안해요"}
    rid, a = selfcheck.add_rule(root, quote, src, action_basis_ref="diagnose",
                                why_not_higher=(p.get("why_not_higher") or "기계로 판정할 수 없는 사실이라 규칙으로 둔다")[:200],
                                hypothesis=p.get("blocks"), rule_text=p["rule_text"].strip(), requires=p.get("requires"))
    return dict({"kind": "rule", "rule_id": rid, "action_id": a["action_id"]}, **_waiting(root, p))


def _waiting(root, p):
    """전제조건이 아직 안 맞아 꺼 둔 장치면 {"waiting": [설명]} — 지금 맞으면 바로 켠다(밤까지 기다릴 이유가 없다)."""
    if not p.get("requires"):
        return {}
    left = requires.unmet(root, p["requires"])
    if not left:
        requires.enable_ready(root)
        return {}
    return {"waiting": [requires.describe(x) for x in left]}


def accept(root, numbers):
    """[추가] = 명시 요청. 원문 = 제안 문구 그대로(title). 결과 목록."""
    d = load(root)
    if not d or not d.get("proposals"):
        raise ValueError("진단 결과 없음 — 먼저 `vmc-harness diagnose`")
    by_no = {p["no"]: p for p in d["proposals"]}
    hist = read_json(history_path(root), {"shown": [], "excluded": [], "accepted": []}) or {}
    out = []
    for n in numbers:
        p = by_no.get(int(n))
        if not p:
            out.append({"no": n, "error": "없는 번호"})
            continue
        if p.get("accepted"):
            out.append({"no": n, "skipped": "이미 추가함"})
            continue
        if p.get("ticket_id"):
            out.append({"no": n, "skipped": "이미 루프 티켓으로 등록됨", "ticket_id": p["ticket_id"]})
            continue
        quote = p["title"]
        try:
            if p["kind"] == "protected":
                r = _add_protected(root, p, quote)
            elif p["kind"] == "gate":
                r = _create_gate(root, p, quote)
            elif p["kind"] == "rule":
                r = _add_rule(root, p, quote)
            else:  # structure — 루프 티켓(06 §티켓). 보통은 진단 때 자동 등록돼 여기 오지 않는다
                from vmc_harness.loop import tickets
                c = tickets.register_ticket(root, p, human_requested=True)
                p["ticket_id"] = c["candidate_id"]
                r = {"kind": "structure", "ticket_id": c["candidate_id"], "status": c.get("status")}
        except ledger.MetaGateError as e:
            r = {"error": "메타 게이트 거부: %s" % e}
        r["no"] = n
        if not r.get("error"):
            p["accepted"] = now_iso()
            hist.setdefault("accepted", []).append({"title": quote, "at": p["accepted"]})
        out.append(r)
    write_json(diagnosis_path(root), d)
    write_json(history_path(root), hist)
    append_jsonl(local_dir(root) / "diagnose-accepts.jsonl", {"at": now_iso(), "numbers": list(numbers), "results": out,
                                                                 "kernel_version": KERNEL_VERSION})
    return out


def exclude(root, numbers):
    """[제외] = 패널 버튼(명시적 행동) → finding(button=exclude). 다시 내지 않는다."""
    d = load(root) or {}
    by_no = {p["no"]: p for p in d.get("proposals", [])}
    hist = read_json(history_path(root), {"shown": [], "excluded": [], "accepted": []}) or {}
    out = []
    for n in numbers:
        p = by_no.get(int(n))
        if not p:
            out.append({"no": n, "error": "없는 번호"})
            continue
        f = ledger.create_finding(root, "button", {"notice_kind": "diagnose:" + ledger.sha(_key(p))[:12]}, button="exclude")
        p["excluded"] = now_iso()
        hist.setdefault("excluded", []).append(p["title"])
        r = {"no": n, "finding_id": f["finding_id"]}
        if p.get("ticket_id"):  # 자동 등록된 루프 티켓 취소(준비된 브랜치·조치도 버린다)
            from vmc_harness.loop import tickets
            c = tickets.cancel_ticket(root, p["ticket_id"])
            r["ticket_status"] = (c or {}).get("status")
        out.append(r)
    write_json(diagnosis_path(root), d)
    write_json(history_path(root), hist)
    return out


def tool_test(root, d=None):
    """진단 모델용 테스트 실행 도구 — 기준선과 같은 방식으로 돌려 요약만(파일 안 씀)."""
    from vmc_harness.bootstrap import facts as bfacts
    f = bfacts.load(root)
    runs = []
    for c in f.get("command_list") or []:
        if c["kind"] != "test" or (d and c["dir"] != d):
            continue
        if c["name"] == "pytest":
            runs.append(bfacts.pytest_baseline(root, c["dir"]))
        elif c["name"] == "test" and c["source"].endswith("package.json"):
            runs.append(bfacts.npm_baseline(root, c["dir"]))
    base = set((f.get("test_baseline") or {}).get("failures") or [])
    for r in runs:
        now_fail = set(r.get("failed") or []) | set(r.get("errors") or []) | {e["path"] for e in r.get("collection_errors") or []}
        r["new_failures"] = sorted(x for x in now_fail if x not in base and ("%s/%s" % (r.get("dir"), x)) not in base)
    return runs
