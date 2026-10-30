"""학습 자가 건강 신호 (spec 04 — 판정·집계가 멈춘 것을 하네스가 먼저 알린다).

상태 파일: 로컬 원본 `health.json`
  ledger_last_write  이 PC 작업 장부(ledger/tasks/<YYYY-MM>/<pc_id>/<task_id>.json, 옛 <pc_id>.jsonl) 마지막 기록 시각
  rollup_last_run    일일 집계(metrics.rollup) 마지막 실행 시각
  judge_last_run     판정 한 바퀴(judge.run) 마지막 완료 시각
  activity_last      이 PC 로컬 이벤트(events.jsonl) 마지막 기록 시각 — 사람이 하네스를 쓰고 있다는 기준
  last_error         분리 실행·밤 루프의 마지막 실패 {stage, error, at}
멈춤 = 사람은 계속 쓰는데(activity_last) 기록·실행이 health.*_stall_h 넘게 뒤처짐. 쉬는 날은 멈춤이 아니다.
멈추면 꼭 알릴 일(notices must) 1줄 — 같은 멈춤은 한 번만, 풀리면 다시 알릴 수 있게 지운다.
"""

import datetime as _dt
from pathlib import Path

from vmc_harness.core import config, events, notices, paths
from vmc_harness.core.clock import now, now_iso, parse_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.project import local_dir, pc_id

STALL_KIND = "learning_stalled"
FAIL_KIND = "learning_bg_failed"
LABELS = {"judge_last_run": "판정", "rollup_last_run": "집계", "ledger_last_write": "작업 장부"}


def _file(root):
    return local_dir(root) / "health.json"


def load(root):
    return read_json(_file(root), {}) or {}


def _save(root, st):
    write_json(_file(root), st)


def mark(root, key, at=None):
    """key 시각을 지금(또는 at)으로."""
    st = load(root)
    st[key] = at or now_iso()
    _save(root, st)
    return st


def _iso_of_mtime(p):
    try:
        return _dt.datetime.fromtimestamp(Path(p).stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return None


_TAIL_BYTES = 1 << 14


def tail_at(p, keys=("at",)):
    """jsonl 마지막 기록의 시각(keys 중 처음 있는 것). 파일 전체를 읽지 않는다. 없으면 파일 수정 시각."""
    import json
    try:
        with open(str(p), "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            lines = [l for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        for k in keys:
            if isinstance(rec, dict) and parse_iso(rec.get(k)):
                return rec[k]
        break
    return _iso_of_mtime(p)


def ledger_last_write(root):
    """이 PC가 쓴 작업 장부 파일 중 가장 최근 수정 시각(다른 PC 기록·git pull은 세지 않게 pc_id 파일만)."""
    base = paths.ledger_dir(root) / "tasks"
    if not base.is_dir():
        return None
    best = None
    pid = pc_id()
    for f in base.glob("*/%s.jsonl" % pid):   # 0.1.4까지의 추가 전용 기록
        t = tail_at(f, ("updated_at", "ended_at", "at", "started_at"))
        if t and (best is None or parse_iso(t) > parse_iso(best)):
            best = t
    newest = None   # 0.1.5부터 작업당 파일(착수 때 생성·매 턴 덮어쓰기) — 수정 시각이 곧 마지막 기록
    for f in base.glob("*/%s/*.json" % pid):
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        if newest is None or m > newest[0]:
            newest = (m, f)
    if newest:
        from vmc_harness.core.jsonio import read_json
        rec = read_json(newest[1], None) or {}
        t = rec.get("written_at") or _iso_of_mtime(newest[1])
        if t and (best is None or parse_iso(t) > parse_iso(best)):
            best = t
    return best


def record_failure(root, stage, error):
    """분리 실행·밤 루프 실패 — events(로컬 원본)에 남기고, 그날 그 단계 첫 실패만 알림 1줄."""
    err = str(error)[:300]
    try:
        events.log(root, "ledger_bg_error", stage=stage, error=err)
    except Exception:
        pass
    try:
        st = load(root)
        st["last_error"] = {"stage": stage, "error": err, "at": now_iso()}
        key = "%s:%s" % (stage, now_iso()[:10])
        if st.get("failure_noticed") != key:
            st["failure_noticed"] = key
            notices.add(root, FAIL_KIND, "must",
                        "학습 백그라운드 실패(%s) — %s" % (stage, err.splitlines()[0][:120] if err else "원인 미상"),
                        refs={"stage": stage})
        _save(root, st)
    except Exception:
        pass


def check(root, at=None):
    """상태 파일 갱신 + 멈춤 판정. 반환: 상태 dict(stalled = 멈춘 항목 이름 목록)."""
    at = at or now()
    st = load(root)
    st.setdefault("first_seen", at.isoformat(timespec="seconds"))
    lw = ledger_last_write(root)
    if lw:
        st["ledger_last_write"] = lw
    act = tail_at(local_dir(root) / "events.jsonl")
    if act:
        st["activity_last"] = act
    act_t = parse_iso(st.get("activity_last"))
    first = parse_iso(st["first_seen"])
    stalled, worst = [], 0.0
    for key, label in LABELS.items():
        lim = float(config.get("health.ledger_stall_h" if key == "ledger_last_write" else "health.judge_stall_h", root))
        last = parse_iso(st.get(key)) or first
        if act_t is None or last is None:
            continue
        lag_h = (act_t - last).total_seconds() / 3600.0
        if lag_h > lim:
            stalled.append(label)
            worst = max(worst, (at - last).total_seconds() / 3600.0)
    st["stalled"] = stalled
    st["checked_at"] = at.isoformat(timespec="seconds")
    key = ",".join(stalled)
    if stalled and st.get("stall_noticed") != key:
        st["stall_noticed"] = key
        try:
            notices.add(root, STALL_KIND, "must",
                        "학습이 멈췄다 — %s 마지막 기록이 %d시간 전(하네스는 계속 쓰이는 중). `vmc-harness judge`로 바로 돌릴 수 있어요"
                        % ("·".join(stalled), int(worst)), refs={"stalled": stalled})
        except Exception:
            pass
    elif not stalled:
        st.pop("stall_noticed", None)
    _save(root, st)
    return st
