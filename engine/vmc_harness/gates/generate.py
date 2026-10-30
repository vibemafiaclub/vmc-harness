"""사람 명시 요청("앞으로 이건 막아"·진단 [추가]·카드 승격) → 생성 게이트 (spec 02 §생성 게이트, 01 P3·P6).

본체는 LLM(`claude -p`)이 목표·참고 자료·제약·출력 형식만 받아 쓴다(절차 없음). 엔진은 출력 형식·안전선만 기계 검사한다:
엔진 API만 쓰는가(generated.validate_script), 근거 타입(원문 + 출처), 발동 조건 값.
범위는 사람이 한 말 그대로 — 원문은 메타 basis.quote에 손대지 않고 저장한다(렌더링·요약 없음).
기계로 판정할 수 없는 요청(취향·문체)이면 게이트를 만들지 않고 None(호출자가 규칙·카드로).
"""

import json
import re

from vmc_harness.core import config
from vmc_harness.core.clock import now_iso

GOAL = ("사람이 한 요청을 그대로 막는 하네스 게이트 하나를 만든다. 범위는 요청 원문 그대로 — 넓히지도 좁히지도 않는다. "
        "기계로 결정적으로 판정할 수 없는 요청(취향·문체·디자인 감각)이면 만들지 않는다.")
CONSTRAINTS = [
    "본체는 Python 3.9, `def check(api):` 하나. 위반이면 무엇을 어겼는지 한 문장(str), 통과면 None",
    "import는 re, json, fnmatch, math, itertools, collections, functools, string, datetime만. 파일 쓰기·프로세스·네트워크 없음",
    "api 속성: event, paths(발동 조건에 걸린 파일), all_paths, tool_name, command, change_kinds{rel: add|modify|delete}, size",
    "api 메서드: read(rel) 지금 내용, proposed(rel) 편집 뒤 내용, head(rel) HEAD 내용, exists(rel), facts(), match(rel, glob)",
    "위반 문장은 무엇을 어겼는지만 — 해결 방법을 지시하지 않는다",
    "발동 조건(trigger)은 좁게: events(PreToolUse|PostToolUse|Stop), paths(glob), sizes(small|mid), change_kinds(add|modify|delete)",
]
OUTPUT = ('JSON 하나만: {"gateable": true, "title": "짧은 이름", "trigger": {...}, "targets": ["게이트가 가리키는 실제 파일"], '
          '"script": "본체 코드"} 또는 {"gateable": false, "reason": "한 줄"}')


def _prompt(root, quote, source):
    refs = {}
    try:
        from vmc_harness.bootstrap import facts
        f = facts.load(str(root)) or {}
        refs["source_roots"] = f.get("source_roots")
        refs["conventions"] = f.get("conventions")
    except Exception:
        pass
    try:
        from vmc_harness.core import paths
        mp = paths.cache_dir(root) / "map.md"
        if mp.is_file():
            refs["structure_map"] = mp.read_text(encoding="utf-8")[: int(config.get("gates.api_body_max_chars", root))]
    except Exception:
        pass
    return "\n".join([
        "## 목표", GOAL, "",
        "## 사람 요청 원문(그대로)", quote, "",
        "## 출처", json.dumps(source or {}, ensure_ascii=False), "",
        "## 참고할 수 있는 것", json.dumps(refs, ensure_ascii=False), "",
        "## 제약", "\n".join("- " + c for c in CONSTRAINTS), "",
        "## 출력 형식", OUTPUT,
    ])


def _parse(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def from_request(root, quote, source=None, promoted_from=None, draft=None):
    """게이트를 만들면 {'gate_id','action_id','cost_line','over_budget'}, 만들 수 없으면 None.
    draft: LLM 대신 쓸 출력(테스트·에이전트가 직접 쓴 본체) — 형식은 OUTPUT과 같다."""
    from vmc_harness.gates import generated
    source = dict(source or {})
    source.setdefault("date", now_iso()[:10])
    if promoted_from:
        source["promoted_from"] = promoted_from
    out = draft
    if out is None:
        from vmc_harness.core import llm
        res = llm.claude_p(_prompt(root, quote, source), model=config.get("gates.generate_model", root),
                           timeout=config.get("gates.generate_timeout_s", root), cwd=str(root))
        if not res:
            return None
        out = _parse(res["text"])
    if not isinstance(out, dict) or not out.get("gateable") or not out.get("script"):
        return None
    try:
        generated.validate_script(out["script"])
    except generated.GateScriptError:
        return None
    basis = {"type": "explicit_request", "quote": quote, "source": source}
    r = generated.create(root, title=out.get("title") or quote[:40], basis=basis, trigger=out.get("trigger") or {},
                         script_text=out["script"], targets=out.get("targets") or [])
    return {"gate_id": r["meta"]["gate_id"], "action_id": r["meta"]["action_id"], "cost_line": r["cost_line"],
            "over_budget": r["over_budget"]}
