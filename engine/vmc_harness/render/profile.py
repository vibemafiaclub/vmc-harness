"""독자 프로필 — 어휘집·문장 견본 (spec 08 §렌더링 엔진).

출처는 그 사람이 직접 쓴 글만(개발자 = 세션 프롬프트). 로컬 `reader-profile.json`(PC 안, 장부에 안 감).
- preferred: 같은 개념 중 가장 많이 쓴 말(동의어 그룹 — 엔진 내장 + lexicon.json 확장)과 실제 표기('sp' 소문자 등)
- vocab: 그 사람이 쓴 단어(낯선 단어 검사에서 통과)
- objects: 업무 대상을 부르는 이름(테이블·sp·파일 등 코드 이름)
- samples: 짧은 문장 견본 — 단어 선택과 결론부터 말하는 순서용. 말투·반말은 흉내 내지 않는다(재작성 지시에 명시)
- avoid: 독자가 "이게 뭔 말이야"라고 한 말
부트스트랩이 compute(root)를 부르고, 세션이 쌓이면(handlers.on_session_end) 다시 계산한다.
"""

import re
from collections import Counter

from vmc_harness.core import config
from vmc_harness.core.clock import now_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.mask import mask
from vmc_harness.render import checks

PROFILE_FILE = "reader-profile.json"

# 엔진 내장 동의어 그룹. 첫 항목이 그룹 키. 확장은 `.harness/lexicon.json`의 groups.
BUILTIN_GROUPS = [
    ["페이지", "화면"],
    ["적용", "반영"],
    ["원복", "롤백", "되돌리"],
    ["pass", "통과"],
    ["hold", "보류"],
    ["sp", "저장 프로시저", "프로시저", "stored procedure"],
    ["패치 sql", "마이그레이션", "migration"],
    ["배포", "디플로이", "deploy"],
    ["플래그", "flag", "토글"],
    ["초기화", "리셋", "reset"],
    ["오류", "에러", "error"],
    ["컬럼", "column"],
    ["쿼리", "query"],
    ["테이블", "table"],
    ["삭제", "제거"],
    ["커밋", "commit"],
    ["브랜치", "branch"],
    ["로그", "log"],
    ["테스트", "test"],
    ["작업", "태스크", "task"],
    ["승인", "approve"],
    ["거절", "reject"],
]

_LATIN_ONLY = re.compile(r"^[A-Za-z][A-Za-z ]*$")


def _pattern(variant):
    """변형 하나를 찾는 정규식(실제 표기를 잡도록 대소문자 무시). 여러 단어는 공백 0개 이상 허용."""
    body = r"\s*".join(re.escape(p) for p in variant.split())
    if _LATIN_ONLY.match(variant):
        return re.compile(r"(?<![A-Za-z])%s(?![A-Za-z])" % body, re.I)
    return re.compile(r"(?<![가-힣])%s" % body, re.I)


def count_groups(prompts, groups):
    """[{key, counts:{변형:수}, surface:{변형:가장 많이 쓴 표기}}]"""
    out = []
    for g in groups:
        counts, surface = {}, {}
        for v in g:
            pat = _pattern(v)
            forms = Counter()
            for t in prompts:
                for m in pat.finditer(t):
                    forms[m.group(0)] += 1
            counts[v] = sum(forms.values())
            if forms:
                surface[v] = forms.most_common(1)[0][0]
        out.append({"key": g[0], "counts": counts, "surface": surface})
    return out


def _sample_ok(text, max_chars):
    t = text.strip()
    return (4 <= len(t) <= max_chars and "\n" not in t and not t.startswith(("<", "/", "[", "{"))
            and "vmc-harness:" not in t)


