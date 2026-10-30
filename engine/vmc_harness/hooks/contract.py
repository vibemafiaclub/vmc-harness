"""훅 처리 계약 — 디스패처(hooks/dispatcher.py)와 각 영역 핸들러가 공유한다.

각 영역은 `<영역>/handlers.py`에 아래 이름의 함수를 필요한 것만 정의한다:
    on_session_start(ctx) / on_user_prompt(ctx) / on_pre_tool(ctx) / on_post_tool(ctx) / on_stop(ctx) / on_session_end(ctx)
각 함수는 None 또는 HookResult를 돌려준다. 디스패처는 HANDLER_MODULES 순서로 부르고,
block이 나오면 그 자리에서 멈춘다(안전 게이트가 앞). 주입 문구는 core.inject.message()로만 만든다.

핸들러 안의 예외: 안전 게이트(safety=True로 표시된 모듈)는 막는다(fail-closed),
그 외는 통과(fail-open) — 디스패처가 처리한다(spec 01 P8).
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd")
HANDLER_NAMES = {
    "SessionStart": "on_session_start",
    "UserPromptSubmit": "on_user_prompt",
    "PreToolUse": "on_pre_tool",
    "PostToolUse": "on_post_tool",
    "Stop": "on_stop",
    "SessionEnd": "on_session_end",
}

# (모듈 경로, safety) — 순서 = 호출 순서. 안전 게이트가 먼저.
HANDLER_MODULES = [
    ("vmc_harness.gates.handlers", True),      # 엔진 쓰기 보호·DB 직접 접속 차단·보호 영역·검증물 변조·완료 게이트
    ("vmc_harness.db.handlers", True),         # DB 안전장치(패치 폴더 밖 SQL 쓰기 등)
    ("vmc_harness.gates.generated", False),    # L1 생성 게이트 로더
    ("vmc_harness.task.handlers", False),      # 착수 선언 확인·PM·과설계·요청 밖 변경·실측
    ("vmc_harness.ledger.handlers", False),    # 세션 시작 판정(judge)·노출 기록·자가 점검
    ("vmc_harness.brain.handlers", False),     # 브레인 pull
    ("vmc_harness.loop.handlers", False),      # 활동 시각·복귀 양보·아침 머지
    ("vmc_harness.render.handlers", False),    # 브리핑 캐시 갱신
    ("vmc_harness.bootstrap.handlers", False), # 주간 재진단(백그라운드)
    ("vmc_harness.dist.handlers", False),      # 자동 업데이트(세션 시작)
]


@dataclass
class HookContext:
    event: str                      # EVENTS 중 하나
    agent: str                      # claude | codex
    raw: Dict[str, Any]             # 에이전트가 준 원본 입력 JSON
    root: Optional[str] = None      # 프로젝트 루트(.harness/가 있는 폴더). 없으면 하네스 미설치 프로젝트
    session_id: Optional[str] = None
    transcript_path: Optional[str] = None
    cwd: Optional[str] = None
    tool_name: Optional[str] = None         # 정규화: Bash | Edit | Write | MultiEdit | Read | apply_patch | ...
    tool_input: Dict[str, Any] = field(default_factory=dict)
    command: Optional[str] = None           # 셸 명령이면 그 문자열
    write_paths: List[str] = field(default_factory=list)   # 이 도구 호출이 쓰는 파일(프로젝트 루트 상대 경로, / 구분)
    prompt: Optional[str] = None            # UserPromptSubmit 사람 입력
    task_id: Optional[str] = None           # 현재 세션의 작업 id(착수 선언 뒤)
    state: Dict[str, Any] = field(default_factory=dict)    # 같은 호출 안에서 핸들러끼리 넘기는 값


@dataclass
class HookResult:
    block: bool = False
    reason: Optional[str] = None            # 막을 때 사유(무엇을 어겼는지만)
    inject: List[str] = field(default_factory=list)  # core.inject.message()로 만든 문구만
    notice: Optional[str] = None            # 사람에게 보일 한 줄(에이전트 컨텍스트에 넣지 않음)
