"""고객 텍스트 파일(AGENTS.md·CLAUDE.md 등) 읽기·쓰기 — 원래 줄끝(CRLF/LF)·BOM·인코딩을 바이트 그대로 지킨다.

하네스가 고객 파일에 넣는 것은 표시 블록·한 줄뿐이다. 나머지 바이트는 한 바이트도 바꾸지 않는다
(Windows 고객 파일을 LF로 통째로 다시 써서 diff 267줄이 된 일 — B4). 그래서:
- 읽을 때 줄끝을 바꾸지 않는다(newline 번역 없음). UTF-8이 아닌 바이트도 surrogateescape로 그대로 들고 있다가 그대로 쓴다
- 하네스가 만드는 새 문장만 `to_eol`로 파일의 줄끝에 맞춘다
- BOM은 떼어 읽고(블록 탐색이 BOM에 걸리지 않게) 쓸 때 다시 붙인다
"""

from pathlib import Path

_BOM = b"\xef\xbb\xbf"


def detect_eol(text):
    """파일에서 더 많이 쓴 줄끝. 줄바꿈이 없으면 LF."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def read(path):
    """(본문 — BOM 뺌·줄끝 그대로 | 파일 없으면 None, 형식 {"bom": bool, "eol": "\\r\\n"|"\\n"})."""
    p = Path(path)
    if not p.is_file():
        return None, {"bom": False, "eol": "\n"}
    raw = p.read_bytes()
    bom = raw.startswith(_BOM)
    text = raw[len(_BOM) if bom else 0:].decode("utf-8", errors="surrogateescape")
    return text, {"bom": bom, "eol": detect_eol(text)}


def to_eol(text, eol):
    """하네스가 만든 문장(LF)을 파일 줄끝으로."""
    t = text.replace("\r\n", "\n")
    return t.replace("\n", eol) if eol != "\n" else t


def write(path, text, fmt=None):
    """본문을 바이트 그대로 쓴다(줄끝 번역 없음). fmt["bom"]이면 BOM을 다시 붙인다."""
    data = text.encode("utf-8", errors="surrogateescape")
    if (fmt or {}).get("bom"):
        data = _BOM + data
    Path(path).write_bytes(data)
