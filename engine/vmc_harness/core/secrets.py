"""OS 키체인 (macOS Keychain · Windows 자격 증명 관리자). 불가하면 파일 권한 600 대체(spec 09).

API(다른 영역이 이 이름으로 부른다 — 구현은 DB 영역 담당):
    set_secret(name, value) -> None
    get_secret(name) -> str | None
    delete_secret(name) -> None
    read_secret_from_terminal(prompt) -> str   # getpass — 대화로 받지 않는다(P9)
    backend() -> "keychain" | "wincred" | "file"   # 지금 PC에서 set_secret이 쓰는 저장소
name 예: "license", "db:<project_id>", "qa:<project_id>:<account>"

저장 방식
- macOS: `security -i`에 명령을 **표준 입력으로** 넘긴다(비밀값이 명령행 인자에 없어 `ps`에 안 보인다).
  값은 `-X <hex>`로 넘기고, 저장하는 값 자체는 `b64:` + base64(utf-8) — 비ASCII 값이 `-w` 출력에서
  hex로 바뀌는 문제를 피한다. 읽기(`find-generic-password -w`)의 인자는 서비스·계정 이름뿐이다.
  한계: 읽은 비밀값은 `security` 프로세스의 표준 출력 파이프로만 온다(인자·파일에 남지 않음).
- Windows: advapi32 CredWriteW/CredReadW/CredDeleteW(ctypes), CRED_TYPE_GENERIC, 대상 이름 `vmc-harness:<name>`.
- 둘 다 안 되면 `~/.vmc-harness/secrets/<name>` (폴더 700, 파일 600). Windows는 권한 비트가 무의미 — 사용자 프로필 폴더 ACL에 기댄다.
- 환경변수 VMC_HARNESS_SECRETS_BACKEND=keychain|wincred|file 로 강제할 수 있다(테스트용).
"""

import base64
import binascii
import os
import shutil
import subprocess
import sys
from urllib.parse import quote

from vmc_harness.core import paths

SERVICE = "vmc-harness"
_B64 = "b64:"


# ---------------------------------------------------------------- 공용

def _encode(value):
    return _B64 + base64.b64encode(value.encode("utf-8")).decode("ascii")


def _decode(stored):
    if stored is None:
        return None
    if stored.startswith(_B64):
        try:
            return base64.b64decode(stored[len(_B64):].encode("ascii")).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
    return stored


def _check_name(name):
    if not name or not isinstance(name, str) or any(c in name for c in "\r\n\"\\"):
        raise ValueError("비밀값 이름 형식 오류")


def backend():
    forced = os.environ.get("VMC_HARNESS_SECRETS_BACKEND")
    if forced in ("keychain", "wincred", "file"):
        return forced
    if sys.platform == "darwin" and shutil.which("security"):
        return "keychain"
    if sys.platform == "win32":
        return "wincred"
    return "file"


# ---------------------------------------------------------------- macOS 키체인

def _security_stdin(command_line):
    """`security -i`에 한 줄 명령을 표준 입력으로. (rc, out, err)"""
    try:
        r = subprocess.run(["security", "-i"], input=command_line + "\n", capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return -1, "", str(e)


def _kc_get(name):
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", name, "-w"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise OSError("keychain 접근 불가")
    if r.returncode == 0:
        return r.stdout.rstrip("\n")
    if r.returncode == 44:  # errSecItemNotFound
        return None
    raise OSError("keychain 읽기 실패(rc=%s)" % r.returncode)


def _kc_set(name, value):
    stored = _encode(value)
    hexval = binascii.hexlify(stored.encode("ascii")).decode("ascii")
    line = 'add-generic-password -U -s "%s" -a "%s" -l "%s" -X %s' % (SERVICE, name, SERVICE + ":" + name, hexval)
    _security_stdin(line)
    # `security -i`는 실패해도 rc 0일 수 있어 다시 읽어 확인한다
    if _kc_get(name) != stored:
        raise OSError("keychain 저장 확인 실패")


def _kc_delete(name):
    try:
        subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", name],
                       capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


# ---------------------------------------------------------------- Windows 자격 증명 관리자

def _wincred():
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    adv.CredWriteW.restype = wintypes.BOOL
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredDeleteW.restype = wintypes.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]
    adv.CredFree.restype = None
    return ctypes, adv, CREDENTIALW


_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


def _wc_target(name):
    return "%s:%s" % (SERVICE, name)


def _wc_set(name, value):
    ctypes, adv, CREDENTIALW = _wincred()
    blob = _encode(value).encode("utf-8")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = CREDENTIALW()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = _wc_target(name)
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = name
    if not adv.CredWriteW(ctypes.byref(cred), 0):
        raise OSError("CredWriteW 실패(%s)" % ctypes.get_last_error())


def _wc_get(name):
    ctypes, adv, CREDENTIALW = _wincred()
    p = ctypes.POINTER(CREDENTIALW)()
    if not adv.CredReadW(_wc_target(name), _CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
        err = ctypes.get_last_error()
        if err == _ERROR_NOT_FOUND:
            return None
        raise OSError("CredReadW 실패(%s)" % err)
    try:
        c = p.contents
        raw = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
    finally:
        adv.CredFree(p)
    return raw.decode("utf-8", "replace")


def _wc_delete(name):
    ctypes, adv, _ = _wincred()
    adv.CredDeleteW(_wc_target(name), _CRED_TYPE_GENERIC, 0)


# ---------------------------------------------------------------- 파일 대체

def _file_dir():
    return paths.home() / "secrets"


def _file_path(name):
    return _file_dir() / quote(name, safe="")


def _file_set(name, value):
    d = _file_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(d), 0o700)
    except OSError:
        pass
    p = _file_path(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(p), flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(_encode(value))
    try:
        os.chmod(str(p), 0o600)
    except OSError:
        pass


def _file_get(name):
    p = _file_path(name)
    if not p.is_file():
        return None
    return p.read_text(encoding="utf-8").strip()


def _file_delete(name):
    p = _file_path(name)
    try:
        p.unlink()
    except OSError:
        pass


_BACKENDS = {
    "keychain": (_kc_set, _kc_get, _kc_delete),
    "wincred": (_wc_set, _wc_get, _wc_delete),
    "file": (_file_set, _file_get, _file_delete),
}


# ---------------------------------------------------------------- API

def set_secret(name, value):
    """OS 저장소에 저장. 실패하면 파일(600)로 대체한다. 실제로 쓴 저장소 이름을 돌려준다."""
    _check_name(name)
    if value is None:
        raise ValueError("비밀값 없음")
    b = backend()
    if b != "file":
        try:
            _BACKENDS[b][0](name, value)
            _file_delete(name)  # 예전 대체 파일이 남아 있으면 지운다
            return b
        except Exception:
            pass
    _file_set(name, value)
    return "file"


def get_secret(name):
    _check_name(name)
    b = backend()
    if b != "file":
        try:
            v = _BACKENDS[b][1](name)
            if v is not None:
                return _decode(v)
        except Exception:
            pass
    return _decode(_file_get(name))


def delete_secret(name):
    _check_name(name)
    b = backend()
    if b != "file":
        try:
            _BACKENDS[b][2](name)
        except Exception:
            pass
    _file_delete(name)


def read_secret_from_terminal(prompt):
    """터미널 직접 입력만 받는다 — 표준 입력이 터미널이 아니면(에이전트가 파이프로 넣는 경우) 거부."""
    import getpass
    if not sys.stdin or not sys.stdin.isatty():
        raise RuntimeError("비밀값은 터미널 직접 입력만 받음")
    return getpass.getpass(prompt)
