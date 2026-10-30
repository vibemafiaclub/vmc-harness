"""VMC 하네스 CLI 껍데기 (spec 10). **이 파일 하나로 혼자 돈다** — vmc_harness 패키지를 import하지 않는다(커널이 없을 때도 activate가 돌아야 하므로).

한 벌의 원본이 세 곳에서 쓰인다:
- 설치된 껍데기 `~/.vmc-harness/bin/vmc_harness_shell.py` (install.py가 이 소스를 그대로 쓴다)
- 설치 스크립트 `install/install.py` (release.py가 이 소스를 base64로 넣어 렌더 — 서버가 서빙)
- 엔진 `vmc_harness.dist.*` (kernel·server·updater·uninstall이 여기 함수를 부른다)

껍데기가 하는 일: `activate`(키 확인 → 커널 받기·검증·설치)만 직접 하고, 나머지 명령은
`~/.vmc-harness/kernel/<current>/engine`의 엔진(`python -m vmc_harness`와 같은 진입점)에 넘긴다. 커널이 없으면 activate를 먼저 하라고만 말한다.

설치 배치 (`~/.vmc-harness/`, VMC_HARNESS_HOME으로 바꿀 수 있다)
  bin/vmc_harness_shell.py, bin/vmc-harness(맥·리눅스) | bin/vmc-harness.cmd(윈도우)
  kernel/<버전>/  릴리스 zip을 푼 것(읽기 전용). engine/vmc_harness/, manifest.json, .installed.json
  kernel/current  지금 버전 이름이 든 텍스트 파일(심볼릭 링크 대신 — 윈도우 권한 문제 없이 원자적으로 바꾼다)
  license.json    license_id·org·expires_at(키 원문은 OS 키체인 — core/secrets.py, 없으면 secrets/license 600)
  pc.json, update.json

오프라인 번들 서명(activate --offline): HMAC-SHA256, 키 = sha256(라이선스 키) 16진 문자열.
  - 표준 라이브러리만으로 검증할 수 있고(Ed25519 없음), VMC 서버는 키 해시만 갖고 있어 발급 때 키 원문 없이 번들을 만들 수 있다
  - 막는 것: 전달 중(USB 등) 번들 변조, 다른 고객 키로 받은 번들, 만료된 라이선스의 번들
  - 못 막는 것: 그 키를 가진 사람(고객 자신)이 번들을 위조하는 것 — 고객 PC에 풀린 코드는 어차피 고객이 고칠 수 있어 위협이 아니다(spec 10 소스 보호)
"""

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import uuid
from pathlib import Path

# compileall·zipfile은 설치·검증 때만 — 훅마다 뜨는 껍데기가 읽지 않게 함수 안에서 import한다(고객 Windows 훅 기동 비용 E2)

SHELL_VERSION = "0.1.0"
DEFAULT_BASE_URL = "https://api.vibemafiaclub.com"  # = config server.base_url (엔진 테스트가 같은지 확인한다)
# 껍데기 단독 실행(커널 없음) 때만 쓰는 값. 엔진은 config(dist.toml)의 값을 넘긴다 — 엔진 테스트가 같은지 확인한다
CALL_TIMEOUT_S = 10
DOWNLOAD_TIMEOUT_S = 300
KEEP_VERSIONS = 2

KEY_RE = re.compile(r"^vmh_[A-Za-z0-9_-]{43}$")
_KEY_STRIP = "\"'<>`“”‘’"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$")
SECRET_NAME = "license"
PATH_MARKER = "# vmc-harness"
MANIFEST = "manifest.json"
INSTALLED = ".installed.json"
BUNDLE_JSON = "bundle.json"
BUNDLE_SIG = "bundle.sig"


class HarnessError(Exception):
    """사람에게 보일 한 줄(무엇이 안 됐는지)."""


class Unreachable(HarnessError):
    pass


# ---------------------------------------------------------------- 경로

