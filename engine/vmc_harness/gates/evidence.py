"""실제 동작 증거 (spec 02 완료 게이트 — "증거는 하네스 도구가 만든 산출물만").

`.harness/cache/evidence/<task_id>/`에 산출물 + 마커(`<id>.marker.json`: task_id·created_at·kind·target·file·sha256).
완료 게이트는 마커만 본다(존재·마지막 편집 이후 생성·인정 종류). 이 폴더는 엔진 쓰기 보호 대상이라 에이전트가 손으로 못 쓴다.

kind: screenshot | api_call | db_dryrun | test_run | qa_scenario
"""

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from vmc_harness import KERNEL_VERSION
from vmc_harness.core import config, paths
from vmc_harness.core.clock import now
from vmc_harness.core.jsonio import note_engine_write, read_json, write_json
from vmc_harness.core.mask import mask_obj

KINDS = ("screenshot", "api_call", "db_dryrun", "test_run", "qa_scenario")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def evidence_dir(root, task_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id or "nosession")
    return paths.cache_dir(root) / "evidence" / safe


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _new_id():
    return now().strftime("%Y%m%dT%H%M%S%f") + "-" + os.urandom(3).hex()


def worktree_hashes(root):
    """증거를 남기는 순간 작업 트리에서 바뀐 파일(미커밋·미추적)의 내용 sha256 — {상대 경로: sha}.
    완료 게이트가 '마지막 편집 이후 증거'를 파일 수정 시각만으로 재면, 다른 세션의 git autostash가 같은 내용을
    다시 써 시각만 늦춘 파일 때문에 멀쩡한 증거를 무효로 본다(2026-11-15 현장). 내용이 그대로면 증거를 인정하는 데 쓴다.
    상한(gates.evidence_snapshot_max_files)을 넘는 파일은 적지 않는다 — 그 파일은 시각 비교로만 판정된다."""
    from vmc_harness.core import gitutil
    rc, out, _ = gitutil.git(root, "status", "--porcelain", "-z", "-uall")
    if rc != 0:
        return {}
    cap = int(config.get("gates.evidence_snapshot_max_files", root))
    res = {}
    for ent in gitutil.split_z(out):
        rel = ent[3:] if len(ent) > 3 else ""
        if not rel or rel.startswith(".harness/"):
            continue
        p = Path(root) / rel
        if not p.is_file():
            continue
        if len(res) >= cap:
            break
        try:
            res[rel] = _sha(p)
        except OSError:
            pass
    return res


def make(root, task_id, kind, target, src_path=None, data=None):
    """증거 산출물 + 마커. src_path는 복사(스크린샷 등), data는 JSON으로 저장. 마커 dict를 돌려준다.
    마커에 그 순간 바뀐 파일들의 내용 해시(tree_sha)를 함께 남긴다 — completion이 내용이 그대로인지 본다."""
    if kind not in KINDS:
        raise ValueError("증거 종류가 인정 목록 밖: %s" % kind)
    d = paths.ensure(evidence_dir(root, task_id))
    if src_path and Path(src_path).is_file():
        dup = _same_content(root, task_id, kind, _sha(Path(src_path)))
        if dup:
            return dup
    mid = _new_id()
    marker = {"marker_id": mid, "task_id": task_id, "created_at": now().isoformat(), "kind": kind,
              "target": target, "file": None, "sha256": None, "by": "vmc-harness", "kernel_version": KERNEL_VERSION,
              "tree_sha": worktree_hashes(root)}
    if src_path:
        sp = Path(src_path)
        if not sp.is_file():
            raise FileNotFoundError(str(sp))
        dst = d / ("%s-%s" % (mid, sp.name))
        shutil.copy2(str(sp), str(dst))
        note_engine_write(dst)
        marker["file"] = dst.name
        marker["sha256"] = _sha(dst)
        marker["src_mtime"] = _iso_from_ts(sp.stat().st_mtime)
    if data is not None:
        dst = d / ("%s.json" % mid)
        write_json(dst, _masked(data))
        if not marker["file"]:
            marker["file"] = dst.name
            marker["sha256"] = _sha(dst)
        if isinstance(data, dict) and isinstance(data.get("summary"), dict):
            marker["summary"] = data["summary"]
    if not src_path and marker["sha256"]:
        dup = _same_content(root, task_id, kind, marker["sha256"])
        if dup:
            try:
                (d / marker["file"]).unlink()
            except OSError:
                pass
            return dup
    write_json(d / ("%s.marker.json" % mid), marker)
    return marker


def _same_content(root, task_id, kind, sha):
    """같은 작업·같은 종류에 같은 sha256 증거가 이미 있으면 그 마커(duplicate 표시) — 같은 증거는 한 번만(현장 결함 C5)."""
    for m in list_markers(root, task_id):
        if m.get("kind") == kind and m.get("sha256") == sha:
            return dict(m, duplicate=True)
    return None


def _iso_from_ts(ts):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts).astimezone().isoformat()


def _masked(v):
    return mask_obj(v)


def list_markers(root, task_id):
    d = evidence_dir(root, task_id)
    if not d.is_dir():
        return []
    out = []
    for f in d.glob("*.marker.json"):
        m = read_json(f, None)
        if isinstance(m, dict) and m.get("kind") in KINDS:
            out.append(m)
    return sorted(out, key=lambda m: m.get("created_at") or "")


# ---- 산출물 만들기 (명령 `evidence`가 부른다) ----

def screenshot(root, task_id, src, target):
    p = Path(src)
    if not p.is_file() or p.stat().st_size == 0:
        raise FileNotFoundError("스크린샷 파일 없음: %s" % src)
    if p.suffix.lower() not in IMAGE_EXT:
        raise ValueError("이미지 파일이 아님: %s" % src)
    return make(root, task_id, "screenshot", target, src_path=str(p))


