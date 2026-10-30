// VMC 하네스 mod (spec 08 §화면). 보여주는 창고일 뿐이다 — 데이터·판정은 엔진·장부.
// - `vmc-harness brief --json`(spec 09 브리핑 스키마)을 불러 그리기만 한다. 버튼은 해당 `vmc-harness` 명령을 실행한다
//   (버튼 누름 = 사람의 명시적 행동 → finding은 엔진이 만든다).
// - 모델 컨텍스트에 아무것도 넣지 않는다: prompt.compose·session.append·tool 등록 없음, /harness 명령은 text 없이 답한다.
// - 패널형: 전체화면(110칸 이상)에선 대화 옆 도킹, 일반 화면에선 /harness로 입력창 위 인라인.
//   일반 화면에서 확인 필요(승인 카드)가 있을 때만 입력창 위 한 줄 띠. 상태 줄 `🛡 3 · 증거 ✓ · 확인 1 · 알림 2`.
// - 세션 시작에 띄우지 않는다(전체화면 옆 도킹만 — 대화를 가리지 않는다). 작업 중(모델 턴)에는 띠를 그리지 않는다.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Brief, BriefCard, BriefNotice, Detail, Tab } from '../types'

const PANE = 'vmc-harness'
const REFRESH_MS = 60_000
const CLI_TIMEOUT_MS = 90_000

const brief = atom({ plugin: 'vmc-harness', key: 'brief' } as const, null)
const tab = atom({ plugin: 'vmc-harness', key: 'tab' } as const, 'me')
const expanded = atom({ plugin: 'vmc-harness', key: 'expanded' } as const, [])
const detail = atom({ plugin: 'vmc-harness', key: 'detail' } as const, null)
const message = atom({ plugin: 'vmc-harness', key: 'message' } as const, null)

// 엔진 CLI. 설치본은 PATH의 vmc-harness, 없으면 python -m vmc_harness.
const CLI: readonly (readonly string[])[] = [['vmc-harness'], ['python3', '-m', 'vmc_harness'], ['python', '-m', 'vmc_harness']]

let prefix: readonly string[] | null = null
let fullscreen: boolean | undefined
let autoOpened = false
const shownNotices = new Set<string>()
const shownCards = new Set<string>()
const marked = new Set<string>()

async function cli($: EngineInterface, args: string[]): Promise<{ ok: boolean; out: string }> {
  const tries = prefix ? [prefix] : CLI
  for (const pre of tries) {
    try {
      const r = await $.process.run([...pre, ...args], { timeoutMs: CLI_TIMEOUT_MS })
      prefix = pre
      return { ok: r.exitCode === 0, out: r.stdout }
    } catch {
      continue
    }
  }
  return { ok: false, out: '' }
}

// 그려진 알림·카드를 '보임'으로(엔진이 다시 알리지 않게). 그리는 중에는 쓰지 않고 다음 갱신 때 몰아서.
async function flushShown($: EngineInterface) {
  const ids = [...shownNotices].filter(id => !marked.has(id))
  if (ids.length) {
    const r = await cli($, ['notice', 'shown', ...ids])
    if (r.ok) ids.forEach(id => marked.add(id))
  }
  for (const id of [...shownCards]) {
    if (marked.has(id)) continue
    const r = await cli($, ['approval', id, 'shown'])
    if (r.ok) marked.add(id)
  }
}

async function refresh($: EngineInterface) {
  await flushShown($)
  let sid = ''
  try {
    sid = await $.session.id()
  } catch {
    sid = ''
  }
  const r = await cli($, ['brief', '--json', ...(sid ? ['--session', sid] : [])])
  if (!r.ok) return
  let b: Brief | null = null
  try {
    b = JSON.parse(r.out) as Brief
  } catch {
    return
  }
  if (!b || !b.installed) {
    $.ui.status(undefined)
    await update($, brief, () => null)
    return
  }
  const next = b
  await update($, brief, () => next)
  $.ui.status(next.status_line)
  if (fullscreen === true && !autoOpened) {
    autoOpened = true
    const open = (await $.ui.panes()).some(p => p.id === PANE)
    if (!open) void $.ui.open({ id: PANE, title: '하네스' })
  }
}

async function act($: EngineInterface, args: string[], done?: string) {
  const r = await cli($, args)
  await update($, message, () => (r.ok ? done ?? null : '실행 안 됨: vmc-harness ' + args.join(' ')))
  await refresh($)
}

async function toggleDetail($: EngineInterface, d: Detail) {
  await update($, detail, cur => (cur && cur.key === d.key ? null : d))
}

