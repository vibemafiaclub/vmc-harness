"""기준선 `ledger/baseline.json` (spec 07) — 과거 세션 지표. 2차 방문 전후 비교·예산 출발값.

숫자·정의만 담는다(원문 없음, P9). 지표 정의는 아래 DEFINITIONS가 원본이고 리포트에도 그대로 싣는다.
세션은 bootstrap.sessions_input.load()가 준 것(사람이 연 세션 kind=main만 센다).
"""

import re
import statistics

from vmc_harness.core import ledger
from vmc_harness.core.clock import now_iso

DEFINITIONS = {
    "interventions_per_session": "세션당 사람 개입 = 사람이 친 프롬프트 수 + AI가 사람에게 물은 질문(AskUserQuestion·request_user_input 하위 질문) 수. 사람이 연 세션만(서브에이전트·자동 승인 심사 세션 제외)",
    "tool_calls_per_session": "세션당 도구 호출 수",
    "interventions_per_10_tool_calls": "도구 호출 10건당 사람 개입 — 작업 크기가 커져 개입이 는 것을 악화로 잘못 읽지 않게(04 참고 지표)",
    "ai_questions_per_session": "세션당 AI의 질문 수",
    "recheck_after_done_ratio": "완료 후 재확인 비율 = 사람이 결과를 직접 돌려 보고 깨진 것을 다시 알린 프롬프트 ÷ 전체 사람 프롬프트. "
                                "method=tagged면 사람 전수 태깅의 D(AI 결함 복구) 비율, method=keyword면 아래 오류 신호 정규식에 걸린 비율(같은 방법끼리만 비교)",
    "error_paste_prompts": "오류 원문을 붙여 넣은 사람 프롬프트 수(Traceback·Error·Exception·에러/오류 메시지 줄)",
    "qa_human_prompts_per_qa_session": "QA 세션(브라우저 자동화·스크린샷 도구를 쓰거나 사람이 QA를 말한 세션)당 사람 프롬프트 수",
    "repeated_instructions": "같은 지시 반복 = 앞선 다른 세션의 사람 프롬프트와 글자 3-gram 자카드 유사도 0.6 이상인 프롬프트 수(10자 이상만) — 규칙이 세션을 넘어 이어지지 않은 신호",
    "tokens_per_session": "세션당 토큰(입력+출력+캐시)",
}
_FAIL_SIGNAL = re.compile(
    r"(Traceback|Exception|Error[:\s]|\bfailed\b|에러|오류|안\s?돼|안\s?됨|안\s?되|작동\s?안|동작\s?안|그대로\s?(야|임|네|인데)|깨져|깨짐|안\s?나와|안\s?보여|왜\s?안)", re.I)
_ERROR_PASTE = re.compile(r"(Traceback \(most recent call last\)|^\s*\w*(Error|Exception)\b.*$|^\s*(에러|오류)\s*[:：])", re.M)
_QA_TOOL = re.compile(r"(?i)(playwright|browser|chrome|screenshot|view_image|puppeteer|devtools)")
_QA_WORD = re.compile(r"(?i)(\bqa\b|\bpass\b|\bhold\b|시나리오|검수)")
JACCARD_MIN = 0.6     # 정의의 일부(설정 대상 아님 — 바꾸면 지표가 달라진다)
MIN_PROMPT_CHARS = 10


def _stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0, "mean": None, "median": None}
    return {"n": len(xs), "mean": round(statistics.mean(xs), 2), "median": round(statistics.median(xs), 2)}


def _shingles(text):
    t = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()
    return {t[i:i + 3] for i in range(max(0, len(t) - 2))}


