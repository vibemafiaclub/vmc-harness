"""렌더링 엔진 (spec 08 — 사람이 읽는 모든 글이 거친다).

흐름: 에이전트(또는 호출 측)가 독자 프로필을 보고 쓴 초안 → 기계 검사 2개(checks) →
실패할 때만 LLM(`claude -p`, render.model)으로 1회 다시 쓰고 다시 검사. 통과하면 추가 호출 없음.
- 규칙 원문(kind="rule")은 쓰지 않는다 — 원문 그대로 돌려준다(01 P3, 옮기다 뜻이 넓어진다)
- 말투·반말은 흉내 내지 않는다. 독자 문장 견본은 단어 선택과 결론부터 말하는 순서용
- 재작성은 편의 장치(01 P8): LLM이 없거나 실패하면 초안 그대로(검사 결과를 같이 돌려준다)

공개 함수: render(), evaluate() (회귀 세트 ③ — eval 영역이 부른다), load_reader()
"""

import hashlib
import json
from pathlib import Path

from vmc_harness.core import config, paths
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.render import checks, lexicon, profile

SYSTEM = (
    "목표: 아래 독자가 3분 안에 막힘없이 이해하는 한국어 글로 고쳐 쓴다.\n"
    "참고할 수 있는 것: 독자가 평소 쓰는 말(같은 개념이면 이 말을 쓴다), 독자가 직접 쓴 짧은 문장 견본"
    "(단어 선택과 결론부터 말하는 순서만 참고한다).\n"
    "제약: '그대로 남길 값'은 글자 하나 바꾸지 않고 남긴다. '쓰지 말 말'은 쓰지 않는다. 내용을 더하거나 빼지 않는다. "
    "견본의 말투·반말·줄임말은 따라 하지 않는다(존댓말 평서문). 줄 수는 초안과 같게.\n"
    "출력 형식: 고친 글 본문만. 설명·따옴표·머리말 없이."
)
WRITE_SYSTEM = (
    "목표: 아래 사실(기계 기록)을 독자가 3분 안에 막힘없이 이해하는 한국어 글로 쓴다. 결론부터.\n"
    "참고할 수 있는 것: 독자가 평소 쓰는 말, 독자가 직접 쓴 짧은 문장 견본(단어 선택과 순서만 참고).\n"
    "제약: '그대로 남길 값'은 글자 그대로 넣는다. '쓰지 말 말'은 쓰지 않는다. 사실에 없는 내용을 더하지 않는다. "
    "견본의 말투·반말은 따라 하지 않는다(존댓말 평서문).\n"
    "출력 형식: 글 본문만."
)


def load_reader(root, reader):
    """reader: dict | 프로필 json 경로 | 'me'/'local'/None(이 PC의 reader-profile.json) | 이름(~/.vmc-harness/readers/<이름>.json) | 'team'(공용 어휘만)."""
    if isinstance(reader, dict):
        return reader
    if reader in (None, "", "me", "local"):
        return profile.load(root) or {}
    if reader == "team":
        return {"name": "team"}
    p = Path(str(reader)).expanduser()
    if p.is_file():
        return read_json(p, {}) or {}
    named = paths.home() / "readers" / ("%s.json" % reader)
    if named.is_file():
        return read_json(named, {}) or {}
    return {}


def _passed(chk):
    return chk["facts_ok"] and not chk["unknown_words"]


def _better(new, old):
    """새 검사 결과가 낫나: 사실 보존이 먼저, 그다음 낯선 단어 수."""
    if new["facts_ok"] != old["facts_ok"]:
        return new["facts_ok"]
    return len(new["unknown_words"]) < len(old["unknown_words"])


def _reader_block(prof, vocab):
    pref = ["%s(%s 대신)" % (p["term"], "·".join(v for v, c in p["counts"].items() if v != p["variant"]))
            for p in (prof.get("preferred") or {}).values() if p.get("term")]
    lines = []
    if pref:
        lines.append("독자가 쓰는 말: " + ", ".join(pref))
    objs = (prof.get("objects") or [])[:20]
    if objs:
        lines.append("독자가 부르는 업무 대상 이름: " + ", ".join(objs))
    samples = prof.get("samples") or []
    if samples:
        lines.append("독자 문장 견본:\n" + "\n".join("- " + s for s in samples))
    return "\n".join(lines)


def _llm(system, prompt, root):
    from vmc_harness.core import llm
    try:
        r = llm.claude_p(prompt, model=config.get("render.model", root), system=system,
                         timeout=config.get("render.timeout_s", root))
    except Exception:
        return None
    if not r or not (r.get("text") or "").strip():
        return None
    return r["text"].strip().strip("`").strip()


def _rewrite(root, prof, vocab, facts, text, chk):
    keep = [s for s, _ in checks.required_facts(facts)]
    prompt = "\n\n".join(x for x in [
        _reader_block(prof, vocab),
        "그대로 남길 값: " + (", ".join(keep) if keep else "(없음)"),
        "쓰지 말 말: " + (", ".join(chk["unknown_words"]) if chk["unknown_words"] else "(없음)")
        + ("" if chk["facts_ok"] else "\n빠진 값(꼭 넣을 것): " + ", ".join(chk["missing_facts"])),
        "초안:\n" + text,
    ] if x)
    return _llm(SYSTEM, prompt, root)


