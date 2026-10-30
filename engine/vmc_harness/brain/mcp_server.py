"""브레인 MCP stdio 서버 `vmc-harness mcp` (spec 04). JSON-RPC 2.0, 줄 단위 JSON, 표준 라이브러리만.

도구: search_cards(situation) · get_card(id). 반환 카드 = 노출 기록(프로젝트 안에서 띄웠을 때).
"""

import json
import os
import sys

from vmc_harness import __version__

SERVER_NAME = "vmc-brain"
DEFAULT_PROTOCOL = "2025-06-18"

# 서버 안내문 — 사실·제약만(01 P6). 충돌 처리는 04 §충돌.
INSTRUCTIONS = (
    "이 팀의 판단 카드(브레인)입니다. 계획을 세우거나 방향·범위·방식을 정할 때 search_cards에 지금 상황과 내려야 할 "
    "결정을 한두 문장으로 넣으면 관련 카드가 나옵니다. 카드의 '적용하지 않는 경우'에 해당하면 따르지 않습니다. "
    "카드가 지금 사람의 지시와 다르면 지시를 따르고 묻지 않습니다 — 완료 보고에 한 줄로 적고 "
    "`vmc-harness card conflict <id>`로 기록합니다."
)

TOOLS = [
    {"name": "search_cards",
     "description": "지금 상황과 내려야 할 결정에 맞는 판단 카드를 찾는다. 결과: id·제목·판단·신호·적용하지 않는 경우.",
     "inputSchema": {"type": "object",
                     "properties": {"situation": {"type": "string", "description": "지금 상황 + 내려야 할 결정(한두 문장)"},
                                    "top_k": {"type": "integer", "minimum": 1}},
                     "required": ["situation"]}},
    {"name": "get_card",
     "description": "카드 전문(상황·판단·신호·적용하지 않는 경우·원문 인용)을 id로 읽는다.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
]


# 프로젝트를 찾는 순서: 명시(VMC_HARNESS_PROJECT) → 에이전트가 넘기는 프로젝트 폴더 → 현재 폴더.
# 못 찾으면 카드 검색은 되지만 노출이 남지 않는다(판정이 영원히 시험 중) — stderr(클라이언트 MCP 로그)에 한 줄 남긴다.
_ROOT_ENVS = ("VMC_HARNESS_PROJECT", "CLAUDE_PROJECT_DIR")  # CLAUDE_PROJECT_DIR가 MCP 서버에도 오는지는 미검증


def _root():
    from vmc_harness.core import paths
    for start in [os.environ.get(k) for k in _ROOT_ENVS] + [os.getcwd()]:
        if not start:
            continue
        try:
            r = paths.find_project_root(start)
        except OSError:
            r = None
        if r:
            return r
    try:
        sys.stderr.write("vmc-brain: 하네스 프로젝트를 못 찾음(cwd=%s) — 카드 노출을 기록하지 않음\n" % os.getcwd())
        sys.stderr.flush()
    except Exception:
        pass
    return None


def _fmt_card(c, full=False):
    lines = ["[%s] %s" % (c["id"], c.get("title", ""))]
    if full and c.get("situation"):
        lines.append("상황: %s" % c["situation"])
    lines.append("판단: %s" % (c.get("judgment") or ""))
    if c.get("signals"):
        lines.append("이럴 때 꺼낸다: %s" % " / ".join(c["signals"]))
    if c.get("not_when"):
        lines.append("적용하지 않는 경우: %s" % c["not_when"])
    if full and c.get("quote"):
        lines.append("원문 인용: %s" % c["quote"])
    return "\n".join(lines)


def call_tool(name, args, root=None):
    from vmc_harness.brain import search, store
    if name == "search_cards":
        q = (args or {}).get("situation") or (args or {}).get("query") or ""
        if not q.strip():
            return {"content": [{"type": "text", "text": "situation이 비어 있음"}], "isError": True}
        cards = search.search(q, top_k=(args or {}).get("top_k"), root=root)
        text = "\n\n".join(_fmt_card(c) for c in cards) if cards else "맞는 카드 없음"
        return {"content": [{"type": "text", "text": text}]}
    if name == "get_card":
        cid = (args or {}).get("id") or ""
        try:
            c = store.get_card(cid)
        except KeyError:
            c = None
        if not c or c.get("status", "active") != "active":
            return {"content": [{"type": "text", "text": "카드 없음: %s" % cid}], "isError": True}
        return {"content": [{"type": "text", "text": _fmt_card(c, full=True)}]}
    raise KeyError(name)


def handle(msg, root=None):
    """요청 1건 → 응답 dict(알림이면 None)."""
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if mid is None:  # notification
        return None
    try:
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": SERVER_NAME, "version": __version__},
                      "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                result = call_tool(params.get("name"), params.get("arguments") or {}, root)
            except KeyError:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "알 수 없는 도구"}}
        elif method in ("resources/list", "prompts/list"):
            result = {method.split("/")[0]: []}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}
    except Exception as e:  # 서버가 죽지 않게
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)[:300]}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None):
    if stdin is None:
        stdin = sys.stdin
        if hasattr(stdin, "reconfigure"):  # Windows 콘솔 기본 인코딩(cp949)에서 한글이 깨지지 않게
            try:
                stdin.reconfigure(encoding="utf-8")
            except Exception:
                pass
    stdout = stdout or sys.stdout
    root = _root()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            if isinstance(msg, list):
                resp = [r for r in (handle(m, root) for m in msg) if r is not None] or None
            else:
                resp = handle(msg, root)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0
