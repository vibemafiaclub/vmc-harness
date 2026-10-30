"""로컬 원본 보관 기간 정리 (spec 09 ① 로컬 원본 — `retention.local_days`).

세션 시작 때 하루 1회: `retention.local_days`보다 오래된
- `events.jsonl` 행(at 기준 — 파일을 다시 쓴다)
- `quotes/` 파일(마지막 수정 기준)
- `.harness/cache/evidence/<task_id>/` 폴더(마지막 수정 기준 — 스크린샷·응답 원문)
을 지운다. 팀 장부(`.harness/ledger/`)는 건드리지 않는다. 실패해도 작업을 멈추지 않는다(편의 — 호출자가 예외를 삼킨다).
"""

import json
import os
import shutil

from vmc_harness.core import config, paths
from vmc_harness.core.clock import now, parse_iso
from vmc_harness.core.jsonio import read_json, write_json


def _state_path(root):
    from vmc_harness.core.project import local_dir
    return local_dir(root) / "retention.json"


def _old(p, cutoff_ts):
    try:
        return p.stat().st_mtime < cutoff_ts
    except OSError:
        return False


def prune(root, force=False):
    """{'events': 지운 행 수, 'quotes': n, 'evidence': n} 또는 오늘 이미 했으면 None."""
    from vmc_harness.core.project import local_dir
    sp = _state_path(root)
    st = read_json(sp, {}) or {}
    today = now().strftime("%Y-%m-%d")
    if not force and st.get("pruned_on") == today:
        return None
    days = float(config.get("retention.local_days", root))
    cutoff = now().timestamp() - days * 86400
    ld = local_dir(root)
    out = {"events": 0, "quotes": 0, "evidence": 0}
    ev = ld / "events.jsonl"
    if ev.is_file():
        keep, dropped = [], 0
        with ev.open("r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    t = parse_iso(json.loads(s).get("at"))
                except ValueError:
                    t = None
                if t is not None and t.timestamp() < cutoff:
                    dropped += 1
                else:
                    keep.append(s + "\n")
        if dropped:
            tmp = ev.with_name(ev.name + ".tmp")
            with tmp.open("w", encoding="utf-8", newline="\n") as f:
                f.writelines(keep)
            os.replace(str(tmp), str(ev))
        out["events"] = dropped
    qd = ld / "quotes"
    if qd.is_dir():
        for p in qd.iterdir():
            if p.is_file() and _old(p, cutoff):
                p.unlink()
                out["quotes"] += 1
    ed = paths.cache_dir(root) / "evidence"
    if ed.is_dir():
        for d in ed.iterdir():
            if d.is_dir() and _old(d, cutoff) and all(_old(f, cutoff) for f in d.iterdir()):
                shutil.rmtree(str(d), ignore_errors=True)
                out["evidence"] += 1
    st["pruned_on"] = today
    write_json(sp, st)
    return out
