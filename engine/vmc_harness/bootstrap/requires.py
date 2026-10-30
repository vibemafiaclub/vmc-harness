"""진단 제안의 전제조건(`requires`) — 기계로 확인할 수 있는 것만 (spec 07 §제안 → 장치).

"위 명령이 생긴 뒤 켭니다"·".gitattributes 반영 뒤 켜십시오" 같은 제안이 바로 켜진 일(B2)을 막는다:
전제조건이 있는 제안은 [추가] 때 꺼진 채 만들고, 밤 루프(`enable_ready`)가 조건이 다 맞는 것을 켠다.

형식(목록, 모두 맞아야 켠다):
    {"type": "file_exists", "path": "<프로젝트 상대 경로 또는 glob>"}
    {"type": "npm_script", "name": "<package.json scripts 이름>", "dir": "<package.json 폴더, 기본 .>"}
"""

import json
from pathlib import Path

from vmc_harness.core.clock import now_iso

TYPES = ("file_exists", "npm_script")


def validate(reqs):
    """형식 오류 문장 또는 None."""
    if reqs is None:
        return None
    if not isinstance(reqs, list) or not reqs:
        return "requires는 비어 있지 않은 목록이어야 함"
    for r in reqs:
        if not isinstance(r, dict) or r.get("type") not in TYPES:
            return "requires 항목 type이 %s 중 하나가 아님" % "|".join(TYPES)
        key = "path" if r["type"] == "file_exists" else "name"
        v = r.get(key)
        if not isinstance(v, str) or not v.strip() or v.strip().startswith(("/", "..")) or ":" in v[:3]:
            return "requires %s의 %s가 프로젝트 상대 값이 아님" % (r["type"], key)
    return None


def met(root, req):
    root = Path(root)
    t = req.get("type")
    if t == "file_exists":
        p = req["path"].replace("\\", "/")
        if any(c in p for c in "*?["):
            return any(True for _ in root.glob(p))
        return (root / p).exists()
    if t == "npm_script":
        pj = root / (req.get("dir") or ".") / "package.json"
        try:
            scripts = json.loads(pj.read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, ValueError, AttributeError):
            return False
        return req["name"] in scripts
    return False


def unmet(root, reqs):
    return [r for r in reqs or [] if not met(root, r)]


def describe(req):
    if req.get("type") == "file_exists":
        return "%s 있음" % req.get("path")
    if req.get("type") == "npm_script":
        d = req.get("dir") or "."
        return "npm run %s 있음%s" % (req.get("name"), "" if d == "." else " (%s)" % d)
    return str(req)


def enable_ready(root):
    """전제조건 때문에 꺼 둔 생성 게이트·규칙 중 조건이 다 맞은 것을 켠다(밤 루프가 부른다). 반환 {"enabled": [...], "waiting": n}."""
    from vmc_harness.core import events
    enabled, waiting = [], 0
    try:
        from vmc_harness.gates import generated
        for m in generated.load_all(root):
            if m.get("disabled_reason") != "requires" or m.get("status") in ("reverted", "disabled") or m.get("enabled"):
                continue
            if unmet(root, m.get("requires")):
                waiting += 1
                continue
            generated.set_enabled(root, m["gate_id"], True, by_human=False)
            enabled.append(m["gate_id"])
    except ImportError:
        pass
    try:
        from vmc_harness.gates import rules as grules
        data = grules.load(root)
        changed = False
        for r in data["rules"]:
            if r.get("disabled_reason") != "requires" or r.get("enabled", True):
                continue
            if unmet(root, r.get("requires")):
                waiting += 1
                continue
            r["enabled"] = True
            r.pop("disabled_reason", None)
            r["enabled_at"] = now_iso()
            enabled.append(r["id"])
            changed = True
        if changed:
            grules.save(root, data)
            grules.render(root)
    except ImportError:
        pass
    if enabled:
        events.log(str(root), "requires_enabled", ids=enabled)
    return {"enabled": enabled, "waiting": waiting}
