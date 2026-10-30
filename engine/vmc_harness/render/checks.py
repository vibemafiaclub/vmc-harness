"""렌더링 기계 검사 2개 (spec 08 §렌더링 엔진). 표준 라이브러리만.

① 사실 보존: 입력(기계 기록)의 숫자·이름·id·날짜가 출력에 그대로 있는가
② 낯선 단어: 독자 어휘집에도 기본 한국어 목록에도 없는 단어

한국어 토큰화는 형태소 분석기 없이 한다: 어휘(명사·어간) 조각 + 조사·어미 꼬리로 나눌 수 있으면 아는 단어.
코드 이름(경로·식별자)과 숫자가 든 토큰은 단어가 아니라 이름이라 ②에서 보지 않는다(①이 본다).
"""

import re
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"

_TOKEN = re.compile(r"[0-9A-Za-z가-힣_][0-9A-Za-z가-힣_./:\-]*")
_HANGUL_RUN = re.compile(r"[가-힣]+")
_LATIN_RUN = re.compile(r"[A-Za-z]+")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")
_ID_LIKE = re.compile(r"^[A-Z]{1,3}-\d{6}-[a-z0-9]{4,}$")
_CAMEL = re.compile(r"[a-z][A-Z]")

# 조사·어미·활용 꼬리로 쓰이는 음절 (조각 뒤에 붙는 꼬리는 이 음절로만 이뤄져야 한다)
TAIL_SYLLABLES = set(
    "이가은는을를에의도로으와과랑만서까지부터께한테처럼보다요어아여고니면며게기음임함됨"
    "합됩입습었았였했됐겠시세셨죠네데던든단란냐나라래대해돼혀려켜쳐져겨워봐줘꿔뤄춰둬"
    "할될갈올줄볼쓸알을며요죠뿐씩야거걸건게예네엔엘께"
)
# 조각의 마지막 음절이 활용으로 바뀐 꼴 → 어간 끝 음절
CONTRACTION = {
    "해": "하", "했": "하", "돼": "되", "됐": "되", "봐": "보", "봤": "보", "줘": "주", "줬": "주",
    "써": "쓰", "썼": "쓰", "와": "오", "왔": "오", "갔": "가", "꿔": "꾸", "꿨": "꾸", "둬": "두",
    "뒀": "두", "져": "지", "졌": "지", "혀": "히", "혔": "히", "려": "리", "렸": "리", "켜": "키",
    "켰": "키", "쳐": "치", "쳤": "치", "겨": "기", "겼": "기", "워": "우", "웠": "우", "춰": "추",
    "췄": "추", "녀": "니", "냈": "내", "몰": "모", "꺼": "끄", "껐": "끄", "떠": "뜨", "떴": "뜨",
}
_STRIP_JONG = (4, 8, 16, 17, 20)  # ㄴ ㄹ ㅁ ㅂ ㅆ — 관형·미래·명사형·합쇼·과거 꼬리가 붙은 받침

# 어디서나 읽히는 영문 약어(독자 어휘집 없이도 통과)
BASIC_LATIN = {
    "ok", "sql", "db", "api", "ui", "qa", "id", "url", "pdf", "html", "json", "git", "pr", "ai",
    "it", "pc", "cli", "csv", "excel", "css", "js", "ts", "py", "os", "mac", "windows", "claude",
    "codex", "vmc",
}

_basic_cache = {}


def basic_words():
    """기본 한국어 목록 (data/basic_ko.txt)."""
    if "w" not in _basic_cache:
        words = set()
        p = DATA_DIR / "basic_ko.txt"
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.split("#", 1)[0].strip()
            if s:
                words.add(s)
        _basic_cache["w"] = words
    return _basic_cache["w"]


# ---------------- 음절 도구 ----------------

def _is_hangul(ch):
    return "가" <= ch <= "힣"


def _jong(ch):
    return (ord(ch) - 0xAC00) % 28 if _is_hangul(ch) else 0


def _strip_jong(ch):
    j = _jong(ch)
    return chr(ord(ch) - j) if j in _STRIP_JONG else None


def _seg_variants(seg):
    """조각과, 마지막 음절이 활용으로 바뀐 꼴을 어간으로 되돌린 후보들."""
    out = [seg]
    last = seg[-1]
    if last in CONTRACTION:
        out.append(seg[:-1] + CONTRACTION[last])
    s = _strip_jong(last)
    if s:
        out.append(seg[:-1] + s)
        if s in CONTRACTION:
            out.append(seg[:-1] + CONTRACTION[s])
    return out


