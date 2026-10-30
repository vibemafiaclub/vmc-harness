# 영역 간 인터페이스 (병렬 구현 계약)

다른 영역이 부르는 함수는 **이 이름·시그니처로** 만든다. 바꿔야 하면 이 문서를 고치고 최종 보고에 적는다.
다른 영역 함수가 아직 없을 수 있다 — import는 함수 안에서 하고, 없으면(ImportError/AttributeError) 조용히 건너뛰는 방식으로 짠다(편의 경로). 안전 게이트가 의존하는 것은 예외(없으면 막는다).

## 훅 (gates 소유)
- 계약: `hooks/contract.py` (HookContext, HookResult, HANDLER_MODULES). 각 영역은 `<영역>/handlers.py`에 `on_session_start(ctx)` 등 필요한 것만
- `vmc_harness.hooks.install.register(root, agents=("claude","codex"))` / `unregister(root)` — 에이전트 설정 파일에 훅 등록·해제(init·remove가 부른다)
- 훅 명령 문자열: `vmc-harness hook <Event> --agent <claude|codex>`

## 증거 (gates 소유)
- `vmc_harness.gates.evidence.make(root, task_id, kind, target, src_path=None, data=None) -> dict(marker)` — `.harness/cache/evidence/<task_id>/`에 산출물 + 마커(task_id, created_at, kind, target)
- kind: `screenshot` | `api_call` | `db_dryrun` | `test_run` | `qa_scenario`
- `vmc_harness.gates.evidence.list_markers(root, task_id) -> list[dict]`

## 작업 상태 (task 소유)
- `vmc_harness.task.state.current(root, session_id) -> dict | None` — 지금 세션의 작업: `{"task_id","session_id","declare":{...09 스키마},"size":"small|mid","started_at","first_edit_at","last_edit_at"}`
- `vmc_harness.task.state.by_id(root, task_id) -> dict | None`
- 세션 id 해석은 `vmc_harness.task.state.resolve_session_id(root, explicit=None)` / 환경변수만 `env_session_id()` 하나로. 순서 `VMC_HARNESS_SESSION_ID` → `CODEX_THREAD_ID` → `CODEX_SESSION_ID` → `CLAUDE_CODE_SESSION_ID` → `CLAUDE_SESSION_ID`(Claude 안에서 띄운 Codex가 바깥 Claude 값을 물려받는 실측 사례 때문에 Codex가 먼저). `gates.state.env_session_id()`는 이 함수를 부른다
- 완료 보고 아래 사람에게만 보일 줄: `vmc_harness.task.report.notice_lines(root, rec, agent=None) -> list[str]` — render `inline_notices`(작업 모듈·폴더별)·`daily_two_lines`를 모은다. Stop 훅이 막지 않고 끝날 때 `systemMessage`로(보인 것으로 표시되므로 실제로 보일 때만 부른다)
- 착수 선언 필드 중 다른 영역이 보는 것: `declare.scope`(경로·모듈 목록), `declare.protected_touch`(bool), `declare.test_change`({"files":[...], "reason":"..."} | null)

## 세션 파서 (task 소유 — session-export·Argos 이식)
- `vmc_harness.sessions.find(project_root, agent=None, limit=None) -> list[(agent, path)]` — 이 PC의 Claude(`~/.claude/projects`)·Codex(`~/.codex/sessions`) 세션 중 이 프로젝트 것
- `vmc_harness.sessions.parse(path, agent) -> Session` (dataclass): `session_id, agent, cwd, started_at, ended_at, human_prompts:[{"at","text"}], tool_calls:[{"at","name","input","paths":[...]}], tokens:int, agent_sec:float, human_turns:int, measured:bool`
  - 형식이 바뀌어 못 읽으면 `measured=False`(틀린 숫자 금지)

