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


def check(ctx):
    root = ctx.root
    hits = []
    for o in ctx.state.get("file_ops") or []:
        for label, d in guarded_dirs(root):
            if globs.is_under(o["abs"], d):
                hits.append((label, o.get("rel") or o["abs"]))
    if not hits and ctx.command and ctx.state.get("shell_opaque"):
        expanded = os.path.expandvars(os.path.expanduser(ctx.command))
        markers = _TEXT_MARKERS if ctx.state.get("opaque_watch") is not True else [_KERNEL_MARKER]
        for rx in markers:
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