def _all_tail(s, max_len):
    return len(s) <= max_len and all(ch in TAIL_SYLLABLES for ch in s)


def hangul_known(tok, vocab, max_tail=6):
    """조각(어휘) 1개 이상 + 꼬리(조사·어미)로 나눌 수 있으면 True."""
    n = len(tok)
    if n <= 1:
        return True
    if tok in vocab:
        return True
    # reach[i]: 0=못 닿음, 1=1음절 조각으로 닿음(뒤엔 꼬리만), 2=2음절 이상 조각으로 닿음(뒤에 조각 더 가능)
    # 1음절 조각(어간 '막'·'보' 등) 뒤에 다른 조각을 잇지 않는다 — '보+장'처럼 우연히 맞는 걸 막는다
    reach = [0] * (n + 1)
    reach[0] = 2
    for i in range(n):
        if reach[i] != 2:
            continue
        for j in range(i + 1, n + 1):
            seg = tok[i:j]
            if any(v in vocab for v in _seg_variants(seg)):
                kind = 1 if len(seg) == 1 else 2
                reach[j] = max(reach[j], kind)
    for i in range(1, n + 1):
        if reach[i] and (i == n or _all_tail(tok[i:], max_tail)):
            return True
    # 조사 덩어리만(예: '에서', '부터')
    return _all_tail(tok, 2)


def strip_tail(tok, vocab=None):
    """표시용: 뒤쪽 꼬리 음절을 뗀 줄기(낯선 단어 보고에 쓴다)."""
    i = len(tok)
    while i > 2 and tok[i - 1] in TAIL_SYLLABLES:
        i -= 1
    return tok[:i]


# ---------------- 토큰 분류 ----------------

def _is_name_like(tok):
    """코드 이름·경로·id·숫자가 든 토큰 — 단어 검사 대상이 아니다."""
    if any(c.isdigit() for c in tok):
        return True
    if any(c in tok for c in "_./:"):
        return True
    latin = "".join(_LATIN_RUN.findall(tok))
    if latin and _CAMEL.search(latin):
        return True
    if latin and len(latin) >= 2 and latin.isupper() and latin.lower() not in BASIC_LATIN:
        return False  # 대문자 약어는 낯선 단어 후보로 본다(독자가 쓰면 통과)
    return False


def tokens(text):
    return [t.rstrip(".:-/") for t in _TOKEN.findall(text or "") if t.rstrip(".:-/")]


class Vocab:
    """검사용 어휘: 아는 말(words, 소문자 영문 포함), 피하는 말(avoid), 독자가 안 쓰는 동의어(discouraged → 선호어)."""

    def __init__(self, words=(), avoid=(), discouraged=None):
        self.words = set(basic_words()) | set(words)
        self.latin = {w.lower() for w in self.words if _LATIN_RUN.fullmatch(w or "")} | BASIC_LATIN
        self.avoid = {a for a in avoid if a}
        self.discouraged = dict(discouraged or {})  # 변형 → 선호어

    def add(self, words):
        for w in words:
            if not w:
                continue
            self.words.add(w)
            if _LATIN_RUN.fullmatch(w):
                self.latin.add(w.lower())


def unknown_words(text, vocab, exempt=()):
    """낯선 단어 목록(중복 없이, 나온 순서). exempt: 사실 값 등 검사에서 뺄 말."""
    exempt_l = {e.lower() for e in exempt if e}
    out, seen = [], set()

    def flag(w):
        if w and w not in seen:
            seen.add(w)
            out.append(w)

    low = (text or "").lower()
    # 독자가 안 쓰는 동의어(선호어가 따로 있는 개념) — 여러 단어 변형('저장 프로시저')도 본다
    for variant in sorted(vocab.discouraged, key=len, reverse=True):
        v = variant.lower()
        if _LATIN_RUN.fullmatch(variant):
            if re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(v), low):
                flag(variant)
        elif re.search(r"(?<![가-힣])%s" % re.escape(v), low):
            flag(variant)

    for tok in tokens(text):
        if tok.lower() in exempt_l or _is_name_like(tok):
            continue
        for a in vocab.avoid:
            if tok.startswith(a) or tok.lower() == a.lower():
                flag(a)
        for run in _LATIN_RUN.findall(tok):
            r = run.lower()
            if len(r) <= 1 or r in exempt_l or r in vocab.latin:
                continue
            if r in {d.lower() for d in vocab.discouraged}:
                continue
            flag(run)
        for run in _HANGUL_RUN.findall(tok):
            if run in exempt_l:
                continue
            if any(run.startswith(d) for d in vocab.discouraged):
                continue
            if not hangul_known(run, vocab.words):
                flag(strip_tail(run))
    return out