## 코어 공용 (core 소유)
- 작성자 이름 기준 하나: `vmc_harness.core.project.author_name(root=None) -> str` — 프로젝트 `git config user.name`(없으면 전역 → OS 사용자). `render.profile.git_user`·`brain.store.current_author`가 이것을 부른다
- 마스킹: `vmc_harness.core.mask.mask(text)` / `mask_obj(json값)`(비밀 키 이름의 값은 통째로) — 다른 곳에 마스킹 정규식을 두지 않는다
- 가벼움 예산(01 P8): `vmc_harness.core.budget.enabled(root, device) -> bool` — device ∈ `pm`·`overdesign_llm`·`card_search`(착수 묶음)·`render_rewrite`. 꺼졌으면 그 편의 장치를 부르지 않는다. `check(root)`는 세션 시작 때 하루 1회(task 핸들러) — 최근 `budget.window_tasks`건 중위값이 `budget.small_task_overhead_s`·`budget.gate_time_per_task_s`를 넘으면 비싼 것부터 하나 끄고 events(`convenience_disabled`). 커널 버전이 바뀌면 다시 켠다
- 로컬 원본 보관(09): `vmc_harness.core.retention.prune(root)` — 세션 시작 때 하루 1회, `retention.local_days`보다 오래된 events 행·quotes·`.harness/cache/evidence/<task>/`
- 이벤트 `gate_time`(디스패처 행 `gate_id="dispatcher:<Event>"`)은 `sec`(디스패처 자신)·`total_sec`(생성 게이트 포함 훅 전체)·`task_id`. 작업 기록 `harness_overhead.gate_sec` = 그 작업 세션의 `total_sec` 합

## 장부·판정 (core·ledger 소유)
- 기록 함수: `core/ledger.py` (append_task, create_finding, create_candidate, create_action, write_verdict, create_approval, answer_approval …)
- 노출 기록: `vmc_harness.ledger.exposure.record(root, action_id=None, gate_id=None, card=None, area=None)` — 게이트 발동·카드 반환·클렌징 영역 작업
- 판정: `vmc_harness.ledger.judge.run(root) -> list[verdict]`
- 원복: `vmc_harness.ledger.revert.revert_action(root, action_id, reason, by_human=True, make_finding=True)` — 장부(`.harness/ledger/`)는 되돌리지 않는다. 하네스 revert 커밋엔 `Harness-Reverted-By: harness`
  - 노출 기록은 추가 키워드 `rule_id=None, session_id=None`도 받는다. 조치 `target` 규약: `{"gate_id"}`·`{"card"}`·`{"area": 경로 접두}`·`{"rule_id"}`
  - 시험 진행(쓰기 없음): `vmc_harness.ledger.judge.progress(root) -> list` / 팀 합산·참고 지표: `vmc_harness.ledger.metrics.team(root, since=None)`
  - 집계가 읽는 이벤트 필드: `gate_fire`·`gate_block`·`proceed_anyway`·`gate_time(sec)`에 `gate_id`, 카드 반환 수는 `exposure`의 `card`
  - 낡은 문서(에이전트 참고 목록에서 뺄 것): `vmc_harness.ledger.stale_docs.stale_paths(root) -> set` / `is_stale(root, path)`
  - ledger가 기대하는 gates 함수: `gates.generate.from_request(root, quote, source, promoted_from=None)`(명시 요청 "막아"·카드 승격), `gates.rules.add_explicit(root, quote, source)`·`render(root)`(명시 요청 규칙)

## 브레인 (ledger·brain 소유)
- `vmc_harness.brain.store.add_card(title, situation, judgment, signals, not_when, quote, author, source, origin) -> path`
- `vmc_harness.brain.search.search(query, top_k=None, use_llm=True, root=None, record=True, session_id=None) -> list[card dict]` — 반환 카드 = 노출 기록(root 없으면 cwd의 프로젝트)
- 브리핑 "배운 판단": `vmc_harness.brain.store.learned(since=None, viewer=None)` / `view(card, viewer)` — 작성자는 viewer 본인일 때만
- `vmc_harness.brain.install.register_mcp(root)` / `unregister_mcp(root)`