def _write(root, prof, vocab, facts):
    keep = [s for s, _ in checks.required_facts(facts)]
    prompt = "\n\n".join(x for x in [
        _reader_block(prof, vocab),
        "그대로 남길 값: " + (", ".join(keep) if keep else "(없음)"),
        "사실(JSON):\n" + json.dumps(facts, ensure_ascii=False, indent=1),
    ] if x)
    return _llm(WRITE_SYSTEM, prompt, root)


def _fallback_text(facts):
    if isinstance(facts, dict):
        for k in ("text", "summary", "title"):
            if isinstance(facts.get(k), str) and facts[k].strip():
                return facts[k]
        parts = []
        for k, v in facts.items():
            if str(k).startswith("_") or isinstance(v, (dict, list)):
                continue
            parts.append("%s %s" % (k, v))
        return " · ".join(parts)
    return str(facts)


def _cache_key(prof, facts, draft, kind):
    blob = json.dumps({"r": prof.get("computed_at"), "n": prof.get("name"), "a": prof.get("avoid"),
                       "f": facts, "d": draft, "k": kind}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _cache_path(root, key):
    return paths.cache_dir(root) / "render" / ("%s.json" % key)


def render(root, reader, facts, draft=None, kind="notice", allow_llm=True, cache=False):
    """→ {"text","checks":{"facts_ok","missing_facts","unknown_words"},"rewritten":bool,"llm_calls":int}

    facts: 입력 기계 기록(숫자·이름·id·날짜는 출력에 그대로 있어야 한다. '_keep': 꼭 남길 말 목록).
    draft: 에이전트가 프로필을 보고 쓴 초안. 없으면 facts['text'] → 없으면 LLM이 쓴다(→ 없으면 사실 나열).
    kind="rule": 규칙 원문 — 쓰지 않고 그대로 돌려준다.
    allow_llm=False: 검사만(재작성 없음). cache=True: 같은 입력의 통과 결과를 `.harness/cache/render/`에서 다시 쓴다.
    """
    facts = facts if facts is not None else {}
    if allow_llm and root:
        from vmc_harness.core import budget
        allow_llm = budget.enabled(root, "render_rewrite")  # 가벼움 예산 초과면 검사만(01 P8)
    if kind == "rule":
        text = draft if draft is not None else (facts.get("text", "") if isinstance(facts, dict) else str(facts))
        return {"text": text, "checks": {"facts_ok": True, "missing_facts": [], "unknown_words": [], "skipped": "rule"},
                "rewritten": False, "llm_calls": 0}
    prof = load_reader(root, reader)
    key = None
    if cache and root:
        key = _cache_key(prof, facts, draft, kind)
        hit = read_json(_cache_path(root, key), None)
        if hit:
            hit["cached"] = True
            return hit
    vocab = lexicon.vocab_for(root, prof)
    calls = 0
    text = draft
    if text is None and isinstance(facts, dict) and isinstance(facts.get("text"), str):
        text = facts["text"]
    if text is None:
        if allow_llm:
            calls += 1
            text = _write(root, prof, vocab, facts)
        if not text:
            text = _fallback_text(facts)
    chk = checks.run(text, facts, vocab)
    rewritten = False
    if not _passed(chk) and allow_llm:
        calls += 1
        new = _rewrite(root, prof, vocab, facts, text, chk)
        if new:
            chk2 = checks.run(new, facts, vocab)
            if _better(chk2, chk) or (_passed(chk2) and not _passed(chk)):
                text, chk, rewritten = new, chk2, True
    out = {"text": text, "checks": chk, "rewritten": rewritten, "llm_calls": calls}
    # 검사를 통과했거나 재작성까지 해 본 결과만 남긴다(검사만 한 실패는 다음에 재작성할 수 있게)
    if key and (_passed(chk) or calls):
        write_json(_cache_path(root, key), out)
    return out


# ---- 회귀 세트 ③ (spec 11) — eval 영역이 부른다 ----

def evaluate_one(root, reader, case, allow_llm=True):
    """case: {"facts":{…}, "draft":"…"(선택), "kind":"notice"(선택), "reference":"독자 실제 문장"(선택)}
    → {"text","facts_ok","missing_facts","unknown_count","unknown_words","rewritten","llm_calls","reference"}"""
    r = render(root, reader, case.get("facts") or {}, draft=case.get("draft"),
               kind=case.get("kind") or "notice", allow_llm=allow_llm)
    return {"id": case.get("id"), "text": r["text"], "facts_ok": r["checks"]["facts_ok"],
            "missing_facts": r["checks"].get("missing_facts", []),
            "unknown_count": len(r["checks"].get("unknown_words", [])),
            "unknown_words": r["checks"].get("unknown_words", []),
            "rewritten": r["rewritten"], "llm_calls": r["llm_calls"], "reference": case.get("reference")}


def evaluate(root, reader, cases, allow_llm=True):
    """→ {"cases":[…], "summary":{"n","facts_ok_rate","unknown_total","unknown_zero_rate","llm_calls"}}.
    지표: 사실 보존 100%, 낯선 단어 0(11 회귀 세트 ③). 자연스러움은 사람이 표본을 읽어 판단."""
    rows = [evaluate_one(root, reader, c, allow_llm) for c in cases or []]
    n = len(rows)
    return {"cases": rows, "summary": {
        "n": n,
        "facts_ok_rate": round(sum(1 for r in rows if r["facts_ok"]) / n, 3) if n else None,
        "unknown_total": sum(r["unknown_count"] for r in rows),
        "unknown_zero_rate": round(sum(1 for r in rows if not r["unknown_count"]) / n, 3) if n else None,
        "llm_calls": sum(r["llm_calls"] for r in rows),
    }}
