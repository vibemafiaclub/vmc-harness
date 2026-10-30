"""회귀 세트 공통 — 데이터셋 파일·임시 프로젝트·환경변수 잠시 바꾸기."""

import contextlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from vmc_harness.core.jsonio import read_json


class DatasetError(ValueError):
    pass


def dataset_file(dataset, name):
    p = Path(dataset).expanduser() / name
    return p if p.is_file() else None


def load(dataset, name, required=True):
    p = dataset_file(dataset, name)
    if p is None:
        if required:
            raise DatasetError("데이터셋에 %s 없음 (%s)" % (name, Path(dataset).expanduser()))
        return None
    data = read_json(p, None)
    if data is None:
        raise DatasetError("%s 읽기 실패(JSON 형식)" % p)
    return data


def expand(p, dataset=None):
    """데이터셋 설정 안의 경로 — `~` 풀기, 상대 경로는 데이터셋 폴더 기준."""
    if not p:
        return None
    q = Path(str(p)).expanduser()
    if not q.is_absolute() and dataset is not None:
        q = Path(dataset).expanduser() / q
    return q


@contextlib.contextmanager
def env(**kv):
    """환경변수를 잠시 바꾼다(None이면 지운다). 끝나면 원래대로."""
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# 평가가 지금 에이전트 세션 안에서 돌아도 하네스 훅이 그 세션으로 착각하지 않게 지우는 변수
SESSION_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID",
                "VMC_HARNESS_SESSION_ID", "VMC_HARNESS_NESTED")


@contextlib.contextmanager
def temp_project(files=None, git=False):
    """합성 임시 프로젝트(.harness/config.json) + 임시 하네스 홈. (root, home) — 끝나면 지운다."""
    tmp = Path(tempfile.mkdtemp(prefix="vmh-eval-"))
    try:
        root = tmp / "proj"
        root.mkdir()
        for rel, text in (files or {}).items():
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        (root / ".harness").mkdir()
        (root / ".harness" / "config.json").write_text(json.dumps(
            {"project_id": str(uuid.uuid4()), "profile": "standard", "source_roots": ["."]}), encoding="utf-8")
        if git:
            from vmc_harness.core.proc import run
            for args in (["init", "-q", "-b", "main"], ["config", "user.email", "eval@vmc"], ["config", "user.name", "eval"],
                         ["add", "-A"], ["commit", "-q", "-m", "init"]):
                run(["git"] + args, cwd=str(root))
        yield root, tmp / "home"
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


def home_env(home):
    """그 프로젝트의 하네스 홈으로 잠시 바꾼다. home이 없으면 지금 값 그대로."""
    return env(VMC_HARNESS_HOME=str(home)) if home else contextlib.nullcontext()
