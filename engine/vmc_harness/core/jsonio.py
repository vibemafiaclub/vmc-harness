"""JSON 파일 읽기·쓰기. 쓰기는 임시 파일 → 교체(원자적), jsonl은 추가 전용."""

import json
import os
import tempfile
import time
from pathlib import Path


def read_json(path, default=None):
    p = Path(path)
    if not p.is_file():
        return default
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=False)
            f.write("\n")
        _replace(tmp, str(p))
        note_engine_write(p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def _windows():
    return os.name == "nt"


def _replace(src, dst):
    """os.replace. Windows에서 다른 프로세스가 대상 파일을 열고 있으면 접근 거부(WinError 5·32)가 난다 —
    병렬 도구 호출 때 훅 둘이 같은 캐시·세션 파일을 동시에 쓰는 경우(현장 결함 F1). 잠깐 기다렸다 다시 시도한다."""
    if not _windows():
        os.replace(src, dst)
        return
    from vmc_harness.core import config
    vals = config.defaults()[0]
    tries, wait = int(vals["io.replace_retries"]), float(vals["io.replace_retry_wait_s"])
    for i in range(tries + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i >= tries:
                raise
            time.sleep(wait * (1 + i))


def read_text_any(path):
    """텍스트 파일 읽기 — UTF-8이 아닌 파일도 오류 없이. BOM으로 UTF-16·UTF-8-SIG를 가리고(SSMS 등이 UTF-16으로 저장),
    그 밖에 UTF-8로 못 읽으면(바이너리·다른 인코딩) None. 없는 파일도 None."""
    p = Path(path)
    if not p.is_file():
        return None
    data = p.read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    try:
        return data.decode("utf-8-sig" if data[:3] == b"\xef\xbb\xbf" else "utf-8")
    except UnicodeDecodeError:
        return None


def append_jsonl(path, record):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line)
    note_engine_write(p)
    return p


def engine_writes_log(root):
    """엔진 장부·증거 쓰기 일지 — 사용자 홈(레포 밖: git 작업 트리를 더럽히지 않는다), 프로젝트 경로마다 하나."""
    import hashlib
    from vmc_harness.core import paths
    key = hashlib.sha256(os.path.realpath(str(root)).encode("utf-8")).hexdigest()[:16]
    return paths.home() / "engine-writes" / ("%s.log" % key)


def note_engine_write(path):
    """장부(`.harness/ledger/`)·증거(`.harness/cache/evidence/`) 쓰기를 일지에 남긴다 — 해석 못 한 셸 명령의
    실행 뒤 대조(gates.opaque_watch)가 같은 시간에 엔진 프로세스가 쓴 파일을 되돌리지 않게. 실패해도 쓰기를 막지 않는다."""
    parts = Path(os.path.abspath(str(path))).parts
    if ".harness" not in parts:
        return
    i = len(parts) - 1 - parts[::-1].index(".harness")
    rest = parts[i + 1:]
    if not rest or not (rest[0] == "ledger" or tuple(rest[:2]) == ("cache", "evidence")):
        return
    try:
        log = engine_writes_log(Path(*parts[:i]))
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8", newline="\n") as f:
            f.write("%d\t%s\n" % (time.time_ns(), "/".join(parts[i:])))
    except OSError:
        pass


def read_jsonl(path):
    p = Path(path)
    if not p.is_file():
        return []
    out = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out
