"""기존 관례 추출 (spec 07 기계 사실 — 결정적).

반복되는 구조를 찾는다. 예: 도메인마다 `XRepository(Protocol)` + `InMemoryXRepository` + `MssqlXRepository` 3종 세트,
같은 부모 아래 모듈들이 같은 파일 조합(models·repository·service)을 갖는 것, 테스트 폴더가 도메인 이름을 따라가는 것.
같은 구조가 `bootstrap.convention_min_modules` 이상 모듈에서 반복될 때만 관례로 본다.
"""

import ast
import re
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

from vmc_harness.bootstrap import util
from vmc_harness.core import config

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
SHARE = 0.6  # 부모 아래 모듈의 이 비율 이상이 가진 파일 = 핵심 파일 조합 (비율 정의 — 설정 대상 아님)


def _classes(text):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = []
            for b in node.bases:
                if isinstance(b, ast.Name):
                    bases.append(b.id)
                elif isinstance(b, ast.Attribute):
                    bases.append(b.attr)
                elif isinstance(b, ast.Subscript):
                    v = b.value
                    bases.append(v.id if isinstance(v, ast.Name) else getattr(v, "attr", ""))
            out.append((node.name, bases))
    return out


def _class_variants(root, module):
    """[(시그니처, 예시)] — 시그니처 = (기반 종류, 변형 접두 튜플, 역할 토큰)"""
    classes = []
    for f in module["file_list"]:
        if not f.endswith(".py"):
            continue
        classes.extend(_classes(util.read_text(Path(root) / f)))
    names = {n: b for n, b in classes}
    sigs = []
    for base, bases in classes:
        prefixes = []
        for other in names:
            if other != base and other.endswith(base) and other[: -len(base)][:1].isupper():
                prefixes.append(other[: -len(base)])
        if len(prefixes) < 1:
            continue
        kind = "Protocol" if "Protocol" in bases else ("ABC" if "ABC" in bases else "class")
        toks = _CAMEL.findall(base)
        role = toks[-1] if toks else base
        sig = (kind, tuple(sorted(set(prefixes))), role)
        sigs.append((sig, base))
    return sigs


def detect(root, map_data):
    """관례 목록. 각 항목에 읽은 방법(command)을 함께 둔다."""
    min_n = config.get("bootstrap.convention_min_modules", root)
    modules = map_data.get("modules", [])
    out = []

    # 1) 클래스 변형 세트 (Protocol + 구현 여러 벌)
    sig_mods = defaultdict(list)
    for m in modules:
        if "python" not in m.get("lang", []):
            continue
        seen = set()
        for sig, base in _class_variants(root, m):
            if sig in seen:
                continue
            seen.add(sig)
            sig_mods[sig].append((m["id"], base))
    for sig, mods in sorted(sig_mods.items(), key=lambda kv: -len(kv[1])):
        if len(mods) < min_n:
            continue
        kind, prefixes, role = sig
        base_label = "X%s%s" % (role, "(%s)" % kind if kind != "class" else "")
        parts = [base_label] + ["%sX%s" % (p, role) for p in prefixes]
        out.append({
            "id": "class-set:%s:%s:%s" % (kind, "+".join(prefixes), role),
            "kind": "class_variant_set",
            "text": "모듈마다 %s %d종 세트 — %d개 모듈" % (" + ".join(parts), len(parts), len(mods)),
            "count": len(mods),
            "modules": [m for m, _ in mods],
            "examples": [b for _, b in mods[:3]],
            "command": "vmc-harness map → AST(최상위 class 이름·부모 클래스) 비교",
        })

    # 2) 같은 부모 아래 모듈의 핵심 파일 조합
    by_parent = defaultdict(list)
    for m in modules:
        parent = PurePosixPath(m["id"]).parent.as_posix()
        by_parent[parent].append(m)
    for parent, kids in sorted(by_parent.items()):
        if len(kids) < min_n:
            continue
        cnt = Counter()
        for k in kids:
            for f in {PurePosixPath(x).name for x in k["file_list"]}:
                if f not in ("__init__.py", "index.ts", "index.tsx", "index.js"):
                    cnt[f] += 1
        core = sorted(f for f, c in cnt.items() if c >= max(min_n, SHARE * len(kids)))
        if len(core) < 2:
            continue
        having = [k["id"] for k in kids if set(core) <= {PurePosixPath(x).name for x in k["file_list"]}]
        if len(having) < min_n:
            continue
        out.append({
            "id": "file-set:%s" % parent,
            "kind": "file_set",
            "text": "`%s/*` 아래 모듈 %d개 중 %d개가 같은 파일 조합(%s)" % (parent, len(kids), len(having), "·".join(core)),
            "count": len(having),
            "modules": having,
            "files": core,
            "command": "vmc-harness map → 모듈별 파일 이름 집계",
        })

    # 3) 테스트 폴더가 도메인 이름을 따라간다
    test_names, src_names = defaultdict(list), defaultdict(list)
    for m in modules:
        parts = PurePosixPath(m["id"]).parts
        name = parts[-1] if parts else m["id"]
        if any(p in ("tests", "test", "__tests__", "spec") for p in parts[:-1]):
            test_names[name].append(m["id"])
        elif name not in ("tests", "test", "__tests__", "spec"):
            src_names[name].append(m["id"])
    mirrored = sorted(n for n in test_names if n in src_names)
    if len(mirrored) >= min_n:
        out.append({
            "id": "test-mirror",
            "kind": "test_mirror",
            "text": "테스트 폴더가 도메인 폴더 이름을 그대로 따른다 — %d개 도메인" % len(mirrored),
            "count": len(mirrored),
            "modules": mirrored,
            "command": "vmc-harness map → tests/ 아래 폴더 이름과 소스 폴더 이름 대조",
        })
    return out