def repeated_instructions(sessions):
    """다른 세션의 앞선 프롬프트와 거의 같은 프롬프트 수.

    모든 앞선 프롬프트와 짝지어 보면 프롬프트 수의 제곱이라(세션 200개·프롬프트 6천 개에 약 24초) 접두 필터로 후보만 본다.
    접두 필터: 3-gram을 드문 것부터 줄 세우면, 자카드 >= JACCARD_MIN인 두 집합은 각자 앞쪽
    (크기 - 필요 겹침 + 1)개 안에 같은 3-gram을 하나 이상 가진다. 후보만 정확히 다시 재므로 결과는 전수 비교와 같다."""
    items = []
    for s in sessions:
        for p in s.get("human_prompts") or []:
            text = p.get("text") or ""
            if len(text.strip()) >= MIN_PROMPT_CHARS:
                items.append((p.get("at") or s.get("started_at") or "", s.get("session_id"), _shingles(text)))
    items.sort(key=lambda x: x[0])
    df = {}
    for _, _, sh in items:
        for g in sh:
            df[g] = df.get(g, 0) + 1
    index = {}  # 3-gram → 그 3-gram이 접두에 있는 앞선 항목 번호
    count = 0
    for i, (_, sid, sh) in enumerate(items):
        if not sh:
            continue
        order = sorted(sh, key=lambda g: (df[g], g))
        need = max(1, int(JACCARD_MIN * len(sh)))  # 필요한 겹침의 하한(내림 — 접두가 길어질 뿐 놓치지 않는다)
        prefix = order[:len(sh) - need + 1]
        seen = set()
        for g in prefix:
            for j in index.get(g, ()):
                if j in seen:
                    continue
                seen.add(j)
                sid2, sh2 = items[j][1], items[j][2]
                # 크기 필터: 자카드 <= 작은 쪽 크기 ÷ 큰 쪽 크기
                if sid2 == sid or min(len(sh), len(sh2)) < JACCARD_MIN * max(len(sh), len(sh2)):
                    continue
                inter = len(sh & sh2)
                if inter and inter / float(len(sh | sh2)) >= JACCARD_MIN:
                    count += 1
                    break
            else:
                continue
            break
        for g in prefix:
            index.setdefault(g, []).append(i)
    return count


def compute(sessions, tags=None):
    """세션 목록 → 기준선 dict(숫자만)."""
    main = [s for s in sessions if s.get("kind", "main") == "main" and s.get("measured", True)]
    with_prompts = [s for s in main if s.get("human_prompts")]
    prompts = [p.get("text") or "" for s in with_prompts for p in s["human_prompts"]]
    tool_counts = [len(s.get("tool_calls") or []) for s in with_prompts]
    turns = [s.get("human_turns") or len(s.get("human_prompts") or []) for s in with_prompts]
    per10 = [10.0 * t / c for t, c in zip(turns, tool_counts) if c]
    by_agent = {}
    for s in with_prompts:
        a = by_agent.setdefault(s.get("agent") or "?", {"sessions": 0, "prompts": 0})
        a["sessions"] += 1
        a["prompts"] += len(s["human_prompts"])
    fail_kw = sum(1 for p in prompts if _FAIL_SIGNAL.search(p))
    pastes = sum(1 for p in prompts if _ERROR_PASTE.search(p))
    qa_sessions = []
    for s in with_prompts:
        tool_hit = any(_QA_TOOL.search((t.get("name") or "") + " " + " ".join(t.get("commands") or []))
                       for t in s.get("tool_calls") or [])
        word_hit = any(_QA_WORD.search(p.get("text") or "") for p in s["human_prompts"])
        if tool_hit or word_hit:
            qa_sessions.append(len(s["human_prompts"]))
    starts = sorted(s.get("started_at") for s in with_prompts if s.get("started_at"))
    recheck = {"method": "keyword", "value": round(fail_kw / float(len(prompts)), 4) if prompts else None,
               "numerator": fail_kw, "denominator": len(prompts)}
    out = {
        "period": [starts[0], starts[-1]] if starts else None,
        "sessions": {"main_with_prompts": len(with_prompts), "main_total": len(main),
                     "sub": sum(1 for s in sessions if s.get("kind") == "sub"),
                     "guardian": sum(1 for s in sessions if s.get("kind") == "guardian"),
                     "by_agent": by_agent},
        "human_prompts": len(prompts),
        "interventions_per_session": _stats(turns),
        "tool_calls_per_session": _stats(tool_counts),
        "interventions_per_10_tool_calls": _stats(per10),
        "ai_questions_per_session": _stats([s.get("asks") or 0 for s in with_prompts]),
        "recheck_after_done_ratio": recheck,
        "recheck_after_done_ratio_keyword": dict(recheck),
        "error_paste_prompts": pastes,
        "qa_human_prompts_per_qa_session": _stats(qa_sessions),
        "repeated_instructions": repeated_instructions(with_prompts),
        "tokens_per_session": _stats([s.get("tokens") for s in with_prompts if s.get("tokens")]),
    }
    if tags and tags.get("totals", {}).get("H"):
        t = tags["totals"]
        out["recheck_after_done_ratio"] = {"method": "tagged", "value": round(t["D"] / float(t["H"]), 4),
                                           "numerator": t["D"], "denominator": t["H"]}
        out["tagged"] = {k: t[k] for k in "HABCD"}
        substantive = t["A"] + t["B"] + t["D"]
        if substantive:
            out["tagged"]["D_share_of_substantive"] = round(t["D"] / float(substantive), 4)
            out["tagged"]["A_share_of_substantive"] = round(t["A"] / float(substantive), 4)
    return out


