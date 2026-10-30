#!/usr/bin/env python3
"""하네스 릴리스 (spec 10·11). VMC 맥에서 돌린다. 표준 라이브러리만.

  python3 scripts/release.py build [--out DIR]                      zip만 만든다(업로드 없음)
  python3 scripts/release.py render-install [--out PATH]            서버가 서빙할 install.py(껍데기 소스를 넣고 ASCII로)
  python3 scripts/release.py publish --eval-passed [--accept-open ID,..] [--notes ".."] [--dry-run] [--context-hub PATH] [--allow-dirty]
        zip 빌드 → R2 업로드 → harness_releases 행 추가(context-hub packages/db 스크립트) → install.py 업로드
        회귀 세트(spec 11)를 통과한 버전만 올린다 — --eval-passed가 없으면 거부한다. 행이 생기는 순간 고객 PC 다음 세션이 받는다
        출고 기준 scripts/release-gate.json: 시나리오 테스트 파일이 없거나, 고객 환경 미확인(open) 항목을 --accept-open으로
        하나하나 인정하지 않으면 거부한다. 인정한 항목은 manifest.unverified·릴리스 노트에 '시험 기능'으로 실린다
        환경변수: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, (R2_BUCKET=vmc-harness-releases), DATABASE_URL(없으면 ~/.config/vmc-db/env)
  <bundle-info JSON> | python3 scripts/release.py bundle --zip <릴리스 zip> [--out DIR]
        오프라인 번들(USB 전달용). bundle-info는 context-hub에서:
          pnpm -C packages/db exec tsx scripts/harness-license.ts bundle-info <license_id> | python3 scripts/release.py bundle --zip dist/vmc-harness-0.1.0.zip
        bundle-info의 key_hash는 번들 서명 키다 — 파일로 저장하지 않고 파이프로만 넘긴다

zip 구성: manifest.json(버전·파일별 sha256·eval_passed·git 커밋), engine/vmc_harness/**, install/install.py(렌더본), mod/**(있으면), PRINCIPLES.md·docs/kernel-writing.md(있으면)
"""

import argparse
import base64
import datetime as _dt
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine"
sys.path.insert(0, str(ENGINE))
from vmc_harness.dist import shell  # noqa: E402 — 검증·서명 규칙을 껍데기와 한 벌로

R2_RELEASE_PREFIX = "releases/"
R2_INSTALL_KEY = "install/install.py"  # = packages/db HARNESS_INSTALL_R2_KEY (서버가 이 키를 서빙한다)
EXCLUDE_DIRS = {"__pycache__", "node_modules", ".git", ".next", "tests", ".pytest_cache"}
EXCLUDE_FILES = {".DS_Store"}
ZIP_DATE = (2026, 1, 1, 0, 0, 0)


def version():
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (ENGINE / "vmc_harness" / "__init__.py").read_text(encoding="utf-8"))
    return m.group(1)


def git_commit():
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "engine", "install", "mod", "scripts"],
                               cwd=str(ROOT), capture_output=True, text=True).stdout.strip()
        return sha or None, bool(dirty)
    except OSError:
        return None, True


def render_install():
    """install/install.py에 껍데기 소스를 base64로 넣고, 비ASCII를 \\u 이스케이프로(PowerShell 5.1 파이프가 ASCII라서)."""
    src = (ROOT / "install" / "install.py").read_text(encoding="utf-8")
    shell_src = (ENGINE / "vmc_harness" / "dist" / "shell.py").read_text(encoding="utf-8")
    b64 = base64.b64encode(shell_src.encode("utf-8")).decode("ascii")
    out, n = re.subn(r"^SHELL_B64 = None.*$", 'SHELL_B64 = "%s"' % b64, src, count=1, flags=re.M)
    if n != 1:
        raise SystemExit("install.py에 SHELL_B64 자리가 없음")
    out = out.encode("ascii", "backslashreplace").decode("ascii")
    compile(out, "install.py", "exec")
    return out


def _files():
    """(zip 안 경로, 원본 경로) — install.py는 렌더본으로 따로 넣는다."""
    items = []
    roots = [ENGINE / "vmc_harness", ROOT / "mod"]
    for base in roots:
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(str(base)):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
            for fn in sorted(filenames):
                if fn in EXCLUDE_FILES or fn.endswith((".pyc", ".pyo")):
                    continue
                p = Path(dirpath) / fn
                items.append((p.relative_to(ROOT).as_posix(), p))
    for extra in ("docs/kernel-writing.md",):  # PRINCIPLES.md는 engine/vmc_harness/ 안에 있어 위에서 함께 묶인다
        if (ROOT / extra).is_file():
            items.append((extra, ROOT / extra))
    return items


