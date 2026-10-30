# vmc-harness

VMC 하네스 소스(비공개). **설계 원본은 context-hub `projects/vmc-harness/`** — 이 레포는 그 구현이다.
- 설계: `/Users/sumin/repos/vibemafiaclub/context-hub/projects/vmc-harness/spec/` (00·01을 먼저, 그다음 맡은 문서)
- 의도 원문: 같은 폴더 `intent/` — 명세가 애매하면 원문이 우선
- spec과 구현이 달라지면(스키마 필드 등) spec 문서도 함께 고친다

## 규칙

- 엔진은 **Python ≥3.9 표준 라이브러리만**(DB 조회만 선택 의존성 pymssql/pyodbc). 셸 문법 의존 금지 — subprocess 인자는 리스트로(`core/proc.py`). macOS·Windows 둘 다 돈다(경로는 pathlib, 저장 경로 구분자는 `/`)
- 설정 숫자는 `core/config.get()`으로만. 게이트 코드에 숫자 리터럴 금지(커널 CI가 검사). `config/defaults.toml`의 기존 키를 쓰고, 새 키는 영역 파일 `config/<영역>.toml`에 같은 주석 메타 형식으로(키 중복은 로드 오류)
- 장부 기록은 `core/ledger.py` 함수로만(메타 게이트 강제가 거기 있다). 로컬 원본 이벤트는 `core/events.py`
- 에이전트에게 주입하는 문구는 `core/inject.message()`로만(01 P5). 게이트 메시지는 무엇이 빠졌는지·무엇을 어겼는지만(01 P6)
- 고객 데이터(세션 원본·코드·DB 정보·인용)를 이 레포에 넣지 않는다. 테스트 픽스처는 합성 데이터만
- **공개 레포다(2026-10-30).** 고객사·고객 담당자 이름, 고객 레포의 실제 파일·DB 이름, 계정·리소스 식별자(Cloudflare 계정, 라이선스 id 등)를 코드·주석·커밋 메시지에 쓰지 않는다. 결함 출처는 "현장 결함 C1"처럼 번호만. 운영 문서는 context-hub `projects/vmc-harness/`
- 테스트: `cd engine && python3 -m unittest discover -s tests -t .` — 영역마다 `tests/test_<영역>_*.py`
- 하위 명령은 `engine/vmc_harness/commands/<이름>.py`에 `register(subparsers)` — cli.py가 자동 등록

## 영역 소유 (병렬 구현 때 남의 영역 파일을 고치지 않는다 — 코어 수정이 필요하면 함수 추가만)

| 영역 | 폴더·명령 | spec |
|---|---|---|
| core | `core/`, `cli.py`, `hooks/contract.py`, `config/defaults.toml` 뼈대 | 09·11 |
| gates | `hooks/dispatcher.py`, `hooks/adapters/`, `gates/`, 명령 `hook`·`evidence`·`impact`·`proceed`·`gate` | 02 |
| db | `db/`, `core/secrets.py`, 명령 `db`·`db-credential` | 02 |
| task | `task/`, `sessions/`, 명령 `declare`·`report` | 03 |
| ledger·brain | `ledger/`, `brain/`, 명령 `judge`·`why`·`revert`·`mcp`·`link-brain`·`finding`·`selfcheck` | 04 |
| bootstrap | `bootstrap/`, 명령 `init`·`diagnose`·`remove`·`map` | 07·08(구조 지도) |
| qa·loop | `qa/`, `loop/`, 명령 `qa`·`start-loop`·`loop` | 05·06 |
| render·brief·mod | `render/`, `mod/`, 명령 `render`·`brief`·`approval`·`notice` | 08 |
| dist | `dist/`, `install/`, `scripts/release.py`, 명령 `activate`·`update` | 10 |
| eval·kernel | `eval/`, `PRINCIPLES.md`, `docs/kernel-writing.md`, 명령 `eval`, 커널 CI | 01·11 |