def home():
    p = os.environ.get("VMC_HARNESS_HOME")
    return Path(p).expanduser() if p else Path.home() / ".vmc-harness"


def bin_dir():
    return home() / "bin"


def kernel_root():
    return home() / "kernel"


def is_windows():
    return sys.platform == "win32"


def launcher_path():
    return bin_dir() / ("vmc-harness.cmd" if is_windows() else "vmc-harness")


def shell_path():
    return bin_dir() / "vmc_harness_shell.py"


def base_url(default=None):
    return (os.environ.get("VMC_HARNESS_SERVER") or default or DEFAULT_BASE_URL).rstrip("/")


def now_iso():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_iso(s):
    if not s:
        return None
    try:
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)


def _read_json(p, default=None):
    try:
        with open(str(p), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_text_atomic(p, text):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, str(p))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_json(p, data):
    _write_text_atomic(p, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


# ---------------------------------------------------------------- PC·라이선스

def pc_id():
    """core.project.pc_id와 같은 파일·형식(~/.vmc-harness/pc.json)."""
    p = home() / "pc.json"
    data = _read_json(p)
    if not data or not data.get("pc_id"):
        data = {"pc_id": str(uuid.uuid4()), "os": platform.system().lower(), "created_at": now_iso()}
        _write_json(p, data)
    return data["pc_id"]


def os_name():
    return platform.system().lower()


def license_file():
    return home() / "license.json"


def read_license():
    return _read_json(license_file())


def _secrets_module():
    """엔진의 core.secrets(키체인). 엔진이 없으면 None — 껍데기 단독일 때는 같은 형식의 파일로 대신한다."""
    try:
        from vmc_harness.core import secrets  # noqa: 엔진 안이거나 커널 경로가 sys.path에 있을 때
        return secrets
    except Exception:
        pass
    d = current_dir()
    if d and (d / "engine").is_dir():
        sys.path.insert(0, str(d / "engine"))
        try:
            from vmc_harness.core import secrets
            return secrets
        except Exception:
            return None
    return None


def _secret_file():
    # core.secrets 파일 대체와 같은 위치·형식("b64:" + base64)
    return home() / "secrets" / SECRET_NAME


def store_key(key):
    """키 원문을 OS 키체인에(안 되면 파일 600). 실제로 쓴 저장소 이름."""
    sec = _secrets_module()
    if sec is not None:
        try:
            used = sec.set_secret(SECRET_NAME, key)
            return used or "keychain"
        except NotImplementedError:
            pass
        except Exception:
            pass
    p = _secret_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(p.parent), 0o700)
    except OSError:
        pass
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("b64:" + base64.b64encode(key.encode("utf-8")).decode("ascii"))
    try:
        os.chmod(str(p), 0o600)
    except OSError:
        pass
    return "file"


def load_key():
    sec = _secrets_module()
    if sec is not None:
        try:
            v = sec.get_secret(SECRET_NAME)
            if v:
                return v
        except Exception:
            pass
    p = _secret_file()
    if p.is_file():
        raw = p.read_text(encoding="utf-8").strip()
        if raw.startswith("b64:"):
            try:
                return base64.b64decode(raw[4:]).decode("utf-8")
            except ValueError:
                return None
        return raw or None
    return None


def forget_key():
    sec = _secrets_module()
    if sec is not None:
        try:
            sec.delete_secret(SECRET_NAME)
        except Exception:
            pass
    try:
        _secret_file().unlink()
    except OSError:
        pass


def key_hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- HTTP