def build(prompts, groups=None, name=None, root=None):
    """프롬프트 원문 목록 → 프로필 dict (순수 함수)."""
    prompts = [p for p in (prompts or []) if isinstance(p, str) and p.strip()]
    groups = groups if groups is not None else BUILTIN_GROUPS
    vocab_max = config.get("render.profile_vocab_max", root)
    obj_max = config.get("render.profile_objects_max", root)
    n_samples = config.get("render.profile_samples", root)
    sample_chars = config.get("render.sample_max_chars", root)
    obj_min = config.get("render.profile_object_min_count", root)

    preferred = {}
    for g in count_groups(prompts, groups):
        total = sum(g["counts"].values())
        if not total:
            continue
        best = max(g["counts"].items(), key=lambda kv: kv[1])[0]
        preferred[g["key"]] = {"term": g["surface"].get(best, best), "variant": best, "counts": g["counts"]}

    vocab, objects = Counter(), Counter()
    for t in prompts:
        for tok in checks.tokens(t):
            if any(c.isdigit() for c in tok) and not any(c in tok for c in "_./"):
                continue
            if checks._is_name_like(tok):
                objects[tok] += 1
                continue
            for run in checks._LATIN_RUN.findall(tok):
                if len(run) > 1:
                    vocab[run.lower()] += 1
            for run in checks._HANGUL_RUN.findall(tok):
                if len(run) > 1:
                    vocab[checks.strip_tail(run)] += 1
    samples = []
    seen = set()
    cands = [mask(p.strip()) for p in prompts if _sample_ok(p, sample_chars)]
    if cands:
        step = max(1, len(cands) // max(1, n_samples))
        for s in cands[::-1][::step]:
            if s not in seen:
                seen.add(s)
                samples.append(s)
            if len(samples) >= n_samples:
                break
    return {
        "version": 1, "name": name, "computed_at": now_iso(),
        "source": {"prompts": len(prompts)},
        "preferred": preferred,
        "vocab": dict(vocab.most_common(vocab_max)),
        "objects": [o for o, c in objects.most_common(obj_max) if c >= obj_min],
        "samples": samples,
        "avoid": [],
    }


def discouraged(profile):
    """독자가 한 번도 안 쓴 동의어 → 그 사람의 선호어. 낯선 단어 검사에 쓴다."""
    out = {}
    for key, p in (profile or {}).get("preferred", {}).items():
        for v, c in (p.get("counts") or {}).items():
            if not c and v != p.get("variant"):
                out[v] = p.get("term")
    return out


def words(profile):
    """검사에서 아는 말로 칠 말: 쓴 단어 + 업무 대상 이름 + 선호어."""
    p = profile or {}
    out = set(p.get("vocab") or {})
    out.update(p.get("objects") or [])
    for x in (p.get("preferred") or {}).values():
        out.add(x.get("term"))
        for v, c in (x.get("counts") or {}).items():
            if c:
                out.add(v)
                out.update(v.split())
    return {w for w in out if w}


# ---- 로컬 저장 ----

def _path(root):
    from vmc_harness.core.project import local_dir
    return local_dir(root) / PROFILE_FILE


def load(root):
    if not root:
        return None
    try:
        return read_json(_path(root))
    except RuntimeError:
        return None


def save(root, profile):
    old = load(root) or {}
    profile["avoid"] = sorted(set(old.get("avoid") or []) | set(profile.get("avoid") or []))
    write_json(_path(root), profile)
    return profile


def git_user(root):
    """이 PC 사람 이름 — 기준은 core.project.author_name 하나(brain 작성자 비교와 같은 값)."""
    from vmc_harness.core.project import author_name
    return author_name(root)


def collect_prompts(root, limit=None):
    """이 PC의 이 프로젝트 세션(Claude·Codex)에서 사람 프롬프트 원문. 세션 파서가 없으면 빈 목록."""
    try:
        from vmc_harness import sessions
    except ImportError:
        return [], 0
    limit = limit or config.get("bootstrap.max_sessions", root)
    prompts, n = [], 0
    for agent, path in sessions.find(root, limit=limit):
        s = sessions.parse(path, agent)
        if not getattr(s, "measured", False) and not getattr(s, "human_prompts", None):
            continue
        n += 1
        for hp in s.human_prompts or []:
            t = (hp or {}).get("text") if isinstance(hp, dict) else None
            if t:
                prompts.append(t)
    return prompts, n


def compute(root, prompts=None):
    """프로필 재계산 → 로컬 저장, 업무 대상 이름은 프로젝트 어휘집(lexicon.json)에 합친다."""
    from vmc_harness.render import lexicon
    n_sessions = None
    if prompts is None:
        prompts, n_sessions = collect_prompts(root)
    prof = build(prompts, groups=lexicon.groups(root), name=git_user(root), root=root)
    prof["source"]["sessions"] = n_sessions
    save(root, prof)
    if prof.get("objects"):
        lexicon.merge_terms(root, prof["objects"], kind="object")
    return prof


def add_avoid(root, word):
    p = load(root) or {"version": 1, "preferred": {}, "vocab": {}, "objects": [], "samples": [], "avoid": []}
    av = set(p.get("avoid") or [])
    av.add(word)
    p["avoid"] = sorted(av)
    write_json(_path(root), p)
    return p
