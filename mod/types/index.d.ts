// vmc-harness mod 상태 계약. 브리핑 모양은 엔진 `vmc-harness brief --json`(spec 09 브리핑 스키마)을 그대로 받는다.

export type BriefNotice = {
  id: string
  kind: string
  level: 'must' | 'flow'
  text: string
  when: string
  buttons: string[]
  seen: boolean
  refs: Record<string, unknown>
}

export type BriefCard = {
  approval_id: string
  lines: string[]
  buttons: string[]
}

export type BriefEvidence = { kind: string; label: string; ok: boolean; file: string | null; is_image: boolean }

export type BriefTask = {
  task_id: string
  title: string
  expect: string | null
  elapsed_min: number | null
  evidence: BriefEvidence[]
  evidence_ok: boolean | null
  out_of_scope: { path: string | null; reason: string | null }[]
}

export type BriefLearned = { action_id: string; text: string; source: string; author?: string }

export type BriefRow = { key: string; label: string; value: string; note?: string | null }

export type Brief = {
  installed: boolean
  status_line: string
  ui: { tabs: string[]; per_section: number }
  me: {
    current_task: BriefTask | null
    approvals: BriefCard[]
    notices: BriefNotice[]
    learned: BriefLearned[]
    learned_summary: { fixed: number; prediction_hit: { prev: number | null; now: number | null } }
    proposals: { id: string; text: string; buttons: string[] }[]
  }
  team: {
    week: string
    numbers: { rows: BriefRow[] }
    spread: { users: number; users_total: number; names: string[]; shared_judgments: number }
    harness_did: { added: number; reverted: number; cleaned: number; items: { action_id: string; text: string }[] }
    report_line: string
  }
}

export type Tab = 'me' | 'team'

export type Detail = { key: string; text: string; link: string | null }

declare module 'claude-code' {
  interface PluginState {
    'vmc-harness': {
      brief: Brief | null
      tab: Tab
      expanded: string[]
      detail: Detail | null
      message: string | null
    }
  }
}
