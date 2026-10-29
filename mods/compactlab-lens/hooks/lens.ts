// Pure helpers for compactlab-lens: reading the lens file, matching this session's model, and the glyph drawings.
// Nothing here touches `$`, so the tests can call these directly.

import type { LensFacts, LensGroup, LensImpact, LensProfileName } from '../types'

export const PROFILES: readonly LensProfileName[] = ['cost', 'balanced', 'time']
export type Lang = 'en' | 'zh'

const L = {
  en: {
    cost: 'cost', balanced: 'balanced', time: 'time', ctx: 'context', def: 'default', cur: 'current',
    perDay: '/day', saves: 'saves', adds: 'adds', copy: 'Copy', copied: 'Copied', noClip: 'Copy failed',
    hint: 'No compactlab result yet: run compactlab simulate', noGroup: 'No compactlab figures for this model',
    unreadable: 'The lens file could not be read', live: 'Live auto-compact window', differs: 'differs from',
    env: 'set by CLAUDE_CODE_AUTO_COMPACT_WINDOW, which wins over /autocompact', compactions: 'compactions this session',
    expand: 'Show the lens card', collapse: 'Hide the lens card', wait: 'wait', turns: 'turns', keep: 'keep default',
    caption: 'vs harness default · list-price estimate · recall is a lite-mode proxy', demo: 'synthetic demo data',
    curve: 'context per turn', profile: 'Profile', title: 'compactlab lens',
  },
  zh: {
    cost: '省钱', balanced: '均衡', time: '省时', ctx: '上下文', def: '默认', cur: '当前',
    perDay: '/天', saves: '节省', adds: '增加', copy: '复制', copied: '已复制', noClip: '复制失败',
    hint: '还没有 compactlab 结果:运行 compactlab simulate', noGroup: '这个模型没有 compactlab 数据',
    unreadable: '无法读取 lens 文件', live: '当前自动压缩窗口', differs: '不同于',
    env: '由 CLAUDE_CODE_AUTO_COMPACT_WINDOW 设定,优先于 /autocompact', compactions: '本会话压缩次数',
    expand: '展开 lens 卡片', collapse: '收起 lens 卡片', wait: '等待', turns: '每轮', keep: '保持默认',
    caption: '对 harness 默认 · 按公开标价估算 · 召回为 lite 代理值', demo: '合成演示数据',
    curve: '每轮上下文', profile: '方案', title: 'compactlab lens',
  },
} as const

export type Words = (typeof L)['en']
export const words = (lang: Lang): Words => L[lang] as Words

export const langOf = (setting: string | undefined, env: string | undefined): Lang =>
  setting === 'en' || setting === 'zh' ? setting : /^zh/i.test(env ?? '') ? 'zh' : 'en'

export const k = (n: number | null | undefined): string =>
  n == null ? '–' : n >= 1_000_000 ? `${+(n / 1_000_000).toFixed(2)}M` : `${Math.round(n / 1000)}k`

const MINUS = '−'
/** '−$204.8' saves, '+$0.7' adds. */
export const usd = (v: number): string =>
  Math.abs(v) < 0.05 ? '$0' : `${v < 0 ? MINUS : '+'}$${Math.abs(v) < 1000 ? Math.abs(v).toFixed(1) : Math.round(Math.abs(v)).toLocaleString('en-US')}`
export const mins = (v: number): string =>
  Math.abs(v) < 0.05 ? '0m' : `${v < 0 ? MINUS : '+'}${Math.abs(v) < 100 ? Math.abs(v).toFixed(1) : Math.round(Math.abs(v))}m`
export const arrow = (v: number): string => (Math.abs(v) < 0.05 ? '' : v < 0 ? '▼' : '▲')
/** Theme colour for a delta: savings green, additions amber (red is kept for broken guardrails). */
export const tone = (v: number): 'success' | 'warning' | undefined =>
  Math.abs(v) < 0.05 ? undefined : v < 0 ? 'success' : 'warning'

/** `/autocompact 450k`: what the copy button puts on the clipboard. */
export const command = (threshold: number): string => `/autocompact ${k(threshold)}`

/** Model ids as sessions report them (`claude-opus-5-5[1m]`, a date suffix) matched to compactlab's group names. */
export const canonical = (model: string): string =>
  model.toLowerCase().replace(/\[.*?\]$/, '').replace(/-\d{8}$/, '').trim()

/** Reads either lens.json (schema 1) or a full result.json, and picks this model's group for `role`. */
export const factsFrom = (text: string, path: string, model: string, role = 'main'): LensFacts => {
  let data: { groups?: unknown; demo?: unknown }
  try {
    data = JSON.parse(text) as { groups?: unknown; demo?: unknown }
  } catch {
    return { status: 'unreadable', path }
  }
  const groups = Array.isArray(data.groups) ? (data.groups as Record<string, unknown>[]) : []
  const want = canonical(model)
  const g = groups.find(x => canonical(String(x.model)) === want && x.role === role)
    ?? groups.find(x => want.startsWith(canonical(String(x.model))) && x.role === role)
  if (!g) return { status: 'no-group', path }
  const group = 'default_threshold' in g ? (g as unknown as LensGroup) : fromResultGroup(g)
  return group ? { status: 'ok', path, group, isDemo: Boolean(data.demo) } : { status: 'unreadable', path }
}

