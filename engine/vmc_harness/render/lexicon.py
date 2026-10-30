"""업무 용어 어휘집 (spec 08 §렌더링 엔진 — 저장·학습).

- 프로젝트 공용 L1 `.harness/lexicon.json`: 업무 용어(terms), 동의어 그룹 확장(groups), 팀이 낯설다고 한 말(avoid),
  "이게 뭔 말이야" 후보(candidates — 단어와 횟수만, 원문·사람 이름 없음)
- 개인 표현은 로컬 `reader-profile.json`(profile.py)
- 학습: 독자가 "이게 뭔 말이야" → 그 사람 프로필 avoid에 바로, 프로젝트 후보 횟수 +1,
  `render.lexicon_unclear_min`회 이상이면 프로젝트 avoid로. 영향은 어휘집 안에서만(규칙·게이트로 안 간다)

양방향 — 사람 → 에이전트(term_context):
- 사람 프롬프트에 프로젝트 용어가 나오면 그 뜻 한 줄을 에이전트 컨텍스트에 넣는다(core.inject.message("term_context")).
  대상: 모호어(ambiguous — 한 단어가 여러 대상을 가리킴: 기본 해석 + 대안), 프로젝트 동의어 그룹(groups — 엔진 내장 그룹 제외),
  뜻이 적힌 용어(terms[x].means). 같은 세션 같은 용어는 1회, 한 프롬프트 `render.term_inject_max`줄까지. 문자열 매칭만(LLM 없음)
- 모호어 출처: ①기계 사실 — 'DB'·'디비'·'데이터베이스'는 db-credential로 등록한 개발 DB(query 프로필)가 기본, 운영은
  `.harness/config.json` db.prod_names. 어휘집에 복사하지 않고 읽을 때마다 원본에서 만든다(원본 하나)
  ②사람 정정 — "'X'는 Y를 말한 거야" 꼴을 정규식으로 잡아 후보(meaning_candidates — 단어·뜻·횟수만)로,
  같은 뜻이 `render.lexicon_correction_min`회 이상이면 모호어로(04 학습 후보 규칙과 같은 2회). 정정이 기계 사실 기본값보다 앞선다
"""

import re

from vmc_harness.core import config, paths
from vmc_harness.core.clock import now, now_iso, parse_iso
from vmc_harness.core.jsonio import read_json, write_json

def _empty():
    return {"version": 1, "terms": {}, "groups": [], "avoid": [], "candidates": {},
            "ambiguous": {}, "meaning_candidates": {}}


def _path(root):
    return paths.harness_dir(root) / "lexicon.json"


def load(root):
    if not root:
        return _empty()
    data = read_json(_path(root), None) or {}
    out = _empty()
    out.update(data)
    return out


def save(root, data):
    write_json(_path(root), data)
    return data


def groups(root):
    from vmc_harness.render.profile import BUILTIN_GROUPS
    extra = [g for g in load(root).get("groups") or [] if isinstance(g, list) and len(g) >= 2]
    return [list(g) for g in BUILTIN_GROUPS] + extra


def merge_terms(root, terms, kind="term"):
    if not root:
        return None
    data = load(root)
    t = data.setdefault("terms", {})
    changed = False
    for term in terms:
        if term and term not in t:
            t[term] = {"kind": kind, "added_at": now_iso()}
            changed = True
    if changed:
        save(root, data)
    return data


# "'X'가 뭔 말이야" / "X 무슨 뜻이야" — 지시어(이게·그거…)만 있으면 단어를 모르므로 기록하지 않는다
_QUOTED = re.compile(r"['\"‘“`「]([^'\"’”`」]{1,30})['\"’”`」]\s*(?:이|가|은|는|란|이란|라는\s*게)?\s*"
                     r"(?:뭔\s*말|무슨\s*말|무슨\s*뜻|뭔\s*뜻|뭐야|뭐예요|뭐에요|뭔데|뭐지|뭔가요|이해가\s*안)")
_BARE = re.compile(r"(?:^|\s)([0-9A-Za-z가-힣_\-]{2,20}?)(?:이|가|은|는|란|이란)?\s+"
                   r"(?:뭔\s*말|무슨\s*말|무슨\s*뜻|뭔\s*뜻|뭐야|뭐예요|뭐에요|뭔데|뭔가요)")
_DEMONSTRATIVE = {"이게", "그게", "저게", "이거", "그거", "저거", "이건", "그건", "저건", "이것", "그것", "저것",
                  "이", "그", "저", "뭐", "무슨", "이 말", "그 말", "이말", "그말", "게"}


def detect_unclear(prompt):
    """사람 프롬프트에서 '모르겠다'고 한 단어. 없거나 지시어뿐이면 None."""
    if not prompt:
        return None
    m = _QUOTED.search(prompt)
    if m:
        w = m.group(1).strip()
        return w if w and w not in _DEMONSTRATIVE else None
    m = _BARE.search(prompt)
    if m:
        w = m.group(1).strip()
        return w if w and w not in _DEMONSTRATIVE else None
    return None