def http(method, url, body=None, key=None, timeout=CALL_TIMEOUT_S, headers=None, to_file=None):
    """(status, data). data: JSON이면 dict, to_file이면 (sha256, size). 서버에 못 닿으면 Unreachable."""
    import socket
    import urllib.error
    import urllib.request
    h = {"User-Agent": "vmc-harness-shell/%s (%s)" % (SHELL_VERSION, os_name()), "Accept": "application/json"}
    if key:
        h["Authorization"] = "Bearer " + key
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read() or b""
        finally:
            e.close()
        try:
            return e.code, json.loads(raw.decode("utf-8"))
        except ValueError:
            return e.code, {"error": raw[:200].decode("utf-8", "replace")}
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        raise Unreachable("서버에 닿지 않음: %s" % (getattr(e, "reason", None) or e))
    with resp:
        if to_file is not None:
            h256 = hashlib.sha256()
            size = 0
            try:
                with open(str(to_file), "wb") as f:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        h256.update(chunk)
                        size += len(chunk)
                        f.write(chunk)
            except (socket.timeout, ConnectionError, OSError) as e:
                raise Unreachable("내려받기 중단: %s" % e)
            return resp.status, (h256.hexdigest(), size)
        raw = resp.read()
        try:
            return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            return resp.status, {"text": raw.decode("utf-8", "replace")}


def api(method, path, body=None, key=None, timeout=CALL_TIMEOUT_S, base=None, headers=None, to_file=None):
    return http(method, base_url(base) + path, body=body, key=key, timeout=timeout, headers=headers, to_file=to_file)


# ---------------------------------------------------------------- 커널 설치

def current_version():
    p = kernel_root() / "current"
    try:
        v = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if v and VERSION_RE.match(v) and (kernel_root() / v / INSTALLED).is_file():
        return v
    return None


def current_dir():
    v = current_version()
    return kernel_root() / v if v else None


def engine_dir(version=None):
    v = version or current_version()
    return kernel_root() / v / "engine" if v else None


def installed_versions():
    """완전히 설치된 버전 [(installed_at, version)] 오래된 순."""
    out = []
    root = kernel_root()
    if not root.is_dir():
        return out
    for d in root.iterdir():
        if d.is_dir() and VERSION_RE.match(d.name):
            meta = _read_json(d / INSTALLED)
            if meta:
                out.append((meta.get("installed_at") or "", d.name))
    return sorted(out)


def set_current(version):
    _write_text_atomic(kernel_root() / "current", version + "\n")


def _safe_member(name):
    if name.startswith(("/", "\\")) or ":" in name.split("/")[0]:
        return False
    parts = name.replace("\\", "/").split("/")
    return ".." not in parts


def verify_release_zip(zip_path, expected_version=None):
    """zip 안 manifest.json의 파일별 sha256을 대조한다. manifest dict 또는 HarnessError."""
    import zipfile
    try:
        zf = zipfile.ZipFile(str(zip_path))
    except (zipfile.BadZipFile, OSError):
        raise HarnessError("릴리스 파일이 zip이 아님")
    with zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if MANIFEST not in names:
            raise HarnessError("릴리스에 manifest.json 없음")
        try:
            manifest = json.loads(zf.read(MANIFEST).decode("utf-8"))
        except ValueError:
            raise HarnessError("manifest.json 형식 오류")
        files = manifest.get("files") or {}
        version = manifest.get("version")
        if not version or not VERSION_RE.match(version):
            raise HarnessError("manifest 버전 형식 오류")
        if expected_version and version != expected_version:
            raise HarnessError("manifest 버전 불일치: %s ≠ %s" % (version, expected_version))
        for n in names:
            if not _safe_member(n):
                raise HarnessError("허용되지 않는 경로: %s" % n)
            if n == MANIFEST:
                continue
            want = files.get(n)
            if not want:
                raise HarnessError("manifest에 없는 파일: %s" % n)
            if hashlib.sha256(zf.read(n)).hexdigest() != want:
                raise HarnessError("파일 해시 불일치: %s" % n)
        missing = sorted(set(files) - set(names))
        if missing:
            raise HarnessError("manifest의 파일이 zip에 없음: %s" % missing[0])
    return manifest


