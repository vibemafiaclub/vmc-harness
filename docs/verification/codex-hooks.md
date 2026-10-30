# 훅 실측 — Codex 0.146.0 · Claude Code 2.1.301 (2026-10-22, macOS)

spec 12 미검증 #1·#2·#3의 확인 결과. 임시 디렉터리 프로젝트 + 임시 `CODEX_HOME`(실제 `~/.codex/config.toml`은 건드리지 않음),
Claude는 `claude -p --setting-sources project`(사용자 설정을 읽지도 바꾸지도 않음)로 쟀다. 탐침 훅은 stdin을 그대로 적어 두는 Python 스크립트.

## 결과 표

| 항목 | Codex 0.146.0 (`codex exec`) | Claude Code 2.1.301 (`claude -p`) |
|---|---|---|
| 훅 기능 켜기 | 따로 안 켬. `codex features list` → `hooks stable true` | 해당 없음 |
| **신뢰(trust) 등록 필요** | **필요.** 신뢰 기록 없는 프로젝트 훅은 경고 없이 건너뛴다(막아야 할 명령이 그대로 실행됨 — 실측). `--dangerously-bypass-hook-trust`를 주면 실행됨(0.133 때와 달라짐) | 없음(프로젝트 `.claude/settings.json`을 그대로 읽음) |
| 신뢰를 프로그램으로 등록 | **가능.** `codex app-server`(stdio JSON-RPC) `hooks/list {cwds:[root]}` → 훅마다 `key`·`currentHash` → `config/value/write {keyPath: 'hooks.state."<key>".trusted_hash', value: currentHash, mergeStrategy: upsert}`. 등록 뒤 `--dangerously-…` 없이 `codex exec`에서 훅 실행·차단 확인. 지우기는 같은 키에 `value: null, replace` | 해당 없음 |
| 프로젝트 신뢰 | 프로젝트 자체가 `projects."<경로>".trust_level="trusted"`가 아니면 프로젝트 훅이 목록에 안 나옴 — 같은 API로 등록 | 해당 없음 |
| 신뢰 키 형식 | `<hooks.json 절대 경로(심볼릭 링크 푼 것)>:<event_snake>:<그룹 순번>:<훅 순번>` — PC·경로마다 다르고 **순번 기반**이라 앞쪽 그룹을 지우면 뒤 훅의 신뢰가 풀린다 | — |
| PreToolUse deny (JSON) | **막힘.** `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"…"}}` → `Command blocked by PreToolUse hook: <사유>` | **막힘.** 같은 JSON → `PreToolUse:Write hook error: <사유>` |
| PreToolUse deny (exit 2 + stderr) | 막힘 | 막힘 |
| 막힌 도구 | 셸(`tool_name: "Bash"`, `tool_input.command` 문자열), 파일 편집(`tool_name: "apply_patch"`, `tool_input.command` = 패치 본문 `*** Add/Update/Delete File:`) | Write·Edit(`file_path`), Bash(`command`) |
| Stop block | **됨.** `{"decision":"block","reason":"…"}` → 모델이 이어서 답함, 두 번째 Stop에 `stop_hook_active: true` | **됨.** 같은 형식·같은 `stop_hook_active` |
| UserPromptSubmit | **있음.** 필드 `prompt`, `turn_id` | 있음. `prompt`, `prompt_id` |
| SessionEnd | **있음.** 필드 `reason`(exec 끝 = `other`) | 있음. `reason` |
| additionalContext 주입 | SessionStart·UserPromptSubmit·PreToolUse·PostToolUse 모두 모델 컨텍스트에 들어감 | 같음(넷 다 확인) |
| `systemMessage` | exec 출력에 안 보임(TUI는 미검증) | 사람에게만 보이고 모델 컨텍스트엔 안 들어감 |
| 공통 입력 필드 | session_id, transcript_path(`~/.codex/sessions/…/rollout-…jsonl`), cwd, hook_event_name, model, permission_mode, turn_id | session_id, transcript_path, cwd, hook_event_name, permission_mode, prompt_id, effort |
| 훅에서 분리 실행한 자식 프로세스 | **살아남음**(`start_new_session=True`로 띄운 45초 sleep 자식이 codex 종료 뒤 파일을 씀) | 살아남음(같은 방법) |
| 훅 프로세스의 샌드박스 | 훅은 샌드박스 밖(훅이 `~/…` 아래에 씀) | 해당 없음 |
| 에이전트 셸 안 세션 id | `CODEX_THREAD_ID`(= 훅 session_id). Claude 안에서 띄운 Codex는 바깥 `CLAUDE_CODE_SESSION_ID`도 물려받음 | `CLAUDE_CODE_SESSION_ID` |
| 에이전트 셸 샌드박스(workspace-write 기본) | 작업 폴더·/tmp 밖 쓰기 거부 → 에이전트가 부른 `vmc-harness` 명령은 `~/.vmc-harness`에 못 씀. **네트워크 차단**(`CODEX_SANDBOX_NETWORK_DISABLED=1`) → `evidence api_call`이 localhost도 `Operation not permitted` | 해당 없음 |

## 하네스 실물로 끝까지(E2E)

`hooks/install.register()`로 임시 프로젝트에 훅을 깔고 실제 에이전트로 돌렸다.

- Claude: `.harness/ledger/x.json` Write·셸 리다이렉트 둘 다 `[vmc-harness:gate_violation] 엔진 쓰기 보호…`로 막힘. 착수 선언 → 페이지 파일 수정 → Stop에서 `[vmc-harness:missing_evidence] 빠진 증거 — 바뀐 동작(페이지: fe/pages/a.tsx)…`로 막힘 → 모델이 `vmc-harness evidence test_run`으로 증거를 남기고 종료 통과(10턴, 32초)
- Codex(신뢰는 `install.trust_codex()`가 등록, `--dangerously-…` 없이): 장부 쓰기 막힘. 모델이 `vmc-harness proceed`로 스스로 풀려 했으나 "사람이 터미널에서 직접 실행한 경우만 인정"으로 거부됨. 페이지 수정 뒤 Stop이 두 번 막히고 모델이 증거를 남긴 뒤 통과

## 어댑터·설치에 반영한 것

- Codex도 Claude와 같은 JSON 출력 형식을 쓴다(`hooks/adapters/common.output`). `apply_patch` 본문에서 쓰는 파일을 뽑는다
- Codex 훅도 6개 이벤트 전부 등록(UserPromptSubmit·SessionEnd 있음 → spec 12 #2의 대체안 불필요)
- `install.register()`가 Codex 신뢰·프로젝트 신뢰를 app-server로 등록한다. 팀원 PC처럼 이 PC 기록이 없으면 SessionStart 훅이 백그라운드로 한 번 등록(`vmc-harness gate codex-trust`)
- 에이전트 셸에서 쓰는 세션 id는 `CODEX_THREAD_ID` → `CLAUDE_CODE_SESSION_ID` 순으로 본다
- 훅 안 비동기 백그라운드 호출(PM·카드 검색)은 Codex에서도 된다 → spec 12 #3 "Codex는 중·대 작업에서만 동기" 대체안 불필요

## 남은 미검증

- Codex 대화형 TUI에서 같은 동작(특히 `systemMessage` 표시), Windows에서 `codex.cmd app-server` 실행
- Codex 샌드박스에서 API 증거: 네트워크를 열지(`sandbox_workspace_write.network_access`) 사람 결정 필요 — 지금은 스크린샷·테스트 실행 증거만 가능