async function detailFrom($: EngineInterface, key: string, args: string[]) {
  const cur = await read($, detail)
  if (cur && cur.key === key) {
    await update($, detail, () => null)
    return
  }
  const r = await cli($, args)
  let text = r.out.trim()
  let link: string | null = null
  try {
    const j = JSON.parse(text) as { pdf?: string | null; html?: string }
    link = j.pdf || j.html || null
    text = j.pdf ? 'PDF' : 'HTML (브라우저가 없어 PDF 없음)'
  } catch {
    if (/\.html$/.test(text)) {
      link = text
      text = '이번 주 브리핑 HTML'
    }
  }
  await update($, detail, () => ({ key, text, link: link ? 'file://' + link : null }))
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'harness', description: '하네스 패널: 내 작업 · 팀 대시보드' })
    void refresh($)
    $.clock.every(REFRESH_MS, () => void refresh($))
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const out = await next(e)
    void refresh($)
    return out
  })

  // /harness [team] — 패널 열기(사람이 연 것이라 110칸 이상이면 도킹, 아니면 입력창 위 인라인). 대화에 아무것도 남기지 않는다.
  on('command.run', { command: 'harness' }, async ($, e) => {
    const args = String((e as { args?: unknown }).args ?? '').trim()
    if (args === 'team' || args === '팀') await update($, tab, () => 'team' as Tab)
    await $.ui.open({ id: PANE, title: '하네스' })
    void refresh($)
    return {}
  })

  // 일반 화면: 확인 필요가 있을 때만 입력창 위 한 줄. 전체화면 여부도 여기서 안다(패널 자동 도킹 판단용).
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    fullscreen = e.viewport?.isFullscreen
    const b = await read($, brief)
    if (!b || fullscreen !== false || e.props.isWorking || e.props.hasSurvey || b.me.approvals.length === 0) {
      return next(e)
    }
    const open = (await $.ui.panes()).some(p => p.id === PANE && p.isPlaced)
    if (open) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const c = b.me.approvals[0]
    if (!c) return next(e)
    shownCards.add(c.approval_id)
    return (
      <Box flexDirection="row" gap={1}>
        <Text color="#d97757">확인 {b.me.approvals.length}</Text>
        <Text wrap="truncate-end">{c.lines[0] ?? ''}</Text>
        <Button key={'band-ap-' + c.approval_id} variant="primary" label="승인" onPress={() => void act($, ['approval', c.approval_id, 'approve'], '승인')} />
        <Button key={'band-rj-' + c.approval_id} label="거절" onPress={() => void act($, ['approval', c.approval_id, 'reject'], '거절')} />
        <Button key={'band-dt-' + c.approval_id} label="자세히" onPress={() => void $.ui.open({ id: PANE, title: '하네스' })} />
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Link } = $.ui.resolve(e)
    const b = await read($, brief)
    const t = await read($, tab)
    const ex = await read($, expanded)
    const d = await read($, detail)
    const msg = await read($, message)
    const cols = Math.max(20, e.props.bodyColumns)
    const rule = (title: string) => '── ' + title + ' ' + '─'.repeat(Math.max(2, Math.floor(cols - title.length * 2 - 4)))
    const per = b?.ui.per_section ?? 3
    const limit = <T,>(key: string, xs: T[]): T[] => (ex.includes(key) ? xs : xs.slice(0, per))
    const more = (key: string, n: number) =>
      n > per ? (
        <Button key={'more-' + key} plain dimColor label={ex.includes(key) ? '접기' : '더 보기 ' + (n - per)}
          onPress={() => void update($, expanded, xs => (xs.includes(key) ? xs.filter(x => x !== key) : [...xs, key]))} />
      ) : null

    const tabs = (
      <Box flexDirection="row" gap={1}>
        <Text color="#d97757" bold>하네스</Text>
        <Button key="tab-me" variant={t === 'me' ? 'primary' : 'secondary'} label="내 작업" onPress={() => void update($, tab, () => 'me' as Tab)} />
        <Button key="tab-team" variant={t === 'team' ? 'primary' : 'secondary'} label="팀 대시보드" onPress={() => void update($, tab, () => 'team' as Tab)} />
        {t === 'team' && b ? <Text dimColor>{b.team.week}</Text> : null}
      </Box>
    )
    if (!b) {
      return (
        <Box flexDirection="column">
          {tabs}
          <Text dimColor>하네스 정보를 불러오는 중이거나 이 프로젝트에 하네스가 없습니다.</Text>
        </Box>
      )
    }

    const detailBox = d ? (
      <Box key="detail" flexDirection="column" borderStyle="round" borderDimColor paddingX={1}>
        {d.text.split('\n').slice(0, 40).map((l, i) => <Text key={'dl' + i}>{l}</Text>)}
        {d.link ? <Link href={d.link} label={d.link.replace('file://', '')} /> : null}
      </Box>
    ) : null

    if (t === 'team') {
      const tm = b.team
      return (
        <Box flexDirection="column">
          {tabs}
          {detailBox}
          <Text dimColor>{rule('이번 주')}</Text>
          {tm.numbers.rows.map(r => (
            <Box key={'row-' + r.key} flexDirection="row" gap={1}>
              <Text>{r.label}</Text>
              <Text bold>{r.value}</Text>
              {r.note ? <Text dimColor wrap="truncate-end">{r.note}</Text> : null}
            </Box>
          ))}
          <Text dimColor>{rule('확산')}</Text>
          <Box flexDirection="row" gap={1}>
            <Text>사용 인원</Text>
            <Text bold>{tm.spread.users} / {tm.spread.users_total}</Text>
            <Text dimColor wrap="truncate-end">{tm.spread.names.join(' · ')}</Text>
          </Box>
          <Box flexDirection="row" gap={1}>
            <Text>공유된 판단</Text>
            <Text bold>{tm.spread.shared_judgments}개</Text>
            <Text dimColor>한 사람만 알던 규칙</Text>
          </Box>
          <Text dimColor>{rule('하네스가 한 일')}</Text>
          <Box flexDirection="row" gap={1}>
            <Text>추가 {tm.harness_did.added} · 원복 {tm.harness_did.reverted} · 정리 {tm.harness_did.cleaned}</Text>
            <Button key="did-detail" label="자세히" onPress={() => void toggleDetail($, {
              key: 'did', link: null,
              text: tm.harness_did.items.map(x => '· ' + x.text).join('\n') || '이번 주 추가한 것 없음',
            })} />
          </Box>
          <Text dimColor>{rule('보고용 한 줄')}</Text>
          <Box borderStyle="round" borderDimColor paddingX={1}>
            <Text>{tm.report_line}</Text>
          </Box>
          <Box flexDirection="row" gap={1}>
            <Button key="copy" variant="primary" label="복사" onPress={press => void $.ui.copy({ text: tm.report_line, surface: press.surface }).then(() => $.ui.toast('복사됨'))} />
            <Button key="pdf" label="PDF" onPress={() => void detailFrom($, 'pdf', ['brief', '--pdf'])} />
            <Button key="team-detail" label="자세히" onPress={() => void detailFrom($, 'html', ['brief', '--html'])} />
          </Box>
          {msg ? <Text dimColor>{msg}</Text> : null}
        </Box>
      )
    }

    const m = b.me
    const ct = m.current_task
    const card = (c: BriefCard) => {
      shownCards.add(c.approval_id)
      return (
        <Box key={'card-' + c.approval_id} flexDirection="column" borderStyle="round" borderColor="#7a5240" paddingX={1}>
          {c.lines.map((l, i) => <Text key={c.approval_id + '-l' + i} color={i === 0 ? '#d97757' : undefined}>{l}</Text>)}
          <Box flexDirection="row" gap={1}>
            <Button key={'ap-' + c.approval_id} variant="primary" label="승인" onPress={() => void act($, ['approval', c.approval_id, 'approve'], '승인')} />
            <Button key={'rj-' + c.approval_id} label="거절" onPress={() => void act($, ['approval', c.approval_id, 'reject'], '거절')} />
            <Button key={'dt-' + c.approval_id} label="자세히" onPress={() => void detailFrom($, 'ap-' + c.approval_id, ['approval', c.approval_id, 'detail'])} />
          </Box>
        </Box>
      )
    }
    const noticeBtn = (n: BriefNotice, name: string) => {
      const k = n.id + '-' + name
      if (name === '알림 끄기') return <Button key={k} label={name} onPress={() => void act($, ['notice', 'mute', n.id], '알림 끔')} />
      if (name === '켜기') return <Button key={k} label={name} onPress={() => void act($, ['notice', 'on', n.id], '켬')} />
      if (name === '유지') return <Button key={k} label={name} onPress={() => void act($, ['notice', 'keep', n.id], '유지')} />
      if (name === '원복') return <Button key={k} label={name} onPress={() => void act($, ['notice', 'revert', n.id], '원복')} />
      if (name === '아님') return <Button key={k} label={name} onPress={() => void act($, ['notice', 'dismiss', n.id], '아님')} />
      return <Button key={k} label={name} onPress={() => void toggleDetail($, {
        key: 'nt-' + n.id, link: null,
        text: n.text + '\n' + Object.entries(n.refs).map(([a, v]) => a + ': ' + String(v)).join('\n'),
      })} />
    }
    const notice = (n: BriefNotice) => {
      if (!n.seen) shownNotices.add(n.id)
      const dot = n.level === 'must' ? 'red' : 'blue'
      return (
        <Box key={'nt-' + n.id} flexDirection="column" borderStyle="round" borderDimColor paddingX={1}>
          <Box flexDirection="row" gap={1}>
            <Text color={dot}>●</Text>
            <Text dimColor={n.seen} wrap="wrap">{n.text}</Text>
            <Text dimColor>· {n.when}</Text>
          </Box>
          <Box flexDirection="row" gap={1}>{n.buttons.map(name => noticeBtn(n, name))}</Box>
        </Box>
      )
    }
    const ph = m.learned_summary.prediction_hit
    const pct = (x: number | null) => (x === null ? '' : Math.round(x * 100) + '%')
    const summary = [
      m.learned_summary.fixed ? '고친 판단 ' + m.learned_summary.fixed : '',
      ph.now !== null ? '예측 적중 ' + (ph.prev !== null ? pct(ph.prev) + ' → ' : '') + pct(ph.now) : '',
    ].filter(Boolean).join(' · ')

    return (
      <Box flexDirection="column">
        {tabs}
        {detailBox}
        <Text dimColor>{rule('지금 작업')}</Text>
        {ct ? (
          <Box flexDirection="column">
            <Box flexDirection="row" gap={1}>
              <Text wrap="truncate-end">{ct.title}</Text>
              <Text dimColor>{[ct.expect ? '예상 ' + ct.expect : '', ct.elapsed_min !== null ? ct.elapsed_min + '분 경과' : ''].filter(Boolean).join(' · ')}</Text>
            </Box>
            {limit('ev', ct.evidence).map((ev, i) => (
              <Box key={'ev-' + i} flexDirection="row" gap={1}>
                <Text>증거</Text>
                <Text color={ev.ok ? 'green' : 'red'}>{ev.ok ? '✓' : '✗'}</Text>
                {ev.is_image && ev.file ? <Link href={'file://' + ev.file} label={ev.label} /> : <Text>{ev.label}</Text>}
              </Box>
            ))}
            {more('ev', ct.evidence.length)}
            {ct.out_of_scope.length ? (
              <Box flexDirection="row" gap={1}>
                <Text>범위 밖 변경 {ct.out_of_scope.length}건 · {ct.out_of_scope.map(x => x.path ?? '').slice(0, per).join(', ')}</Text>
                <Button key="oos-detail" label="자세히" onPress={() => void toggleDetail($, {
                  key: 'oos', link: null,
                  text: ct.out_of_scope.map(x => (x.path ?? '') + (x.reason ? ': ' + x.reason : '')).join('\n'),
                })} />
              </Box>
            ) : null}
          </Box>
        ) : <Text dimColor>진행 중인 작업 없음</Text>}
        <Text dimColor>{rule('확인 필요')}</Text>
        {m.approvals.length ? limit('ap', m.approvals).map(card) : <Text dimColor>없음</Text>}
        {more('ap', m.approvals.length)}
        <Text dimColor>{rule('알아둘 것')}</Text>
        {m.notices.length ? limit('nt', m.notices).map(notice) : <Text dimColor>없음</Text>}
        {more('nt', m.notices.length)}
        {limit('pp', m.proposals).map(p => (
          <Box key={'pp-' + p.id} flexDirection="row" gap={1}>
            <Text wrap="wrap">◇ {p.text}</Text>
            <Button key={'pp-add-' + p.id} label="추가" onPress={() => void act($, ['notice', 'add', p.id], '추가')} />
            <Button key={'pp-ex-' + p.id} label="제외" onPress={() => void act($, ['notice', 'exclude', p.id], '제외')} />
          </Box>
        ))}
        {more('pp', m.proposals.length)}
        <Text dimColor>{rule('이번 주 배운 것')}</Text>
        {m.learned.length ? limit('ln', m.learned).map(x => (
          <Box key={'ln-' + x.action_id} flexDirection="row" gap={1}>
            <Text wrap="wrap">· {x.text}{x.author ? ' · ' + x.author : ''}</Text>
            <Button key={'ln-del-' + x.action_id} label="삭제" onPress={() => void act($, ['notice', 'delete', x.action_id], '삭제')} />
          </Box>
        )) : <Text dimColor>없음</Text>}
        {more('ln', m.learned.length)}
        {summary ? (
          <Box flexDirection="row" gap={1}>
            <Text>{summary}</Text>
            <Button key="growth-detail" label="자세히" onPress={() => void detailFrom($, 'html', ['brief', '--html'])} />
          </Box>
        ) : null}
        {msg ? <Text dimColor>{msg}</Text> : null}
      </Box>
    )
  })
}