## DB (db 소유)
- `vmc_harness.db.client.query(root, sql) -> {"columns":[...], "rows":[...], "truncated":bool}` (SELECT만)
- `vmc_harness.db.client.dryrun(root, sql_path) -> {"affected_rows":int, "statements":int}`
- `vmc_harness.db.sqlguard.is_select_only(sql) -> (bool, reason)`
- `vmc_harness.db.sqlguard.is_direct_db_command(command, cwd=None) -> bool` (sqlcmd·pyodbc·Invoke-Sqlcmd·연결 문자열). `cwd`를 주면 `python <파일.py>`의 스크립트 안 직접 접속도 본다 — 훅에서는 `ctx.cwd or ctx.root`를 넘긴다. 사유 한 줄은 `direct_db_reason(command, cwd=None) -> str | None`
- `vmc_harness.db.client.patch_dir(root) -> str | None` — SQL 패치 폴더(`.harness/config.json`의 `db.patch_dir`가 우선, 부트스트랩이 기존 관례로 덮어쓴다). DB를 쓰는 프로젝트가 아니면 None
- `vmc_harness.db.client.prepare_apply_approval(root, task_id, sql_path) -> approval` — 운영 적용 승인 기록. 롤백 SQL·지금 파일 내용의 성공 드라이런이 없으면 `MetaGateError`
- `vmc_harness.db.facts.load(root) -> {"db_kind","read_at","facts":[{"key","value","command","read_at"}]}` — DB 기계 사실(호환성 수준 등, 읽은 명령 포함). 부트스트랩 facts가 가져간다
- 키체인: `core/secrets.py` (set_secret/get_secret/delete_secret/read_secret_from_terminal, `backend()` → keychain|wincred|file)

## 구조 지도 (bootstrap 소유)
- `vmc_harness.bootstrap.structmap.build(root) -> dict` (`.harness/cache/map.json`·`map.md` 갱신)
- `vmc_harness.bootstrap.structmap.module_of(root, rel_path) -> str | None`
- `vmc_harness.bootstrap.facts.load(root) -> dict` (기계 사실: 빌드·테스트 명령, 테스트 기준선, DB 종류, 관례 목록 `conventions`)
  - 키: `commands{test|build|lint|typecheck: {"command","dir","all"}}`·`test_command` 등(루트에서 그대로 돌릴 수 있는 문자열), `command_list[...]`,
    `python`(가상환경), `test_baseline{"failures":[테스트 id·수집 오류 경로 — 실행 폴더 기준과 루트 기준 둘 다], "runs":[...]}`,
    `db`(DB가 없으면 None)·`db_kind`, `sql_patch_dir{"suggested",...}`, `behavior_globs{page,api,db}`, `environment`, `conventions`,
    `protected_candidates[{glob,category,reason,...}]`, `agents_md`, `_commands{키: 읽은 방법}`
- `vmc_harness.bootstrap.structmap.diff(old, new)` / `task_bundles(root, since=None)` — 지도 diff·`Harness-Task` 묶음("기능 X 추가(날짜): 모듈 N개 생김, 의존 a→b")
- `vmc_harness.bootstrap.backtest.run(root, condition, sessions) -> {"would_block","commits","sessions","problem_changes","scenes","elapsed_sec"}` (조건 type: path_write·write_outside·command·db_direct·test_modify·diff_regex)
- 기계 사실 규칙(rules.json, gates 형식): `id: mf-*`, `basis.source: "bootstrap"`, `basis.command: ["vmc-harness","init","--print-fact",<id>]` — gates.rules.refresh_machine_facts가 다시 실행
- init이 config.json에 쓰는 것: `source_roots`, `db.patch_dir`(+`patch_dir_basis` machine_fact — 사람이 정한 값은 덮지 않음), `created_files`