type Imp = { usd: Per; minutes: Per; wait_minutes: Per; turn_gain_minutes: Per; compactions: Per; ref_compactions: Per }
type Per = { per_day: number; per_active_day: number | null }

const impactOf = (c: { impact?: Imp } | null | undefined): LensImpact | null => {
  const im = c?.impact
  if (!im) return null
  return {
    usd_per_day: im.usd.per_day,
    usd_per_active_day: im.usd.per_active_day,
    min_per_day: im.minutes.per_day,
    wait_min_per_day: im.wait_minutes.per_day,
    turn_gain_min_per_day: im.turn_gain_minutes.per_day,
    compactions_per_day: im.compactions.per_day,
    ref_compactions_per_day: im.ref_compactions.per_day,
  }
}

/** A group of a full result.json (schema 4+) in lens.json's shape, or null when it predates per-day impact. */
const fromResultGroup = (g: Record<string, unknown>): LensGroup | null => {
  const r = g as {
    harness: string; model: string; role: string; days: number; active_days?: number; no_action_needed: boolean
    effective_max?: number; max_window?: number; default: { threshold: number }
    current?: { threshold: number; configured?: number } | null
    profiles: Record<string, { threshold: number; flags: string[] }>
    comparison: Record<string, { action: string; vs_default: { impact?: Imp }; vs_current?: { impact?: Imp } | null }>
  }
  if (!r.profiles || !r.comparison || !r.default) return null
  const profiles = {} as LensGroup['profiles']
  for (const n of PROFILES) {
    const p = r.profiles[n], c = r.comparison[n]
    if (!p || !c) return null
    profiles[n] = { threshold: p.threshold, action: c.action, flags: [...p.flags],
      vs_default: impactOf(c.vs_default), vs_current: impactOf(c.vs_current) }
  }
  return {
    harness: r.harness, model: r.model, role: r.role, default_threshold: r.default.threshold,
    current_threshold: r.current ? (r.current.configured ?? r.current.threshold) : null,
    effective_max: r.effective_max ?? null, max_window: r.max_window ?? null, days: r.days,
    active_days: r.active_days ?? null, no_action_needed: r.no_action_needed, profiles,
  }
}

export type Cell = { ch: string; tone: 'fill' | 'empty' | 'default' | 'profile' | 'both' }

/** A dot bar of `cells` over `[0, max]`: filled up to `tokens`, with tick marks at the two thresholds. */
export const dotBar = (tokens: number, max: number, def: number | null, prof: number | null, cells = 24): Cell[] => {
  const at = (v: number | null) => (v == null || max <= 0 ? -1 : Math.min(cells - 1, Math.max(0, Math.round((v / max) * cells) - 1)))
  const filled = max > 0 ? Math.round((Math.min(tokens, max) / max) * cells) : 0
  const d = at(def), p = at(prof)
  return Array.from({ length: cells }, (_, i): Cell => {
    if (i === d && i === p) return { ch: '┃', tone: 'both' }
    if (i === p) return { ch: '┃', tone: 'profile' }
    if (i === d) return { ch: '┊', tone: 'default' }
    return i < filled ? { ch: '●', tone: 'fill' } : { ch: '·', tone: 'empty' }
  })
}

const BLOCKS = '▁▂▃▄▅▆▇█'
/** The last `width` turns of the context curve as block glyphs, scaled to `max`. */
export const spark = (curve: readonly number[], max: number, width = 24): string =>
  curve.slice(-width).map(v => BLOCKS[Math.min(7, Math.max(0, Math.round((v / Math.max(max, 1)) * 7)))]).join('')

/** The live compaction point: the window the breakdown measured against, else the harness default. */
export const livePoint = (compactWindow: number | null, source: string | null, def: number): number =>
  compactWindow && source && source !== 'auto' ? compactWindow : def

/** Whether the live point is off the profile's threshold by more than 5%. */
export const differs = (live: number, prof: number): boolean => Math.abs(live - prof) > prof * 0.05

/** A small SVG of the session's context per turn with the default and profile thresholds as lines. */
export const curveSvg = (curve: readonly number[], max: number, def: number, prof: number, dark = false): string => {
  const W = 280, H = 84, pad = 4
  const ink = dark ? '#8f8c83' : '#77746c', acc = dark ? '#d97757' : '#c15f3c'
  const y = (v: number) => (H - pad - (Math.min(v, max) / Math.max(max, 1)) * (H - 2 * pad)).toFixed(1)
  const pts = curve.length > 1
    ? curve.map((v, i) => `${(pad + (i / (curve.length - 1)) * (W - 2 * pad)).toFixed(1)},${y(v)}`).join(' ')
    : ''
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">`
    + `<line x1="${pad}" x2="${W - pad}" y1="${y(def)}" y2="${y(def)}" stroke="${ink}" stroke-width="1" stroke-dasharray="3 3"/>`
    + `<line x1="${pad}" x2="${W - pad}" y1="${y(prof)}" y2="${y(prof)}" stroke="${acc}" stroke-width="1.2"/>`
    + (pts ? `<polyline points="${pts}" fill="none" stroke="${acc}" stroke-width="1.6" stroke-linejoin="round"/>` : '')
    + `</svg>`
}