# ---------------- ① 사실 보존 ----------------

NAME_KEYS = re.compile(r"(^|_)(name|names|title|path|paths|file|files|module|modules|table|tables|sp|"
                       r"target|targets|page|pages|gate|gates|card|author|who|user|id|ids)$", re.I)


def _num_alts(v):
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v)
    alts = [s]
    if isinstance(v, int) and abs(v) >= 1000:
        alts.append("{:,}".format(v))
    return alts


def _date_alts(m):
    y, mo, d = m.group(1), m.group(2), m.group(3)
    mi, di = str(int(mo)), str(int(d))
    return ["%s-%s-%s" % (y, mo, d), "%s/%s" % (mi, di), "%s/%s" % (mo, d), "%s월 %s일" % (mi, di),
            "%s월%s일" % (mi, di), "%s.%s" % (mi, di), "%s.%s.%s" % (y, mo, d), "%s-%s" % (mo, d)]


_BACKTICK = re.compile(r"`([^`\n]{1,80})`")


_TERM_KEYS = re.compile(r"(?i)(cmd|command|tool|명령|도구)")


def _ascii_term(s, key=""):
    """한글 없는 짧은 영문 명령·도구 표기(예 sqlcmd, vmc-harness db query)는 그대로 남아야 하는 사실.
    일반 영단어(approve 등)까지 잡지 않게 — 하이픈·공백이 있거나 명령·도구 키의 값일 때만."""
    if not (len(s) <= 60 and s.isascii() and any(c.isalpha() for c in s) and s.count(" ") <= 4):
        return False
    return ("-" in s) or (" " in s) or bool(_TERM_KEYS.search(key or ""))


def _code_like(s):
    return (" " not in s and len(s) <= 120 and
            (any(c.isdigit() for c in s) or any(c in s for c in "_./\\") or bool(_CAMEL.search(s))
             or bool(_ID_LIKE.match(s))))


def required_facts(facts, _key=""):
    """사실 dict → [(표시값, [허용 표기…])]. 숫자·날짜·id·코드 이름·이름 키 값.
    '_keep': 꼭 남길 문자열 목록. 그 밖의 '_'로 시작하는 키는 보지 않는다. 긴 서술 문장은 보지 않는다(풀어 써도 된다)."""
    out = []
    if isinstance(facts, dict):
        for k, v in facts.items():
            if k == "_keep":
                for s in (v or []):
                    out.append((str(s), [str(s)]))
                continue
            if str(k).startswith("_"):
                continue
            out.extend(required_facts(v, str(k)))
        return out
    if isinstance(facts, (list, tuple)):
        for v in facts:
            out.extend(required_facts(v, _key))
        return out
    if isinstance(facts, bool) or facts is None:
        return out
    if isinstance(facts, (int, float)):
        return [(str(facts), _num_alts(facts))]
    if isinstance(facts, str):
        s = facts.strip()
        if not s:
            return out
        m = _ISO_DATE.match(s)
        if m:
            return [(s, _date_alts(m))]
        if _code_like(s) or _ascii_term(s, _key):
            return [(s, [s])]
        ticks = _BACKTICK.findall(s)
        if ticks:
            out.extend((t, [t]) for t in ticks)
        if NAME_KEYS.search(_key) and len(s) <= 40 and s.count(" ") <= 3:
            return [(s, [s])]
    return out


def _present(alt, text, low):
    if re.fullmatch(r"-?[\d,]+(\.\d+)?", alt):
        return re.search(r"(?<![\d.])%s(?![\d])" % re.escape(alt), text) is not None
    if _LATIN_RUN.search(alt) and not any(c.isdigit() for c in alt) and " " not in alt and not any(c in alt for c in "_./"):
        return alt.lower() in low
    return alt in text


def missing_facts(text, facts):
    text = text or ""
    low = text.lower()
    miss = []
    for shown, alts in required_facts(facts):
        if not any(_present(a, text, low) for a in alts):
            if shown not in miss:
                miss.append(shown)
    return miss


def fact_strings(facts):
    """낯선 단어 검사에서 뺄 말(사실 값 그 자체)."""
    out = []
    for shown, alts in required_facts(facts):
        out.extend(alts)
        out.extend(tokens(shown))
    return out


def run(text, facts, vocab):
    miss = missing_facts(text, facts)
    unk = unknown_words(text, vocab, exempt=fact_strings(facts))
    return {"facts_ok": not miss, "missing_facts": miss, "unknown_words": unk}