def build(root, sessions, info=None, tags=None):
    """기준선 계산 → 장부 baseline.json(core.ledger.write_baseline). 이미 있으면 "이전"은 처음 값을 지킨다.
    guardian_filtered = Codex 자동 승인 심사 요청을 사람 지시에서 뺀 세션 읽기로 쟀다는 표시(없으면 옛 기준선 — correct_guardian)."""
    before = compute(sessions, tags)
    prev = ledger.read_baseline(root)
    rec = {"definitions": DEFINITIONS, "source": {k: v for k, v in (info or {}).items() if k != "analysis_dir"},
           "before": before, "after": None, "computed_at": now_iso(), "guardian_filtered": True}
    if prev and prev.get("before") and prev["before"].get("human_prompts"):
        rec["before"] = prev["before"]      # 처음 기준선은 바꾸지 않는다
        rec["after"] = prev.get("after")
        rec["latest"] = before              # 다시 계산한 값은 따로
        rec["created_at"] = prev.get("created_at")
        rec["source"] = prev.get("source") or rec["source"]
        for k in ("guardian_filtered", "guardian_correction", "before_uncorrected"):
            if k in prev:
                rec[k] = prev[k]
            elif k == "guardian_filtered":
                rec.pop(k, None)
    return ledger.write_baseline(root, rec)


def correct_guardian(root, sessions):
    """옛 기준선 정정(D4) — 0.1.4까지 이 PC 세션 직접 읽기가 Codex 자동 승인 심사 요청을 사람 지시로 셌다.
    표시(guardian_filtered)가 없는 기준선이면, 이번에 다시 읽은 세션 중 설치 전 것으로 "하네스 전"을 다시 잰다.
    다른 PC 묶음(--sessions-dir)·사람 태깅으로 잰 기준선은 처음부터 심사 요청을 뺐으니 표시만 단다.
    반환: None(정정할 것 없음) | {"status": "corrected"|"marked"|"no_sessions", ...}"""
    from vmc_harness.bootstrap import remeasure
    bl = ledger.read_baseline(root)
    if not bl or not bl.get("before") or bl.get("guardian_filtered"):
        return None
    if (bl.get("source") or {}).get("source") == "sessions_dir" or bl["before"].get("tagged"):
        bl["guardian_filtered"] = True
        bl["guardian_correction"] = {"status": "marked", "at": now_iso()}
        ledger.write_baseline(root, bl)
        return bl["guardian_correction"]
    since = remeasure.init_at(root)
    before_s = [s for s in sessions or [] if since and not remeasure._after(s.get("started_at"), since)]
    if not any(s.get("kind", "main") == "main" and s.get("human_prompts") for s in before_s):
        res = {"status": "no_sessions", "at": now_iso()}  # 설치 전 세션이 이 PC에 없다 — 다음 재진단 때 다시 시도
        bl["guardian_correction"] = res
        ledger.write_baseline(root, bl)
        return res
    old = bl["before"]
    new = compute(before_s)
    res = {"status": "corrected", "at": now_iso(),
           "human_prompts_before": old.get("human_prompts"), "human_prompts_after": new.get("human_prompts"),
           "guardian_sessions": sum(1 for s in before_s if s.get("kind") == "guardian"),
           "guardian_requests": sum(int(s.get("guardian_requests") or 0) for s in before_s),
           "interventions_median_before": (old.get("interventions_per_session") or {}).get("median"),
           "interventions_median_after": (new.get("interventions_per_session") or {}).get("median")}
    bl["before_uncorrected"] = {k: old.get(k) for k in ("period", "sessions", "human_prompts", "interventions_per_session",
                                                        "interventions_per_10_tool_calls")}
    bl["before"] = new
    bl["guardian_filtered"] = True
    bl["guardian_correction"] = res
    ledger.write_baseline(root, bl)
    return res