def unclear(root, word, source="command"):
    """독자가 모른다고 한 말 기록 → {'word','personal':True,'team_count':n,'team_avoid':bool}."""
    from vmc_harness.render import profile
    word = (word or "").strip()
    if not word:
        raise ValueError("단어 없음")
    profile.add_avoid(root, word)
    data = load(root)
    c = data.setdefault("candidates", {}).setdefault(word, {"count": 0, "first_at": now_iso()})
    c["count"] = int(c.get("count") or 0) + 1
    c["last_at"] = now_iso()
    c["source"] = source
    team_avoid = c["count"] >= config.get("render.lexicon_unclear_min", root)
    if team_avoid and word not in (data.get("avoid") or []):
        data["avoid"] = sorted(set(data.get("avoid") or []) | {word})
    save(root, data)
    return {"word": word, "personal": True, "team_count": c["count"], "team_avoid": word in (data.get("avoid") or [])}


def vocab_for(root, reader_profile):
    """검사용 checks.Vocab: 기본 목록 + 독자 어휘 + 프로젝트 업무 용어, 피하는 말, 독자가 안 쓰는 동의어."""
    from vmc_harness.render import checks, profile
    data = load(root)
    words = set(profile.words(reader_profile)) | set((data.get("terms") or {}).keys())
    avoid = set((reader_profile or {}).get("avoid") or []) | set(data.get("avoid") or [])
    words -= avoid
    return checks.Vocab(words=words, avoid=avoid, discouraged=profile.discouraged(reader_profile))


# ---------------------------------------------------------------- 사람 → 에이전트: 용어 뜻 주입(term_context)

DB_WORDS = ("DB", "디비", "데이터베이스", "database")

# "'X'는 Y를 말한 거야" / "여기서 DB는 운영 DB 말하는 거" / "X라고 하면 Y를 뜻하는 거야" — 뜻을 정해 주는 정정만
_CORRECTION = re.compile(
    r"(?:^|[\s,.!?(])['\"‘“`]?([0-9A-Za-z가-힣_\-]{1,20})['\"’”`]?\s*"
    r"(?:은|는|이란|란|이라고\s*하면|라고\s*하면|이라고\s*한\s*건|라고\s*한\s*건|이라는\s*건|라는\s*건)\s+"
    r"['\"‘“`]?([^\n'\"’”`.,?!]{1,30}?)['\"’”`]?\s*(?:을|를)?\s*"
    r"(?:말한|말하는|말했던|뜻한|뜻하는|얘기한|얘기하는|가리킨|가리키는|의미한|의미하는)\s*(?:거|것|건|게)")
_NOT_TERM = _DEMONSTRATIVE | {"나", "저", "내", "제", "우리", "너", "니", "난", "전", "여기", "거기", "아니", "그러니까"}


def detect_correction(prompt):
    """사람이 용어 뜻을 정해 준 말 → (단어, 뜻). 없으면 None."""
    if not prompt:
        return None
    m = _CORRECTION.search(prompt)
    if not m:
        return None
    word, means = m.group(1).strip(), m.group(2).strip()
    if not word or not means or word in _NOT_TERM or word == means:
        return None
    return word, means


def correct(root, word, means, source="prompt"):
    """정정 후보 기록 → {'word','means','count','promoted'}. 같은 뜻 render.lexicon_correction_min회 이상이면 모호어로."""
    data = load(root)
    cands = data.setdefault("meaning_candidates", {}).setdefault(word, {})
    c = cands.setdefault(means, {"count": 0, "first_at": now_iso()})
    c["count"] = int(c.get("count") or 0) + 1
    c["last_at"] = now_iso()
    c["source"] = source
    promoted = c["count"] >= config.get("render.lexicon_correction_min", root)
    if promoted:
        amb = data.setdefault("ambiguous", {})
        prev = amb.get(word) or {}
        alts = [a for a in (prev.get("alternatives") or []) if a != means]
        if prev.get("default") and prev["default"] != means and prev["default"] not in alts:
            alts.insert(0, prev["default"])
        amb[word] = {"default": means, "alternatives": alts, "aliases": prev.get("aliases") or [],
                     "source": "correction", "added_at": prev.get("added_at") or now_iso(), "updated_at": now_iso()}
    save(root, data)
    return {"word": word, "means": means, "count": c["count"], "promoted": promoted}


def _db_seed(root):
    """기계 사실 모호어 'DB': 기본 = 하네스에 등록된 개발 DB(db-credential query 프로필), 대안 = 운영 DB(db.prod_names)."""
    try:
        from vmc_harness.db import client
        dev = (client.load_db_json(root).get("query") or {}).get("database")
        prod = client.prod_names(root)
    except Exception:
        return None
    if not dev:
        return None
    return {"words": list(DB_WORDS), "default": "개발 DB(%s)" % dev,
            "alternatives": ["운영 DB(%s)" % "·".join(prod) if prod else "운영 DB"], "source": "machine_fact"}


