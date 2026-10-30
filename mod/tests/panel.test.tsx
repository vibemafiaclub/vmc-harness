// mod는 엔진 `vmc-harness brief --json`을 그리기만 하고, 버튼은 해당 vmc-harness 명령을 실행한다(spec 08).
import { expect, test } from 'claude-code/testing'

const BRIEF = {
  installed: true,
  status_line: '🛡 3 · 증거 ✓ · 확인 1 · 알림 2',
  ui: { tabs: ['내 작업', '팀 대시보드'], per_section: 3 },
  me: {
    current_task: {
      task_id: 'T-261028-ab12cd', title: '반려 후 재상신 오류', expect: '8~15분', elapsed_min: 11,
      evidence: [{ kind: 'qa_scenario', label: 'QA 시나리오 결재 3건', ok: true, file: null, is_image: false }],
      evidence_ok: true, out_of_scope: [{ path: 'menu.tsx', reason: '폭 조정' }],
    },
    approvals: [{ approval_id: 'P-261028-aaaaaa', lines: ['운영 적용 · 반려 sp 수정', '권장 승인 — 회수 상태 원인 수정', '원복 가능 (롤백 sql 준비됨) · 영향 행 2'], buttons: ['승인', '거절', '자세히'] }],
    notices: [
      { id: 'n1', kind: 'gate_blocked', level: 'must', text: '운영 DB 쓰기 시도 차단', when: '어제', buttons: ['자세히', '알림 끄기'], seen: false, refs: {} },
      { id: 'n2', kind: 'gate_disabled', level: 'must', text: "'정산 페이지 폭' 게이트 꺼짐", when: '어제', buttons: ['켜기', '유지'], seen: false, refs: { gate_id: 'g1' } },
      { id: 'n3', kind: 'guide_conflict', level: 'flow', text: "'전체 말고 한 페이지만' — 이 지침과 부딪힌 것 같음: 전체 페이지에 적용", when: '10/28', buttons: ['원복', '아님'], seen: false, refs: { guide_id: 'K-1' } },
      { id: 'n4', kind: 'structure_change', level: 'flow', text: 'b', when: '10/28', buttons: ['자세히'], seen: false, refs: {} },
    ],
    learned: [{ action_id: 'A-1', text: '페이지 하나 먼저 적용 후 확산', source: '요청' }],
    learned_summary: { fixed: 1, prediction_hit: { prev: 0.41, now: 0.49 } },
    proposals: [],
  },
  team: {
    week: '10/27 ~ 10/31',
    numbers: { rows: [{ key: 'blocked', label: '사전 차단', value: '4건', note: '운영 DB 쓰기 시도 등' }] },
    spread: { users: 3, users_total: 3, names: [], shared_judgments: 7 },
    harness_did: { added: 3, reverted: 1, cleaned: 2, items: [] },
    report_line: 'AI 개발 표준 체계 적용 1주차 — 사전 차단 4건',
  },
}

const PANE = {
  component: 'Pane',
  requestId: 'vmc-harness',
  props: { title: '하네스', isFocused: false, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 60 }, view: {} },
} as const

test('패널: 엔진 브리핑을 그리고 버튼은 vmc-harness 명령을 부른다', async ($, on) => {
  const calls: string[][] = []
  on('process.run', ($, e) => {
    calls.push([...e.argv])
    const out = e.argv.includes('--json') && e.argv.includes('brief') ? JSON.stringify(BRIEF) : '{}'
    return { value: { exitCode: 0, stdout: out, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: { command: 'harness' } }))
  on('session.id', () => ({ value: 'sess-1' }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.panes', () => ({ value: [] }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('clock.every', () => ({ value: undefined }))
  await $.session.start({ cwd: '/proj', surface: 'terminal', isInteractive: true })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'vmc-harness', surface, ...PANE })
    expect(await ui.find({ type: 'Text', text: /반려 후 재상신 오류/ })).toBeDefined()
    expect(await ui.find({ key: 'ap-P-261028-aaaaaa' })).toBeDefined()
    // 칸마다 3장 + 더 보기
    expect(await ui.find({ key: 'more-nt' })).toBeDefined()
    await ui.press({ key: 'ap-P-261028-aaaaaa' })
    expect(calls.some(c => c.join(' ').endsWith('approval P-261028-aaaaaa approve'))).toBe(true)
    await ui.press({ key: 'n1-알림 끄기' })
    expect(calls.some(c => c.join(' ').endsWith('notice mute n1'))).toBe(true)
    await ui.press({ key: 'n3-원복' })
    expect(calls.some(c => c.join(' ').endsWith('notice revert n3'))).toBe(true)
    await ui.press({ key: 'n3-아님' })
    expect(calls.some(c => c.join(' ').endsWith('notice dismiss n3'))).toBe(true)
    await ui.press({ key: 'tab-team' })
    expect(await ui.find({ type: 'Text', text: /표준 체계/ })).toBeDefined()
    await ui.press({ key: 'tab-me' })
    await ui.unmount()
  }
})

test('/harness는 대화(모델 컨텍스트)에 아무것도 남기지 않고, 일반 화면에선 확인 필요가 있을 때만 입력창 위 띠', async ($, on) => {
  const calls: string[][] = []
  on('process.run', ($, e) => {
    calls.push([...e.argv])
    const out = e.argv.includes('--json') && e.argv.includes('brief') ? JSON.stringify(BRIEF) : '{}'
    return { value: { exitCode: 0, stdout: out, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: { command: 'harness' } }))
  on('session.id', () => ({ value: 'sess-1' }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.panes', () => ({ value: [] }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('clock.every', () => ({ value: undefined }))
  on('ui.render', ($, e) => {
    const { Box } = $.ui.resolve(e)
    return <Box key="engine-own" />
  })
  await $.session.start({ cwd: '/proj', surface: 'terminal', isInteractive: true })
  const r = await $.command.run({ command: 'harness', args: '' } as never)
  expect((r as { text?: string }).text).toBeUndefined()
  expect((r as { context?: string[] }).context).toBeUndefined()
  const band = await $.ui.mount({
    plugin: 'vmc-harness', surface: 'terminal', component: 'AbovePrompt',
    props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 80, scroll: { offset: 0, bodyRows: 10 }, view: {} },
    viewport: { columns: 85, rows: 40, isFullscreen: false },
  })
  expect(await band.find({ key: 'band-ap-P-261028-aaaaaa' })).toBeDefined()
  await band.unmount()
  const busy = await $.ui.mount({
    plugin: 'vmc-harness', surface: 'terminal', component: 'AbovePrompt',
    props: { hasSurvey: false, isWorking: true, maxRows: 10, bodyColumns: 80, scroll: { offset: 0, bodyRows: 10 }, view: {} },
    viewport: { columns: 85, rows: 40, isFullscreen: false },
  })
  expect(await busy.find({ key: 'band-ap-P-261028-aaaaaa' })).toBeUndefined()  // 작업 중엔 끼어들지 않는다
  await busy.unmount()
})
