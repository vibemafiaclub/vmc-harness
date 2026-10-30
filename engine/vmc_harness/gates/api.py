"""생성 게이트(L1 `.harness/gates/<id>.py`)가 쓸 수 있는 엔진 API — 이것만 쓴다(spec 02).

게이트 본체 형식:
    def check(api):
        # 위반이면 무엇을 어겼는지 한 문장(str)을 돌려준다. 통과면 None.
        for rel in api.paths:
            if "TODO" in (api.proposed(rel) or ""):
                return "%s에 TODO 남김" % rel

본체가 import할 수 있는 것: ALLOWED_IMPORTS. 파일 쓰기·프로세스 실행·네트워크는 없다(읽기만).
백테스트(부트스트랩)는 GateAPI를 합성 입력으로 만들어 지난 커밋에 돌린다.
"""

from pathlib import Path

ALLOWED_IMPORTS = ("re", "json", "fnmatch", "math", "itertools", "collections", "functools", "string", "datetime",
                   "vmc_harness.gates.api")
FORBIDDEN_NAMES = ("open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "input",
                   "breakpoint", "getattr", "setattr", "delattr", "memoryview")


class GateAPI(object):
    def __init__(self, root, event, paths=None, all_paths=None, tool_name=None, command=None, proposed=None,
                 change_kinds=None, size=None, facts=None):
        self.root = str(root)
        self.event = event                    # PreToolUse | PostToolUse | Stop | ...
        self.paths = list(paths or [])        # 발동 조건 glob에 걸린 파일(루트 상대)
        self.all_paths = list(all_paths or self.paths)
        self.tool_name = tool_name
        self.command = command
        self.change_kinds = dict(change_kinds or {})   # rel -> add|modify|delete
        self.size = size                      # 작업 크기 small|mid
        self._proposed = dict(proposed or {})  # rel -> 편집 뒤 내용(PreToolUse에서 미리 계산)
        self._facts = facts

    def read(self, rel):
        """지금 디스크 내용(없으면 None)."""
        p = Path(self.root) / rel
        try:
            return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
        except OSError:
            return None

    def proposed(self, rel):
        """이 도구 호출이 끝난 뒤 내용 — 알 수 없으면 지금 내용."""
        if rel in self._proposed:
            return self._proposed[rel]
        return self.read(rel)

    def head(self, rel):
        """HEAD 커밋의 내용(없으면 None)."""
        from vmc_harness.core.gitutil import git
        rc, out, _ = git(self.root, "show", "HEAD:%s" % rel)
        return out if rc == 0 else None

    def exists(self, rel):
        return (Path(self.root) / rel).exists()

    def facts(self):
        """부트스트랩 기계 사실(읽기 전용 사본)."""
        if self._facts is None:
            try:
                from vmc_harness.bootstrap import facts
                self._facts = facts.load(self.root) or {}
            except Exception:
                self._facts = {}
        return dict(self._facts)

    def match(self, rel, glob):
        from vmc_harness.gates import globs
        return globs.match(rel, glob)