def _chmod_tree(root, writable):
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                os.chmod(p, (stat.S_IREAD | stat.S_IWRITE) if writable else stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
            except OSError:
                pass
    # 폴더는 파일 다음에(읽기 전용 폴더 안의 파일 권한을 바꿀 수 있게), 깊은 것부터
    dirs = [root] + [Path(dp) / d for dp, dns, _ in os.walk(str(root)) for d in dns]
    for d in sorted(dirs, key=lambda x: -len(x.parts)) if not writable else dirs:
        try:
            os.chmod(str(d), 0o755 if writable else 0o555)
        except OSError:
            pass


def make_readonly(path):
    _chmod_tree(path, writable=False)


def make_writable(path):
    # 위에서부터 열어야 안쪽에 닿는다
    path = Path(path)
    if not path.exists():
        return
    try:
        os.chmod(str(path), 0o755)
    except OSError:
        pass
    for dirpath, dirnames, filenames in os.walk(str(path)):
        for d in dirnames:
            try:
                os.chmod(os.path.join(dirpath, d), 0o755)
            except OSError:
                pass
        for fn in filenames:
            try:
                os.chmod(os.path.join(dirpath, fn), stat.S_IREAD | stat.S_IWRITE)
            except OSError:
                pass


def rmtree(path):
    path = Path(path)
    if not path.exists():
        return
    make_writable(path)

    def retry(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(str(path), onexc=retry)
    else:
        shutil.rmtree(str(path), onerror=retry)


def install_release(zip_path, expected_version=None, expected_sha256=None, keep=KEEP_VERSIONS):
    """검증한 zip을 kernel/<버전>/에 풀고 읽기 전용으로 만든 뒤 current를 바꾼다. 설치한 버전."""
    if expected_sha256:
        h = hashlib.sha256()
        with open(str(zip_path), "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        if h.hexdigest() != expected_sha256:
            raise HarnessError("내려받은 파일의 sha256이 서버 값과 다름")
    manifest = verify_release_zip(zip_path, expected_version)
    version = manifest["version"]
    root = kernel_root()
    root.mkdir(parents=True, exist_ok=True)
    final = root / version
    if (final / INSTALLED).is_file():
        set_current(version)
        prune(keep)
        return version
    import compileall
    import zipfile
    tmp = Path(tempfile.mkdtemp(dir=str(root), prefix=".tmp-%s-" % version))
    try:
        with zipfile.ZipFile(str(zip_path)) as zf:
            for n in zf.namelist():
                if not _safe_member(n):
                    raise HarnessError("허용되지 않는 경로: %s" % n)
            zf.extractall(str(tmp))
        eng = tmp / "engine"
        if eng.is_dir():
            # 읽기 전용이라 실행 중엔 .pyc를 못 쓴다 — 미리 컴파일해 훅마다 다시 컴파일하지 않게
            compileall.compile_dir(str(eng), quiet=1)
        _write_json(tmp / INSTALLED, {"version": version, "installed_at": now_iso(),
                                      "sha256": expected_sha256, "python": sys.executable})
        make_readonly(tmp)
        os.chmod(str(tmp), 0o755)  # 맥은 폴더 이름을 바꾸려면 그 폴더에 쓰기 권한이 있어야 한다 — 옮긴 뒤 닫는다
        if (final / INSTALLED).is_file():
            rmtree(tmp)  # 다른 프로세스가 같은 버전을 먼저 끝냈다
        else:
            if final.exists():
                rmtree(final)  # 설치가 덜 끝난 같은 버전
            os.replace(str(tmp), str(final))
            try:
                os.chmod(str(final), 0o555)
            except OSError:
                pass
    except HarnessError:
        rmtree(tmp)
        raise
    except BaseException as e:
        rmtree(tmp)
        if isinstance(e, OSError):
            raise HarnessError("커널 설치 실패: %s" % e)
        raise
    set_current(version)
    prune(keep)
    return version


def prune(keep=KEEP_VERSIONS):
    """current와 최근 버전 keep개만 남긴다. 지우다 실패하면(윈도우에서 사용 중) 다음에 다시."""
    cur = current_version()
    vers = [v for _, v in installed_versions()]
    others = [v for v in vers if v != cur]
    remove = others[:max(0, len(others) - max(0, keep - 1))]
    for v in remove:
        rmtree(kernel_root() / v)
    # 중단된 설치 찌꺼기 — 다른 프로세스가 지금 쓰는 중일 수 있어 하루 지난 것만
    if kernel_root().is_dir():
        import time
        for d in list(kernel_root().glob(".tmp-*")) + list(kernel_root().glob(".dl-*")):
            try:
                if time.time() - d.stat().st_mtime > 86400:
                    rmtree(d) if d.is_dir() else d.unlink()
            except OSError:
                continue


# ---------------------------------------------------------------- 활성화·업데이트

def _write_license(info, mode, store):
    _write_json(license_file(), {"license_id": info.get("license_id"), "org": info.get("org"),
                                 "expires_at": info.get("expires_at"), "activated_at": now_iso(),
                                 "mode": mode, "key_store": store})


def update_now(key=None, base=None, check_timeout=CALL_TIMEOUT_S, download_timeout=DOWNLOAD_TIMEOUT_S, keep=KEEP_VERSIONS):
    """서버 최신 버전을 확인해 다르면 받아 설치한다. 서버에 못 닿거나 키가 거부되면 지금 버전 그대로.
    결과 {"status": updated|current|unreachable|unauthorized|no_license|no_release|error, ...}"""
    key = key or load_key()
    if not key:
        return {"status": "no_license"}
    try:
        st, info = api("GET", "/harness/releases/latest", key=key, timeout=check_timeout, base=base)
    except Unreachable as e:
        return {"status": "unreachable", "detail": str(e)}
    if st == 401:
        return {"status": "unauthorized", "reason": (info or {}).get("reason")}
    if st == 404:
        return {"status": "no_release"}
    if st != 200 or not isinstance(info, dict) or not VERSION_RE.match(str(info.get("version", ""))):
        return {"status": "error", "detail": "latest 응답 %s" % st}
    version, cur = info["version"], current_version()
    if version == cur:
        return {"status": "current", "version": cur}
    if (kernel_root() / version / INSTALLED).is_file():
        set_current(version)
        return {"status": "updated", "version": version, "from": cur}
    kernel_root().mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(kernel_root()), prefix=".dl-", suffix=".zip")
    os.close(fd)
    try:
        try:
            st, got = api("GET", "/harness/releases/%s/download" % version, key=key, timeout=download_timeout, base=base,
                          headers={"X-VMC-PC-ID": pc_id(), "X-VMC-OS": os_name(), "Accept": "application/zip"}, to_file=tmp)
        except Unreachable as e:
            return {"status": "unreachable", "detail": str(e)}
        if st != 200:
            return {"status": "unauthorized" if st == 401 else "error", "detail": "download 응답 %s" % st}
        sha, size = got
        if info.get("size") and int(info["size"]) != size:
            return {"status": "error", "detail": "크기 불일치"}
        if sha != info.get("sha256"):
            return {"status": "error", "detail": "sha256 불일치"}
        try:
            install_release(tmp, version, info.get("sha256"), keep=keep)
        except HarnessError as e:
            return {"status": "error", "detail": str(e)}
        return {"status": "updated", "version": version, "from": cur}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def clean_key(raw):
    """붙여 넣으며 섞인 것을 걷어낸다 — 공백·줄바꿈·보이지 않는 문자(메일 본문의 줄바꿈 힌트·soft hyphen·NBSP)와 감싼 따옴표·<>."""
    import unicodedata
    s = "".join("-" if c in "\u2010\u2011\u2012\u2013\u2014\u2212" else c for c in (raw or "")
                if not (c.isspace() or unicodedata.category(c) in ("Cf", "Zs", "Cc")))
    return s.strip(_KEY_STRIP)


def key_problem(key):
    """형식이 틀린 이유 한 줄(키 원문은 보이지 않는다). 맞으면 None."""
    if KEY_RE.match(key or ""):
        return None
    if not key:
        return "라이선스 키가 비어 있음"
    if not key.startswith("vmh_"):
        return "라이선스 키 형식이 아님 — vmh_로 시작해야 함(입력은 %r로 시작)" % key[:4]
    bad = [(i + 1, c) for i, c in enumerate(key[4:], 4) if not (c.isascii() and (c.isalnum() or c in "_-"))]
    if bad:
        i, c = bad[0]
        return "라이선스 키 형식이 아님 — %d번째 글자가 허용되지 않는 문자(U+%04X). 메일에서 복사했다면 키만 다시 붙여 넣기" % (i, ord(c))
    return "라이선스 키 형식이 아님 — 47자여야 하는데 %d자. 일부만 복사됐거나 덧붙은 글자가 있음" % len(key)


def activate(key, base=None, out=print, check_timeout=CALL_TIMEOUT_S, download_timeout=DOWNLOAD_TIMEOUT_S, keep=KEEP_VERSIONS):
    """온라인 활성화. 종료 코드(0 성공)."""
    problem = key_problem(key)
    if problem:
        out(problem)
        return 2
    try:
        st, info = api("POST", "/harness/activate", body={"pc_id": pc_id(), "os": os_name(), "cli_version": SHELL_VERSION},
                       key=key, timeout=check_timeout, base=base)
    except Unreachable as e:
        out("%s — 사내망에서 막혔으면 오프라인 번들로: vmc-harness activate --offline <키> --bundle <zip>" % e)
        return 3
    if st == 401:
        out("라이선스 키가 거부됨(%s)" % (info or {}).get("reason", "unauthorized"))
        return 2
    if st != 200 or not isinstance(info, dict) or not info.get("license_id"):
        out("활성화 실패(서버 응답 %s)" % st)
        return 1
    store = store_key(key)
    _write_license(info, "online", store)
    r = update_now(key=key, base=base, check_timeout=check_timeout, download_timeout=download_timeout, keep=keep)
    if r["status"] not in ("updated", "current"):
        out("라이선스는 확인했지만 커널을 받지 못함(%s %s)" % (r["status"], r.get("detail") or r.get("reason") or ""))
        return 1
    store = store_key(key)  # 커널이 생겼으니 키체인으로 옮긴다(파일 대체를 지운다)
    _write_license(info, "online", store)
    out("활성화 완료: %s · 커널 %s" % (info.get("org"), current_version()))
    return 0


def sign_bundle(bundle_json_bytes, key_hash_hex):
    return hmac.new(key_hash_hex.encode("ascii"), bundle_json_bytes, hashlib.sha256).hexdigest()


def activate_offline(key, bundle_path, out=print, keep=KEEP_VERSIONS):
    """오프라인 번들 활성화(서버 없이). 종료 코드."""
    problem = key_problem(key)
    if problem:
        out(problem)
        return 2
    import zipfile
    try:
        zf = zipfile.ZipFile(str(bundle_path))
    except (zipfile.BadZipFile, OSError):
        out("번들 파일을 열 수 없음: %s" % bundle_path)
        return 2
    with zf:
        names = set(zf.namelist())
        if BUNDLE_JSON not in names or BUNDLE_SIG not in names:
            out("오프라인 번들이 아님(bundle.json·bundle.sig 없음)")
            return 2
        raw = zf.read(BUNDLE_JSON)
        sig = zf.read(BUNDLE_SIG).decode("ascii", "replace").strip()
        if not hmac.compare_digest(sign_bundle(raw, key_hash(key)), sig):
            out("번들 서명이 이 키와 맞지 않음(다른 고객 번들이거나 변조됨)")
            return 2
        meta = json.loads(raw.decode("utf-8"))
        exp = _parse_iso(meta.get("expires_at"))
        if exp and exp <= _dt.datetime.now(_dt.timezone.utc):
            out("라이선스가 만료됨(%s)" % meta.get("expires_at"))
            return 2
        rel = meta.get("release_file")
        if not rel or rel not in names:
            out("번들 안에 릴리스 파일 없음")
            return 2
        kernel_root().mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(kernel_root()), prefix=".dl-", suffix=".zip")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(zf.read(rel))
            try:
                install_release(tmp, meta.get("version"), meta.get("sha256"), keep=keep)
            except HarnessError as e:
                out("번들 검증 실패: %s" % e)
                return 2
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    store = store_key(key)
    _write_license(meta, "offline", store)
    out("오프라인 활성화 완료: %s · 커널 %s" % (meta.get("org"), current_version()))
    return 0


def activate_main(argv, out=print, **kw):
    """`activate [--offline] [<키>] [--bundle <zip>]`. 키를 생략하면 터미널에서 직접 받는다(화면·기록에 안 남게)."""
    import argparse
    p = argparse.ArgumentParser(prog="vmc-harness activate", description="라이선스 키로 하네스를 활성화한다")
    p.add_argument("key", nargs="?", help="라이선스 키(vmh_…). 생략하면 입력을 받는다")
    p.add_argument("--offline", action="store_true", help="서버 없이 오프라인 번들로")
    p.add_argument("--bundle", help="오프라인 번들 zip 경로")
    a = p.parse_args(argv)
    key = a.key
    if not key:
        import getpass
        key = getpass.getpass("라이선스 키: ")
    key = clean_key(key)
    if a.offline:
        if not a.bundle:
            out("--offline에는 --bundle <zip>이 필요")
            return 2
        return activate_offline(key, a.bundle, out=out, keep=kw.get("keep", KEEP_VERSIONS))
    return activate(key, out=out, **kw)


# ---------------------------------------------------------------- 설치(껍데기)·PATH

def _rc_files():
    """PATH 줄을 넣을 셸 시작 파일. zsh면 .zshrc, bash면 .bashrc+.bash_profile, 그 외 .profile."""
    h = Path.home()
    sh = os.path.basename(os.environ.get("SHELL", ""))
    if sh == "zsh":
        return [h / ".zshrc"]
    if sh == "bash":
        return [h / ".bashrc", h / ".bash_profile"]
    return [h / ".profile"]


def _all_rc_files():
    h = Path.home()
    return [h / ".zshrc", h / ".bashrc", h / ".bash_profile", h / ".profile"]


def _posix_path_line():
    return 'export PATH="%s:$PATH"  %s' % (str(bin_dir()).replace('"', '\\"'), PATH_MARKER)


def register_path():
    """PATH에 bin/을 넣는다. 고친 곳 목록."""
    if is_windows():
        return _win_path(add=True)
    changed = []
    line = _posix_path_line()
    for rc in _rc_files():
        text = rc.read_text(encoding="utf-8") if rc.is_file() else ""
        if any(l.strip() == line.strip() for l in text.splitlines()):
            continue
        with open(str(rc), "a", encoding="utf-8") as f:
            f.write(("" if not text or text.endswith("\n") else "\n") + line + "\n")
        changed.append(str(rc))
    return changed


def unregister_path():
    if is_windows():
        return _win_path(add=False)
    changed = []
    for rc in _all_rc_files():
        if not rc.is_file():
            continue
        lines = rc.read_text(encoding="utf-8").splitlines(True)
        keep = [l for l in lines if not (l.rstrip().endswith(PATH_MARKER) and str(bin_dir()) in l)]
        if len(keep) != len(lines):
            rc.write_text("".join(keep), encoding="utf-8")
            changed.append(str(rc))
    return changed


def _win_path(add):
    """사용자 PATH(HKCU\\Environment\\Path)를 직접 고친다. setx는 1024자에서 잘라 PATH를 망가뜨릴 수 있어 쓰지 않는다."""
    import winreg  # noqa: 윈도우 전용
    target = str(bin_dir())
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
        try:
            value, typ = winreg.QueryValueEx(k, "Path")
        except FileNotFoundError:
            value, typ = "", winreg.REG_EXPAND_SZ
        parts = [p for p in (value or "").split(";") if p]
        norm = [os.path.normcase(os.path.normpath(os.path.expandvars(p))) for p in parts]
        mine = os.path.normcase(os.path.normpath(target))
        if add:
            if mine in norm:
                return []
            parts.append(target)
        else:
            if mine not in norm:
                return []
            parts = [p for p, n in zip(parts, norm) if n != mine]
        if typ not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            typ = winreg.REG_EXPAND_SZ
        winreg.SetValueEx(k, "Path", 0, typ, ";".join(parts))
    try:  # 새로 여는 터미널이 바뀐 PATH를 보게 알린다
        import ctypes
        res = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(res))
    except Exception:
        pass
    return ["HKCU\\Environment\\Path"]