def _zinfo(name):
    zi = zipfile.ZipInfo(name, ZIP_DATE)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = (0o644 << 16)
    return zi


GATE_FILE = ROOT / "scripts" / "release-gate.json"


def check_gate(accept_open, gate_file=None):
    """출고 기준 검사(spec 11 §출고 기준). 통과하면 인정한 미확인 항목 [{id, desc}], 아니면 SystemExit.

    2026-10-30 첫 고객 결함 문의의 교훈: 며칠이 걸리는 흐름(장부·판정)을 한 번도 끝까지 돌리지 않고, 고객 환경(Windows)
    미확인을 '현장에서 확인'으로 둔 채 완성품처럼 출고했다. 그래서 (1) 시간 압축 시나리오 테스트가 있어야 하고
    (2) 미확인 항목은 게시하는 사람이 이름으로 인정해야 하며, 인정한 것은 고객에게 시험 기능으로 보인다."""
    gate = json.loads(Path(gate_file or GATE_FILE).read_text(encoding="utf-8"))
    missing = [t for t in gate.get("scenario_tests") or [] if not (ROOT / t).is_file()]
    if missing:
        raise SystemExit("출고 기준: 시나리오 테스트 없음 — %s" % ", ".join(missing))
    accepted = {x.strip() for x in (accept_open or "").split(",") if x.strip()}
    items = gate.get("unverified") or []
    known = {i["id"] for i in items}
    unknown = sorted(accepted - known)
    if unknown:
        raise SystemExit("출고 기준: release-gate.json에 없는 id — %s" % ", ".join(unknown))
    opened = [i for i in items if i.get("status") != "verified"]
    left = [i["id"] for i in opened if i["id"] not in accepted]
    if left:
        raise SystemExit("출고 기준: 고객 환경 미확인 항목 — %s\n시험 기능으로 내보내려면 --accept-open %s"
                         % ("; ".join("%s(%s)" % (i["id"], i["desc"]) for i in opened if i["id"] in left), ",".join(left)))
    return [{"id": i["id"], "desc": i["desc"]} for i in opened]