def api_call(root, task_id, method, url, target=None, body=None, headers=None):
    """urllib로 실제 호출하고 응답을 기록한다. 연결 실패면 예외(마커 없음)."""
    import urllib.error
    import urllib.request
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method.upper())
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None and not any(k.lower() == "content-type" for k in (headers or {})):
        req.add_header("Content-Type", "application/json")
    t0 = time.time()
    status, resp_headers, text = None, {}, ""
    limit = int(config.get("gates.api_body_max_chars", root))
    try:
        with urllib.request.urlopen(req, timeout=config.get("gates.api_timeout_s", root)) as r:
            status = r.status
            resp_headers = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "location", "content-length")}
            text = r.read(limit + 1).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status = e.code
        resp_headers = {k: v for k, v in (e.headers or {}).items() if k.lower() in ("content-type", "location", "content-length")}
        try:
            text = e.read(limit + 1).decode("utf-8", "replace")
        except Exception:
            text = ""
    sec = round(time.time() - t0, 3)
    rec = {"request": {"method": method.upper(), "url": url, "body": (body or "")[:limit] if isinstance(body, str) else None,
                       "headers": sorted((headers or {}).keys())},
           "response": {"status": status, "headers": resp_headers, "body": text[:limit], "truncated": len(text) > limit},
           "sec": sec, "summary": {"status": status, "method": method.upper()}}
    return make(root, task_id, "api_call", target or url, data=rec)


_PYTEST_FAIL = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.M)
_JEST_FAIL = re.compile(r"^\s*(?:FAIL)\s+(\S+)", re.M)
_GENERIC_FAIL = re.compile(r"^\s*(?:✕|×|✗)\s+(.+?)\s*(?:\(\d+\s*ms\))?$", re.M)


def parse_failures(output):
    fails = set(_PYTEST_FAIL.findall(output or "")) | set(_JEST_FAIL.findall(output or ""))
    fails |= {m.strip() for m in _GENERIC_FAIL.findall(output or "")}
    return sorted(fails)


def baseline_failures(root):
    """부트스트랩 기계 사실의 테스트 기준선(지금 실패 목록). 없으면 None.
    형식(bootstrap.facts): facts['test_baseline'] = [{'runner','dir','failed':[...],'errors':[...],
    'collection_errors':[{'path','reason'}]}, ...] — 실행 폴더 기준 id와 루트 기준 id(dir/ 붙임) 둘 다 넣는다."""
    try:
        from vmc_harness.bootstrap import facts
        f = facts.load(str(root)) or {}
    except Exception:
        return None
    b = f.get("test_baseline")
    if b is None:
        return None
    out = set()
    if isinstance(b, dict):
        b = b.get("runs") if isinstance(b.get("runs"), list) else [b]
    for tb in b if isinstance(b, list) else []:
        if isinstance(tb, str):
            out.add(tb)
            continue
        if not isinstance(tb, dict):
            continue
        d = (tb.get("dir") or ".").strip("/")
        ids = list(tb.get("failed") or []) + list(tb.get("errors") or []) + list(tb.get("failures") or [])
        ids += [c.get("path") if isinstance(c, dict) else c for c in tb.get("collection_errors") or []]
        for i in ids:
            if not i:
                continue
            out.add(i)
            if d and d != ".":
                out.add("%s/%s" % (d, i))
    return sorted(out)


def test_run(root, task_id, argv, target=None, cwd=None):
    from vmc_harness.core.proc import run
    exe = shutil.which(argv[0]) or argv[0]
    t0 = time.time()
    rc, out, err = run([exe] + list(argv[1:]), cwd=str(cwd or root), timeout=config.get("gates.test_timeout_s", root))
    sec = round(time.time() - t0, 3)
    output = (out or "") + ("\n" + err if err else "")
    fails = parse_failures(output)
    base = baseline_failures(root)
    new = sorted(set(fails) - set(base or []))
    unparsed = rc != 0 and not fails
    tail_n = int(config.get("gates.test_output_tail_lines", root))
    rec = {"argv": list(argv), "rc": rc, "sec": sec, "failures": fails, "baseline_failures": base,
           "new_failures": new, "unparsed_failure": unparsed,
           "output_tail": "\n".join(output.splitlines()[-tail_n:]),
           "summary": {"rc": rc, "failures": len(fails), "new_failures": len(new), "unparsed_failure": unparsed}}
    return make(root, task_id, "test_run", target or " ".join(argv), data=rec)


def marker_ok(m):
    """인정되는 마커인가 — 테스트 실행 기록은 새 실패 0건(기준선 대비)일 때만."""
    if m.get("kind") != "test_run":
        return True
    s = m.get("summary") or {}
    return int(s.get("new_failures") or 0) == 0 and not s.get("unparsed_failure")


def marker_time(m):
    """증거가 만들어진 시각 — 스크린샷은 원본 파일 시각과 마커 시각 중 이른 쪽(옛 스크린샷 재사용 방지)."""
    from vmc_harness.core.clock import parse_iso
    t = parse_iso(m.get("created_at"))
    s = parse_iso(m.get("src_mtime")) if m.get("src_mtime") else None
    if t and s and s < t:
        return s
    return t


def load_data(root, task_id, marker):
    if not marker.get("file") or not str(marker["file"]).endswith(".json"):
        return None
    return read_json(evidence_dir(root, task_id) / marker["file"], None)


def to_json(m):
    return json.dumps(m, ensure_ascii=False)
