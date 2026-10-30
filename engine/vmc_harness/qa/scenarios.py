"""QA 시나리오 `.harness/qa/scenarios/<id>.json` (spec 05·09).

메타 게이트: **기대값마다 출처 필수** — `current_behavior`(지금 코드가 이렇게 동작한다는 관찰 근거) 또는
`human_quote`(사람 지시 원문 인용). 출처 없는 기대값은 저장도 실행도 거부한다(spec 04 실패 5, 05).

시나리오 형식(기본값):
{
  "id": "login-basic",                       # 파일 이름과 같다. 영문·숫자·-_
  "title": "로그인 후 대시보드",
  "pages": ["/login", "/dashboard"],          # 화면 경로(사람이 부르는 페이지)
  "paths": ["fe/app/login/**"],               # 이 시나리오가 덮는 소스 경로 glob(루프 ④·회귀 판단용)
  "steps": ["..."],                           # 자유 서술(절차 강제 아님 — 모델이 참고)
  "expectations": [
    {"text": "잘못된 비밀번호면 오류 문구가 보인다",
     "source": {"type": "human_quote", "quote": "비번 틀리면 빨간 글씨 떠야 해", "from": "세션·날짜(선택)"}},
    {"text": "목록은 최신순",
     "source": {"type": "current_behavior", "ref": "fe/app/list/page.tsx::sortItems @ <커밋>"}}
  ],
  "data": {"writes": [{"entity": "work_requests", "op": "create"}]},   # 이 시나리오가 개발 DB에 남기는 쓰기(data-policy 검사)
  "account": "qa-user"                        # qa accounts 이름(선택)
}
"""

import re
from pathlib import Path

from vmc_harness.core import paths
from vmc_harness.core.clock import now_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.ledger import MetaGateError
from vmc_harness.core.mask import mask

SOURCE_TYPES = ("current_behavior", "human_quote")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")
WRITE_OPS = ("create", "update", "delete")


def scenarios_dir(root):
    return paths.harness_dir(root) / "qa" / "scenarios"


def scenario_path(root, sid):
    return scenarios_dir(root) / ("%s.json" % sid)


def validate(sc):
    """통과하면 None, 아니면 MetaGateError(무엇이 빠졌는지만)."""
    if not isinstance(sc, dict):
        raise MetaGateError("시나리오가 JSON 객체가 아님")
    sid = sc.get("id")
    if not sid or not ID_RE.match(str(sid)):
        raise MetaGateError("시나리오 id 없음 또는 형식 오류")
    exps = sc.get("expectations")
    if not isinstance(exps, list) or not exps:
        raise MetaGateError("시나리오 %s: 기대값 없음" % sid)
    for i, e in enumerate(exps, 1):
        if not isinstance(e, dict) or not str(e.get("text") or "").strip():
            raise MetaGateError("시나리오 %s 기대값 %d: 내용 없음" % (sid, i))
        src = e.get("source")
        if not isinstance(src, dict) or src.get("type") not in SOURCE_TYPES:
            raise MetaGateError("시나리오 %s 기대값 %d: 출처 없음(current_behavior|human_quote)" % (sid, i))
        if src["type"] == "human_quote" and not str(src.get("quote") or "").strip():
            raise MetaGateError("시나리오 %s 기대값 %d: 사람 지시 원문 없음" % (sid, i))
        if src["type"] == "current_behavior" and not str(src.get("ref") or "").strip():
            raise MetaGateError("시나리오 %s 기대값 %d: 현재 동작 관찰 근거(ref) 없음" % (sid, i))
    writes = (sc.get("data") or {}).get("writes") or []
    if not isinstance(writes, list):
        raise MetaGateError("시나리오 %s: data.writes 형식 오류" % sid)
    for w in writes:
        if not isinstance(w, dict) or not w.get("entity") or w.get("op") not in WRITE_OPS:
            raise MetaGateError("시나리오 %s: data.writes 항목에 entity·op(create|update|delete) 없음" % sid)
    return None


def is_valid(sc):
    try:
        validate(sc)
        return True, None
    except MetaGateError as e:
        return False, str(e)


def _masked(v):
    if isinstance(v, str):
        return mask(v)
    if isinstance(v, dict):
        return {k: _masked(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_masked(x) for x in v]
    return v


def save(root, sc, overwrite=False):
    """메타 게이트를 통과한 시나리오만 저장. 기존 시나리오 덮어쓰기는 overwrite=True일 때만
    (기존 기대값 수정은 검증물 변조 — gates 영역 PreToolUse가 착수 선언 없이 막는다)."""
    validate(sc)
    p = scenario_path(root, sc["id"])
    if p.exists() and not overwrite:
        raise MetaGateError("시나리오 %s 이미 있음" % sc["id"])
    rec = _masked(dict(sc))
    rec.setdefault("created_at", now_iso())
    write_json(p, rec)
    return rec


def load(root, sid):
    return read_json(scenario_path(root, sid))


def load_all(root):
    """[(scenario, ok, reason)] — 형식이 틀린 파일도 이유와 함께 돌려준다(조용히 버리지 않는다)."""
    d = scenarios_dir(root)
    out = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            sc = read_json(f)
        except ValueError:
            out.append(({"id": f.stem}, False, "시나리오 %s: JSON 형식 오류" % f.stem))
            continue
        ok, reason = is_valid(sc)
        if ok and sc.get("id") != f.stem:
            ok, reason = False, "시나리오 %s: 파일 이름과 id 다름" % f.stem
        out.append((sc, ok, reason))
    return out


def check_write(root, rel_path, content):
    """에이전트가 시나리오 파일을 직접 쓰려 할 때(PreToolUse) 쓰는 검사. (ok, reason).
    rel_path가 시나리오 폴더 밖이면 (True, None)."""
    rel = str(rel_path).replace("\\", "/")
    if not re.match(r"^\.harness/qa/scenarios/[^/]+\.json$", rel):
        return True, None
    import json
    try:
        sc = json.loads(content or "")
    except ValueError:
        return False, "시나리오 JSON 형식 오류"
    ok, reason = is_valid(sc)
    if ok and sc.get("id") != Path(rel).stem:
        return False, "시나리오 파일 이름과 id 다름"
    return ok, reason


def covering(root, rel_paths=None, pages=None):
    """주어진 소스 경로·페이지를 덮는 유효 시나리오 id 목록."""
    import fnmatch
    rel_paths = [str(p).replace("\\", "/") for p in (rel_paths or [])]
    pages = set(pages or [])
    out = []
    for sc, ok, _ in load_all(root):
        if not ok:
            continue
        hit = bool(pages & set(sc.get("pages") or []))
        for g in sc.get("paths") or []:
            if any(fnmatch.fnmatch(p, g) or p.startswith(g.rstrip("*").rstrip("/") + "/") for p in rel_paths):
                hit = True
                break
        if hit:
            out.append(sc["id"])
    return out
