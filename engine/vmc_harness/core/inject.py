"""작업 중 에이전트에게 주입할 수 있는 메시지 허용 목록 (spec 01 P5).

훅 디스패처는 이 함수로만 주입 메시지를 만든다. 허용 목록 밖 유형은 버린다.
- gate_violation   : 안전·생성 게이트 위반(무엇을 어겼는지만 — 해결 방법 지시 금지, P6)
- missing_evidence : 종료 시점 "빠진 증거"
- pm_opinion       : PM 의견(선언 범위 밖 첫 편집 직전 1회)
- declare_context  : 착수 시점 정보(브레인 카드, 비슷한 과거 작업 실측) — 착수 때만 허용
- term_context     : 사람이 방금 쓴 프로젝트 용어의 뜻(UserPromptSubmit — 사람 말을 풀어 줄 뿐 압박·지시가 아니다.
                     같은 세션 같은 용어 1회, 숫자 압박 거르기는 그대로 — spec 08 §렌더링 엔진 양방향)
"""

import re

ALLOWED = ("gate_violation", "missing_evidence", "pm_opinion", "declare_context", "term_context")
# 작업 중(declare_context 제외)에는 예측·예산·실측 숫자를 싣지 않는다
# (예측 분위 · 예산이라는 말 · 토큰 수 · 시간 수 · 퍼센트). 파일 경로 속 낱말(예 `auth/token.py`)은 숫자가 없으면 걸리지 않는다
_NUMERIC_PRESSURE = re.compile(
    r"(\bP(?:30|50|80)\b"
    r"|예산|\bbudget\b"
    r"|\d[\d,.]*\s*[kKmM천만]?\s*(?:토큰|\btokens?\b)"
    r"|(?:토큰|\btokens?\b)\s*[:=]?\s*\d"
    r"|(?:초|분)\s*남"
    r"|\d+(?:\.\d+)?\s*%)", re.I)
# 시간 숫자(경과·실측). PM 의견은 "X 부분은 N분 안에 끝나길 희망"이 출력 형식이라(spec 03) 이 검사에서 뺀다
_TIME_NUMBER = re.compile(r"\d+(?:\.\d+)?\s*(?:초|분|시간)(?!기)")


def message(kind, text):
    """허용되면 '[vmc-harness:<kind>] text', 아니면 None."""
    if kind not in ALLOWED:
        return None
    if kind != "declare_context" and _NUMERIC_PRESSURE.search(text or ""):
        return None
    if kind in ("gate_violation", "missing_evidence") and _TIME_NUMBER.search(text or ""):
        return None
    return "[vmc-harness:%s] %s" % (kind, text)
