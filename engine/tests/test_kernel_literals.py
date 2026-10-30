"""커널 CI — 게이트 코드에 설정 밖 숫자 리터럴이 없어야 한다 (spec 11, 01 P6·P7).

대상: 게이트 검사 로직(`gates/`), DB 안전장치(`db/`), 훅 디스패처·어댑터(`hooks/`). 생성 게이트 로더(`gates/generated.py`) 포함.
판정은 ast로 한다. 문턱·상한·시간 같은 값은 `core/config.get()`으로 읽고 `config/<영역>.toml`에 단위·근거 메타와 함께 둔다.

의미 없는 숫자(허용):
- 0·1·-1
- 인덱스·슬라이스 경계(`x[2]`, `s[:16]`), `range()` 인자
- 단위 환산 곱·나눗셈(60·3600·86400·1000·1024), 비트 시프트(버퍼 크기)
- 출력 자릿수·식별자 길이·정규식 그룹 번호 등 형식 인자(`round(x, 3)`, `os.urandom(3)`, `m.group(2)`, `s.split(x, 2)`)
- 토큰 위치 산술(`i += 2`, `idx + 2` — 2·3만), `len(x) == 2`처럼 길이와 비교하는 2·3
- 아래 EXEMPT 표(제품 사실·열거 번호 — 이유를 적는다). 표의 항목이 더 이상 걸리지 않으면 표에서 지운다(낡은 예외 금지)
"""

import ast
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "vmc_harness"
SCAN = ("gates", "db", "hooks")

UNIT_CONSTANTS = {60, 3600, 86400, 1000, 1024}
FORMAT_CALLS = {"range", "round", "urandom", "group", "split", "rsplit", "exit", "_exit", "token_hex", "zfill"}
SMALL_POSITIONAL = {2, 3}

# (패키지 기준 경로, 그 줄에 있는 글자, 값, 이유)
EXEMPT = [
    ("db/facts.py", "COMPAT_MIN", None, "MSSQL 제품 사실 — 함수별 최소 호환성 수준(130·140·160). 설정이 아니다"),
    ("db/facts.py", '("TRANSLATE", 140)', None, "같은 표의 이어지는 줄"),
    ("gates/meta.py", "PROFILE_STRICTNESS", None, "프로파일 엄격도 순서(열거)"),
    ("gates/meta.py", "root, task_id, 2, decision_text", 2, "사람 개입 기준 번호(spec 00 기준 2 = 안전 경계 넓히기)"),
    ("gates/protected.py", 'root, task["task_id"], 2,', 2, "사람 개입 기준 번호(spec 00 기준 2)"),
    ("gates/rules.py", "ord(c) < 128", 128, "ASCII 경계(문자 코드 사실)"),
    ("db/sqlguard.py", "run_dots >= 3", 3, "T-SQL 네 부분 이름(서버.DB.스키마.개체)의 점 3개 — 문법 사실"),
]


def _parents(tree):
    par = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            par[c] = n
    return par


def _call_name(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _is_len_call(node):
    return isinstance(node, ast.Call) and _call_name(node) == "len"


def _allowed(node, par):
    v = node.value
    if v in (0, 1):
        return True
    cur, p = node, par.get(node)
    if isinstance(p, ast.UnaryOp) and isinstance(p.op, ast.USub):
        if v == 1:
            return True
        cur, p = p, par.get(p)
    # 인덱스·슬라이스 안
    a, child = p, cur
    while a is not None:
        if isinstance(a, ast.Subscript) and child is not a.value:
            return True
        if isinstance(a, (ast.stmt, ast.Call, ast.Lambda)):
            break
        child, a = a, par.get(a)
    if isinstance(p, ast.Call) and cur in p.args and _call_name(p) in FORMAT_CALLS:
        return True
    if isinstance(p, ast.BinOp):
        if isinstance(p.op, (ast.Mult, ast.Div, ast.FloorDiv)) and v in UNIT_CONSTANTS:
            return True
        if isinstance(p.op, (ast.LShift, ast.RShift)):
            return True
        if isinstance(p.op, (ast.Add, ast.Sub)) and v in SMALL_POSITIONAL:
            return True
    if isinstance(p, ast.IfExp):
        p = par.get(p)
    if isinstance(p, ast.AugAssign) and isinstance(p.op, (ast.Add, ast.Sub)) and v in SMALL_POSITIONAL:
        return True
    if isinstance(p, ast.Compare) and v in SMALL_POSITIONAL:
        sides = [p.left] + list(p.comparators)
        if any(_is_len_call(s) for s in sides):
            return True
    return False


def scan():
    """[(rel, lineno, value, line)] — 허용 규칙 밖 숫자 리터럴."""
    out = []
    for sub in SCAN:
        for f in sorted((PKG / sub).rglob("*.py")):
            src = f.read_text(encoding="utf-8")
            lines = src.splitlines()
            tree = ast.parse(src)
            par = _parents(tree)
            for n in ast.walk(tree):
                if not (isinstance(n, ast.Constant) and type(n.value) in (int, float)):
                    continue
                # 독스트링·문자열은 Constant(str)라 여기 안 온다
                if _allowed(n, par):
                    continue
                out.append((f.relative_to(PKG).as_posix(), n.lineno, n.value, lines[n.lineno - 1].strip()))
    return out


def _exempt(hit):
    rel, _, value, line = hit
    for i, (path, snippet, val, _) in enumerate(EXEMPT):
        if rel == path and snippet in line and (val is None or val == value):
            return i
    return None


class KernelLiteralTest(unittest.TestCase):
    def test_no_literals_outside_config(self):
        bad = [h for h in scan() if _exempt(h) is None]
        msg = "\n".join("%s:%d  %r  %s" % h for h in bad)
        self.assertEqual(bad, [], "게이트 코드에 설정 밖 숫자 리터럴:\n" + msg)

    def test_exempt_table_not_stale(self):
        used = {_exempt(h) for h in scan()} - {None}
        stale = [EXEMPT[i][:2] for i in range(len(EXEMPT)) if i not in used]
        self.assertEqual(stale, [], "더 이상 걸리지 않는 예외 — 표에서 지운다")

    def test_detector_catches_threshold(self):
        tree = ast.parse("def f(x):\n    if x > 30:\n        return x[:16]\n    return round(x, 3) * 1000\n")
        par = _parents(tree)
        vals = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and not _allowed(n, par)]
        self.assertEqual(vals, [30])


if __name__ == "__main__":
    unittest.main()