def build(out_dir, eval_passed=False, unverified=None):
    """릴리스 zip을 만든다. {path, version, sha256, size, manifest}"""
    v = version()
    contents = {}
    for arc, p in _files():
        contents[arc] = p.read_bytes()
    contents["install/install.py"] = render_install().encode("ascii")
    # 고객 PC에서 읽히는 출고 정보 — `vmc-harness update`가 시험 기능을 보여 준다(core/release_info.py)
    contents["engine/vmc_harness/release_info.json"] = (json.dumps(
        {"version": v, "unverified": list(unverified or [])}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    commit, dirty = git_commit()
    manifest = {
        "name": "vmc-harness", "version": v, "kernel_version": v,
        "built_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "git_commit": commit, "git_dirty": dirty, "eval_passed": bool(eval_passed),
        "unverified": list(unverified or []),
        "files": {k: hashlib.sha256(b).hexdigest() for k, b in sorted(contents.items())},
    }
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ("vmc-harness-%s.zip" % v)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(_zinfo("manifest.json"), json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for k in sorted(contents):
            zf.writestr(_zinfo(k), contents[k])
    data = buf.getvalue()
    path.write_bytes(data)
    shell.verify_release_zip(path, v)  # 껍데기가 하는 검증을 그대로 통과하는지
    return {"path": path, "version": v, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "manifest": manifest}


# ---------------------------------------------------------------- R2 (S3 호환, SigV4 직접 계산)

def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4(method, host, path, headers, payload_hash, access_key, secret_key, region, service, amz_date):
    """Authorization 헤더 값. apps/api/lib/harness/r2.ts signV4와 같은 규칙(둘 다 AWS 문서 예제로 테스트)."""
    from urllib.parse import quote
    date = amz_date[:8]
    names = sorted(h.lower() for h in headers)
    lower = {k.lower(): v for k, v in headers.items()}
    canonical_headers = "".join("%s:%s\n" % (n, lower[n].strip()) for n in names)
    signed = ";".join(names)
    canonical = "\n".join([method, quote(path, safe="/-_.~"), "", canonical_headers, signed, payload_hash])
    scope = "%s/%s/%s/aws4_request" % (date, region, service)
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    k = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    k = _hmac(_hmac(_hmac(k, region), service), "aws4_request")
    sig = hmac.new(k, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s" % (access_key, scope, signed, sig), sig


def r2_config():
    env = os.environ
    missing = [k for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY") if not env.get(k)]
    if missing:
        raise SystemExit("R2 환경변수 없음: %s" % ", ".join(missing))
    return {"account": env["R2_ACCOUNT_ID"], "ak": env["R2_ACCESS_KEY_ID"], "sk": env["R2_SECRET_ACCESS_KEY"],
            "bucket": env.get("R2_BUCKET") or "vmc-harness-releases"}


def r2_put(cfg, key, data, content_type):
    host = "%s.r2.cloudflarestorage.com" % cfg["account"]
    path = "/%s/%s" % (cfg["bucket"], key)
    amz = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ph = hashlib.sha256(data).hexdigest()
    headers = {"host": host, "content-type": content_type, "x-amz-content-sha256": ph, "x-amz-date": amz}
    auth, _ = sigv4("PUT", host, path, headers, ph, cfg["ak"], cfg["sk"], "auto", "s3", amz)
    req = urllib.request.Request("https://%s%s" % (host, path), data=data, method="PUT",
                                 headers={"content-type": content_type, "x-amz-content-sha256": ph, "x-amz-date": amz, "Authorization": auth})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            if r.status not in (200, 201):
                raise SystemExit("R2 PUT %s 실패 %s" % (key, r.status))
    except urllib.error.HTTPError as e:
        raise SystemExit("R2 PUT %s 실패 %s %s" % (key, e.code, (e.read() or b"")[:300].decode("utf-8", "replace")))


def record_release(ctx_hub, v, r2_key, sha, size, notes):
    """harness_releases 행 추가 — 파이썬엔 Postgres 드라이버가 없어 context-hub packages/db 스크립트를 부른다."""
    pnpm = shutil.which("pnpm")
    if not pnpm:
        raise SystemExit("pnpm 없음 — context-hub에서 직접: pnpm -C packages/db exec tsx scripts/harness-release.ts add %s %s %s %s" % (v, r2_key, sha, size))
    cmd = [pnpm, "--dir", str(Path(ctx_hub) / "packages" / "db"), "exec", "tsx", "scripts/harness-release.ts", "add", v, r2_key, sha, str(size)]
    if notes:
        cmd += ["--notes", notes]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise SystemExit("harness_releases 기록 실패(R2에는 올라감 — 같은 명령으로 다시 기록하면 된다)")


def require_windows_ci(commit):
    """그 커밋의 수동 CI(Windows 포함)가 통과했어야 게시한다. push CI는 ubuntu만 돈다(.github/workflows/ci.yml)."""
    gh = shutil.which("gh")
    if not gh:
        raise SystemExit("gh 없음 — Windows CI 통과를 확인할 수 없다")
    r = subprocess.run([gh, "run", "list", "-w", "ci", "-e", "workflow_dispatch", "-c", commit, "--json", "status,conclusion,url"],
                       cwd=str(ROOT), capture_output=True, text=True)
    runs = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    if any(x["conclusion"] == "success" for x in runs):
        return
    if any(x["status"] != "completed" for x in runs):
        raise SystemExit("Windows CI 진행 중: %s — 끝나면 다시" % runs[0]["url"])
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(ROOT), capture_output=True, text=True).stdout.strip()
    subprocess.run([gh, "workflow", "run", "ci", "--ref", branch], cwd=str(ROOT))  # 브랜치 끝이 이 커밋이어야 한다(push 후 실행)
    raise SystemExit("이 커밋(%s)의 Windows CI 통과 기록이 없어 지금 돌렸다 — `gh run list -w ci`로 통과 확인 후 다시" % commit[:7])


def publish(a):
    if not a.eval_passed:
        raise SystemExit("회귀 세트(spec 11)를 통과한 버전만 올린다 — 통과했으면 --eval-passed")
    unverified = check_gate(a.accept_open)
    commit, dirty = git_commit()
    if dirty and not a.allow_dirty:
        raise SystemExit("커밋되지 않은 변경이 있음(engine·install·mod·scripts) — 커밋 후 다시, 또는 --allow-dirty")
    if not a.dry_run:
        require_windows_ci(commit)
    b = build(a.out, eval_passed=True, unverified=unverified)
    notes = a.notes
    if unverified:
        notes = (notes + "\n" if notes else "") + "시험 기능(고객 환경 미확인): " + "; ".join(i["desc"] for i in unverified)
    r2_key = R2_RELEASE_PREFIX + b["path"].name
    plan = ["R2 PUT %s (%d B, sha256 %s)" % (r2_key, b["size"], b["sha256"]),
            "harness_releases add %s" % b["version"], "R2 PUT %s" % R2_INSTALL_KEY]
    print("\n".join(plan))
    if a.dry_run:
        print("(dry-run — 업로드·기록 안 함) zip: %s" % b["path"])
        return 0
    cfg = r2_config()
    r2_put(cfg, r2_key, b["path"].read_bytes(), "application/zip")
    record_release(a.context_hub, b["version"], r2_key, b["sha256"], b["size"], notes)
    r2_put(cfg, R2_INSTALL_KEY, render_install().encode("ascii"), "text/x-python; charset=utf-8")
    print("릴리스 %s 게시 완료" % b["version"])
    return 0


def make_bundle(zip_path, info, out_dir):
    """오프라인 번들 zip. info: {license_id, org, expires_at, key_hash}. 경로."""
    zip_path = Path(zip_path)
    man = shell.verify_release_zip(zip_path)
    data = zip_path.read_bytes()
    kh = info.get("key_hash") or ""
    if not re.fullmatch(r"[0-9a-f]{64}", kh):
        raise SystemExit("bundle-info에 key_hash 없음")
    meta = {"format": 1, "version": man["version"], "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "release_file": zip_path.name, "license_id": info.get("license_id"), "org": info.get("org"),
            "expires_at": info.get("expires_at"), "issued_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    raw = (json.dumps(meta, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    slug = re.sub(r"[^0-9A-Za-z]+", "-", str(info.get("license_id") or "license"))[:36]
    out = Path(out_dir) / ("vmc-harness-offline-%s-%s.zip" % (man["version"], slug))
    out.parent.mkdir(parents=True, exist_ok=True)
    readme = ("VMC 하네스 오프라인 설치\n\n"
              "1) python install.py            (윈도우는 python, 맥은 python3)\n"
              "2) 새 터미널에서: vmc-harness activate --offline <라이선스 키> --bundle \"%s\"\n" % out.name)
    with zipfile.ZipFile(str(out), "w") as zf:
        zf.writestr(_zinfo(shell.BUNDLE_JSON), raw)
        zf.writestr(_zinfo(shell.BUNDLE_SIG), shell.sign_bundle(raw, kh) + "\n")
        zf.writestr(_zinfo(zip_path.name), data)
        zf.writestr(_zinfo("install.py"), render_install())
        zf.writestr(_zinfo("README.txt"), readme)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(prog="release.py", description="VMC 하네스 릴리스")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", default=str(ROOT / "dist"))
    r = sub.add_parser("render-install")
    r.add_argument("--out")
    pb = sub.add_parser("publish")
    pb.add_argument("--eval-passed", action="store_true")
    pb.add_argument("--notes", default="")
    pb.add_argument("--accept-open", default="", help="release-gate.json의 미확인 항목 id(쉼표) — 시험 기능으로 내보냄을 인정")
    pb.add_argument("--dry-run", action="store_true")
    pb.add_argument("--allow-dirty", action="store_true")
    pb.add_argument("--out", default=str(ROOT / "dist"))
    pb.add_argument("--context-hub", default=os.environ.get("VMC_CONTEXT_HUB") or str(ROOT.parent / "context-hub"))
    bd = sub.add_parser("bundle")
    bd.add_argument("--zip", required=True)
    bd.add_argument("--license-info", default="-", help="bundle-info JSON 파일(기본 표준 입력)")
    bd.add_argument("--out", default=str(ROOT / "dist"))
    a = p.parse_args(argv)
    if a.cmd == "build":
        res = build(a.out)
        print("%s\nversion %s\nsha256 %s\nsize %d" % (res["path"], res["version"], res["sha256"], res["size"]))
    elif a.cmd == "render-install":
        text = render_install()
        if a.out:
            Path(a.out).write_text(text, encoding="ascii")
        else:
            sys.stdout.write(text)
    elif a.cmd == "publish":
        return publish(a)
    elif a.cmd == "bundle":
        raw = sys.stdin.read() if a.license_info == "-" else Path(a.license_info).read_text(encoding="utf-8")
        print(make_bundle(a.zip, json.loads(raw), a.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
