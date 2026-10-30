"""QA 테스트 계정 (spec 05·09).

- 목록 `~/.vmc-harness/projects/<project_id>/qa-accounts.json` — 프로젝트 레포 밖, 비밀번호 없음(참조만)
- 비밀번호는 core.secrets(OS 키체인 / Windows 자격 증명 관리자). 이름 `qa:<project_id>:<계정 이름>`
- 입력은 터미널 직접(getpass) — 대화로 받지 않는다(01 P9). 출력은 마스킹
- 에이전트가 로그인할 때는 값을 출력하지 않고 클립보드로만 넘긴다(`qa accounts copy`) — 세션 기록에 평문이 남지 않게
"""

import re
import sys

from vmc_harness.core import secrets
from vmc_harness.core.clock import now_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.proc import run
from vmc_harness.core.project import local_dir, project_id

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


def accounts_path(root):
    return local_dir(root) / "qa-accounts.json"


def _load(root):
    data = read_json(accounts_path(root), None)
    if not isinstance(data, dict):
        data = {"accounts": []}
    data.setdefault("accounts", [])
    return data


def secret_name(root, name):
    return "qa:%s:%s" % (project_id(root), name)


def mask_value(v):
    v = str(v or "")
    if len(v) <= 2:
        return "*" * len(v)
    return v[:2] + "*" * max(3, len(v) - 2)


def add(root, name, username, password, role=None, note=None):
    if not NAME_RE.match(name or ""):
        raise ValueError("계정 이름 형식 오류")
    if not username:
        raise ValueError("아이디 없음")
    if not password:
        raise ValueError("비밀번호 없음")
    secrets.set_secret(secret_name(root, name), password)
    data = _load(root)
    data["accounts"] = [a for a in data["accounts"] if a.get("name") != name]
    data["accounts"].append({"name": name, "username": username, "role": role, "note": note,
                             "secret_ref": secret_name(root, name), "created_at": now_iso()})
    write_json(accounts_path(root), data)
    _chmod600(accounts_path(root))
    return listing(root)


def _chmod600(p):
    try:
        import os
        os.chmod(str(p), 0o600)
    except OSError:
        pass


def remove(root, name):
    data = _load(root)
    before = len(data["accounts"])
    data["accounts"] = [a for a in data["accounts"] if a.get("name") != name]
    if len(data["accounts"]) == before:
        return False
    write_json(accounts_path(root), data)
    try:
        secrets.delete_secret(secret_name(root, name))
    except Exception:
        pass
    return True


def listing(root):
    """마스킹된 목록 — 출력·에이전트용. 비밀번호 값은 없다."""
    out = []
    for a in _load(root)["accounts"]:
        out.append({"name": a.get("name"), "username": mask_value(a.get("username")),
                    "role": a.get("role"), "note": a.get("note")})
    return out


def get(root, name):
    for a in _load(root)["accounts"]:
        if a.get("name") == name:
            return a
    return None


def get_password(root, name):
    """프로그램(하네스가 띄우는 실행기)만 쓴다. 출력·로그에 쓰지 않는다."""
    if not get(root, name):
        return None
    return secrets.get_secret(secret_name(root, name))


def copy_to_clipboard(root, name, field="password"):
    """값을 출력하지 않고 클립보드로. (ok, reason)"""
    a = get(root, name)
    if not a:
        return False, "계정 없음: %s" % name
    value = a.get("username") if field == "username" else get_password(root, name)
    if not value:
        return False, "저장된 값 없음"
    if sys.platform == "darwin":
        rc, _, err = run(["pbcopy"], input_text=value)
    elif sys.platform == "win32":
        rc, _, err = run(["clip"], input_text=value)
    else:
        rc, _, err = run(["xclip", "-selection", "clipboard"], input_text=value)
    return (rc == 0), (None if rc == 0 else "클립보드 복사 실패")