def _launcher_text():
    py = sys.executable or ("python" if is_windows() else "python3")
    if is_windows():
        return ('@echo off\r\nset "VMH_PY=%s"\r\nif not exist "%%VMH_PY%%" set "VMH_PY=python"\r\n'
                '"%%VMH_PY%%" "%%~dp0vmc_harness_shell.py" %%*\r\n') % py
    q = lambda s: "'" + s.replace("'", "'\\''") + "'"
    return ('#!/bin/sh\n# VMC 하네스 CLI 껍데기 실행기 (install.py가 만든다)\nVMH_PY=%s\n'
            '[ -x "$VMH_PY" ] || VMH_PY="$(command -v python3)"\nexec "$VMH_PY" %s "$@"\n') % (q(py), q(str(shell_path())))


def install_self(source, argv=None, out=print):
    """install.py가 부른다: 껍데기 소스·실행기를 bin/에 쓰고 PATH에 등록한다."""
    import argparse
    p = argparse.ArgumentParser(prog="install.py", description="VMC 하네스 CLI 설치")
    p.add_argument("--no-path", action="store_true", help="PATH 등록을 건너뛴다")
    a = p.parse_args(argv or [])
    if sys.version_info < (3, 9):
        out("Python 3.9 이상이 필요")
        return 2
    bin_dir().mkdir(parents=True, exist_ok=True)
    _write_text_atomic(shell_path(), source)
    lp = launcher_path()
    _write_text_atomic(lp, _launcher_text())
    if not is_windows():
        os.chmod(str(lp), 0o755)
    changed = [] if a.no_path else register_path()
    out("VMC 하네스 CLI 설치: %s" % lp)
    if changed:
        out("PATH 등록: %s — 새 터미널을 열면 적용된다" % ", ".join(changed))
    out("다음: vmc-harness activate <라이선스 키>")
    return 0


# ---------------------------------------------------------------- 실행

def _utf8_stdio():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def main(argv=None):
    _utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["activate"]:
        return activate_main(argv[1:])
    if argv[:1] == ["--shell-version"]:
        print(SHELL_VERSION)
        return 0
    eng = engine_dir()
    if not eng or not eng.is_dir():
        print("하네스 커널이 아직 없음 — 먼저: vmc-harness activate <라이선스 키>", file=sys.stderr)
        return 2
    # 엔진이 띄우는 자식 프로세스(python -m vmc_harness …)도 같은 커널을 보게 한다
    os.environ["PYTHONPATH"] = str(eng) + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    sys.path.insert(0, str(eng))
    from vmc_harness.cli import main as engine_main
    return engine_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