## 렌더링·브리핑 (render 소유)
- `vmc_harness.render.engine.render(root, reader, facts:dict, draft:str=None, kind="notice") -> {"text","checks":{"facts_ok","unknown_words"},"rewritten":bool}`
- `vmc_harness.render.brief.build(root, session_id=None, allow_llm=False) -> dict` (09 브리핑 스키마, `.harness/cache/brief.json`)
- `vmc_harness.render.brief.inline_notices(root, area) -> list[str]` — 그 영역과 겹치는 꼭 알릴 일·흐름 한 줄씩(완료 보고 블록에 싣는다, 보인 것으로 표시). task report가 부른다
- `vmc_harness.render.brief.daily_two_lines(root, agent=None) -> list[str]` — Codex·mod 없는 환경의 완료 보고 아래 하루 1회 2줄(없으면 빈 목록)
- `vmc_harness.render.approval_card.card(root, approval_id) -> {"lines":[≤4],"text","buttons"}` (CardError면 카드 없음) / `answer(root, id, "approve"|"reject")` / `detail_text(root, id)` — 대화창에서 승인 카드를 보일 때
- `vmc_harness.render.profile.compute(root, prompts=None) -> dict` — 독자 프로필 재계산(bootstrap이 부른다. 로컬 `reader-profile.json`)
- `vmc_harness.render.engine.evaluate(root, reader, cases, allow_llm=True) -> {"cases","summary"}` — 회귀 세트 ③(eval이 부른다)
- `vmc_harness.render.mod_install.register(root)` / `unregister(root)` / `is_registered(root)` — mod 로컬 경로 설치(init·remove가 부른다)

## 서버·잠금·커널 (dist 소유)
- `vmc_harness.dist.server.call(method, path, body=None, auth=True, timeout=None) -> (status, json)` — 못 닿으면 `(0, {"error":"unreachable"})`, 키 없으면 `(0, {"error":"no_license"})`
- `vmc_harness.dist.lock.acquire(root, ttl_s) -> bool` / `release(root)` — 서버에 못 닿으면 로컬 잠금 파일로
- 커널 설치 위치 `~/.vmc-harness/kernel/<버전>/`, `kernel/current`(지금 버전 이름이 든 **텍스트 파일** — 심볼릭 링크 아님). 해석은 `vmc_harness.dist.kernel.current_version()` / `current_dir()` / `engine_dir()` / `running_from_kernel()`
- CLI 실행기 절대 경로: `vmc_harness.dist.kernel.launcher_path()` (`~/.vmc-harness/bin/vmc-harness` | 윈도우 `vmc-harness.cmd`) — 훅 명령에 PATH 대신 쓸 수 있다
- 사용자 홈 정리: `vmc_harness.dist.uninstall.remove_home(keep_brain=True) -> {"removed":[...], "kept":[...]}` — 커널·bin·라이선스(키체인 포함)·PATH 등록을 지운다. 프로젝트별 정리(훅·.harness·로컬 원본)는 bootstrap `remove`가

## 루프 (qa·loop 소유)
- `vmc_harness.loop.scheduler.register(root) -> {"ok","method":"launchd|schtasks|manual","reason"}` / `unregister(root)` — 등록이 막히면 manual(= `start-loop`만) + must 알림
- `vmc_harness.loop.activity.touch(root)` — 마지막 활동 시각(루프가 돌면 중단 신호)
- 클렌징 조치 장부 규약: `basis={"type":"cleansing","ref":<candidate_id>}`, `layer="code"`, `target={"area":<경로>}`, 밤엔 `status="prepared"` → 아침 머지 뒤 `status="trial"`(+`merged_at`) — ledger 판정이 그대로 센다. 머지 전 버린 준비분은 `status="reverted"` + `discarded=true`

## QA (qa·loop 소유)
- `vmc_harness.qa.install.register(root, agents=("claude","codex"))` / `unregister(root)` — QA 서브에이전트 정의(`.claude/agents/vmc-qa.md`, `.codex/agents/vmc-qa.toml` + `.codex/config.toml` `[agents.vmc-qa]`). **init·remove가 부른다**
- `vmc_harness.qa.policy.check_write(root, target) -> (ok, reason)` — target `{"kind":"data|command|http|url", ...}` 범위 밖 쓰기 판정
- `vmc_harness.qa.scenarios.check_write(root, rel_path, content) -> (ok, reason)` — 시나리오 기대값 출처 메타 게이트
- `vmc_harness.qa.results.regressions(root, scenario_ids=None) -> [id]` — QA 회귀(머지 막는 게이트 신호, finding 아님)
- `vmc_harness.qa.scenarios.covering(root, rel_paths) -> [id]` — 소스 경로를 덮는 시나리오
- PASS 기록 때 지금 작업에 `gates.evidence.make(..., kind="qa_scenario", target=<시나리오 id>)` 마커를 남긴다
