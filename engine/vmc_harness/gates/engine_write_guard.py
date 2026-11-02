"""엔진 쓰기 보호 (spec 02 — 안전, fail-closed).

L0 `~/.vmc-harness/kernel/`·L2 `.harness/ledger/`·증거 폴더 `.harness/cache/evidence/`에 대한 Edit·Write·apply_patch·셸 쓰기를 막는다.
기록은 `vmc-harness` 명령으로만 한다(명령 자체는 막지 않는다 — 훅이 아니라 엔진 프로세스가 쓴다).
셸 명령은 쓰기 대상을 판정해 미리 막는다. 판정할 수 없는 명령(인라인 스크립트 등)은 미리 막지 않고
실행 뒤 장부·증거 상태를 대조해 되돌린다(gates.opaque_watch). 예외:
- 엔진(L0, 레포 밖·git 밖이라 대조·복원을 하지 않는다)은 경로가 명령에 보이면 미리 막는다
- 실행 전 기록을 못 했으면(ctx.state["opaque_watch"]가 True가 아님) 옛 방식 — 장부·증거 경로가 명령에 보이면 미리 막는다
"""

import os
import re

from vmc_harness.core import paths
from vmc_harness.gates import globs
from vmc_harness.gates.common import block

NAME = "engine_write_guard"


def guarded_dirs(root):
    out = [("엔진(L0)", paths.kernel_root())]
    if root:
        out.append(("장부(L2)", paths.ledger_dir(root)))
        out.append(("증거", paths.cache_dir(root) / "evidence"))
    return out


_KERNEL_MARKER = re.compile(r"\.vmc-harness[\\/]+kernel", re.I)
_TEXT_MARKERS = [re.compile(r"\.harness[\\/]+ledger", re.I), _KERNEL_MARKER,
                 re.compile(r"\.harness[\\/]+cache[\\/]+evidence", re.I)]


def mentions_kernel(command):
    """명령 문자열에 엔진(L0) 경로가 보이는가 — 실행 뒤 엔진 파일 대조를 할지."""
    if not command:
        return False
    expanded = os.path.expandvars(os.path.expanduser(command))
    home_k = str(paths.kernel_root())
    return bool(_KERNEL_MARKER.search(expanded) or (home_k and home_k in expanded))


def kernel_changed():
    """지금 쓰는 커널 폴더 파일 중 manifest sha256과 다른 것(상대 경로 목록). 확인할 수 없으면 []."""
    import hashlib
    import json
    from vmc_harness.dist import shell as dist_shell
    d = dist_shell.current_dir()
    if not d:
        return []
    try:
        files = (json.loads((d / dist_shell.MANIFEST).read_text(encoding="utf-8")).get("files") or {})
    except (OSError, ValueError):
        return []
    bad = []
    for rel, want in sorted(files.items()):
        try:
            got = hashlib.sha256((d / rel).read_bytes()).hexdigest()
        except OSError:
            got = None
        if got != want:
            bad.append(rel)
    return bad


def check(ctx):
    root = ctx.root
    hits = []
    for o in ctx.state.get("file_ops") or []:
        for label, d in guarded_dirs(root):
            if globs.is_under(o["abs"], d):
                hits.append((label, o.get("rel") or o["abs"]))
    # 해석 못 한 셸 명령: 실행 뒤 대조(opaque_watch)가 잡혔으면 문자열로 미리 막지 않는다 — 엔진(L0)도 실행 뒤
    # manifest 해시로 대조한다(엔진 폴더에서 복사·읽기만 하는 명령이 막히던 현장 결함 F3). 대조를 못 잡았을 때만 옛 방식.
    if not hits and ctx.command and ctx.state.get("shell_opaque") and ctx.state.get("opaque_watch") is not True:
        expanded = os.path.expandvars(os.path.expanduser(ctx.command))
        for rx in _TEXT_MARKERS:
            if rx.search(expanded):
                hits.append(("하네스 기록 영역", rx.pattern.replace("[\\\\/]+", "/").replace("\\", "")))
                break
        home_k = str(paths.kernel_root())
        if not hits and home_k and home_k in expanded:
            hits.append(("엔진(L0)", home_k))
    if not hits:
        return None
    label, where = hits[0]
    return block("엔진 쓰기 보호: %s 경로 직접 쓰기 차단 (%s)" % (label, where),
                 "엔진 쓰기 보호: %s 경로 직접 쓰기 차단" % label)