_ASCII = re.compile(r"^[0-9A-Za-z_\-. ]+$")


def _hit(low, word):
    w = (word or "").strip().lower()
    if not w:
        return False
    if _ASCII.match(w):
        return re.search(r"(?<![0-9a-z_])" + re.escape(w) + r"(?![0-9a-z_])", low) is not None
    return w in low


def _quote(words):
    return "·".join("'%s'" % w for w in words)


def _josa(text, pair):
    """받침에 맞는 조사(pair = '은는' | '을를'). 괄호 안·따옴표는 건너뛰고, 한글이 아니면 받침 없음으로 본다."""
    s = re.sub(r"\([^)]*\)$", "", str(text)).rstrip("'\"’” ")
    c = s[-1:] if s else ""
    has = ("가" <= c <= "힣" and (ord(c) - ord("가")) % 28 != 0) or c in "013678LMNlmn"  # 영·일·삼·육·칠·팔, 엘·엠·엔
    return pair[0] if has else pair[1]


def _is(text):
    return "%s%s" % (text, _josa(text, "은는"))


def _obj(text):
    return "%s%s" % (text, _josa(text, "을를"))


def term_lines(root, prompt, skip=()):
    """프롬프트에 나온 프로젝트 용어 → [(key, 한 줄 사실)]. 모호어 → 프로젝트 동의어 그룹 → 뜻 적힌 용어 순."""
    if not root or not prompt:
        return []
    low = prompt.lower()
    skip = {s.lower() for s in skip}
    data = load(root)
    out, seen = [], set()
    amb = dict(data.get("ambiguous") or {})
    db_low = {w.lower() for w in DB_WORDS}
    if not any(k.lower() in db_low for k in amb) and any(_hit(low, w) for w in DB_WORDS):
        seed = _db_seed(root)
        if seed:
            amb["DB"] = seed
    for word, a in amb.items():
        if not isinstance(a, dict) or not a.get("default"):
            continue
        words = [word] + list(a.get("words") or []) + list(a.get("aliases") or [])
        if any(w.lower() in skip for w in words) or not any(_hit(low, w) for w in words):
            continue
        line = "이 프로젝트에서 %s 기본적으로 %s 뜻함" % (_is("'%s'" % word), _obj(a["default"]))
        alts = [x for x in a.get("alternatives") or [] if x]
        if alts:
            line += ". 다른 대상은 따로 부름: %s" % _quote(alts)
        out.append(("amb:" + word, line))
        seen |= {w.lower() for w in words}
    for g in data.get("groups") or []:
        if not isinstance(g, list) or len(g) < 2:
            continue
        if any(str(w).lower() in seen | skip for w in g) or not any(_hit(low, str(w)) for w in g):
            continue
        out.append(("grp:" + str(g[0]), "이 프로젝트에서 %s 같은 것을 가리킴" % _is(_quote(g))))
        seen |= {str(w).lower() for w in g}
    for term, t in (data.get("terms") or {}).items():
        means = t.get("means") if isinstance(t, dict) else None
        if not means or term.lower() in seen | skip or not _hit(low, term):
            continue
        out.append(("term:" + term, "이 프로젝트에서 %s %s 뜻함" % (_is("'%s'" % term), _obj(means))))
    return out


def _injected_path(root):
    from vmc_harness.core.project import local_dir
    return local_dir(root) / "lexicon-injected.json"


def term_context(root, session_id, prompt, skip=()):
    """주입할 term_context 문구(core.inject.message) 또는 None. 같은 세션 같은 용어는 1회, render.term_inject_max줄까지."""
    from vmc_harness.core import inject
    lines = term_lines(root, prompt, skip=skip)
    if not lines:
        return None
    p = _injected_path(root)
    st = read_json(p, {}) or {}
    sid = session_id or "-"
    cur = st.get(sid) if isinstance(st.get(sid), dict) else {"keys": []}
    fresh = [(k, l) for k, l in lines if k not in cur.get("keys", [])][:config.get("render.term_inject_max", root)]
    if not fresh:
        return None
    msg = inject.message("term_context", "\n".join(l for _, l in fresh))
    if not msg:
        return None
    cur = {"keys": cur.get("keys", []) + [k for k, _ in fresh], "at": now_iso()}
    t = now()
    # 하루 지난 세션 기록은 버린다
    st = {k: v for k, v in st.items() if k != sid and isinstance(v, dict) and parse_iso(v.get("at"))
          and (t - parse_iso(v["at"])).total_seconds() < 86400}
    st[sid] = cur
    write_json(p, st)
    return msg
