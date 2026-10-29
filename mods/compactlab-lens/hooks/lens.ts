// Pure helpers for compactlab-lens: reading the lens file, matching this session's model, and the glyph drawings.
// Nothing here touches `$`, so the tests can call these directly.

import type { LensCalibration, LensFacts, LensGroup, LensImpact, LensProfileName } from '../types'

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
    curve: 'context per turn', profile: 'Profile', title: 'compactlab lens', calibrated: 'calibrated',
    stale: 'stale: run compactlab recalibrate', ago: 'ago', perD: '/d', liveShort: 'live',
  },
  zh: {
    cost: '省钱', balanced: '均衡', time: '省时', ctx: '上下文', def: '默认', cur: '当前',
    perDay: '/天', saves: '节省', adds: '增加', copy: '复制', copied: '已复制', noClip: '复制失败',
    hint: '还没有 compactlab 结果:运行 compactlab simulate', noGroup: '这个模型没有 compactlab 数据',
    unreadable: '无法读取 lens 文件', live: '当前自动压缩窗口', differs: '不同于',
    env: '由 CLAUDE_CODE_AUTO_COMPACT_WINDOW 设定,优先于 /autocompact', compactions: '本会话压缩次数',
    expand: '展开 lens 卡片', collapse: '收起 lens 卡片', wait: '等待', turns: '每轮', keep: '保持默认',
    caption: '对 harness 默认 · 按公开标价估算 · 召回为 lite 代理值', demo: '合成演示数据',
    curve: '每轮上下文', profile: '方案', title: 'compactlab lens', calibrated: '校准于',
    stale: '已过期:运行 compactlab recalibrate', ago: '前', perD: '/天', liveShort: '当前',
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
/** A share of the default as a signed percent: '−61%', '+0.4%' (one decimal under 10%). */
export const pct = (v: number): string => {
  const a = Math.abs(v * 100)
  return a < 0.05 ? '0%' : `${v < 0 ? MINUS : '+'}${a < 10 ? a.toFixed(1) : Math.round(a)}%`
}
/** Arrow for a share (a fraction): the same 0.05 dead zone, measured in percent points. */
const parrow = (v: number): string => arrow(v * 100)
const ptone = (v: number) => tone(v * 100)
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
  let data: { groups?: unknown; demo?: unknown; calibration?: { at?: unknown; every_s?: unknown } }
  try {
    data = JSON.parse(text) as typeof data
  } catch {
    return { status: 'unreadable', path }
  }
  const groups = Array.isArray(data.groups) ? (data.groups as Record<string, unknown>[]) : []
  const want = canonical(model)
  const g = groups.find(x => canonical(String(x.model)) === want && x.role === role)
    ?? groups.find(x => want.startsWith(canonical(String(x.model))) && x.role === role)
  if (!g) return { status: 'no-group', path }
  const group = 'default_threshold' in g ? (g as unknown as LensGroup) : fromResultGroup(g)
  const c = data.calibration
  const calibration: LensCalibration | null = c && typeof c.at === 'number'
    ? { at: c.at, every_s: typeof c.every_s === 'number' ? c.every_s : null } : null
  return group ? { status: 'ok', path, group, isDemo: Boolean(data.demo), calibration } : { status: 'unreadable', path }
}

type Imp = { usd: Per; minutes: Per; wait_minutes: Per; turn_gain_minutes: Per; compactions: Per; ref_compactions: Per }
type Cmp = { impact?: Imp; d_cost?: number; d_cost_pct?: number; d_time_s?: number; d_time_pct?: number }
// compare() writes 0 for a zero reference; a nonzero change over a zero reference has no percent
const share = (p: number | undefined, d: number | undefined): number | null => (p == null || (p === 0 && d) ? null : p)
type Per = { per_day: number; per_active_day: number | null }

const impactOf = (c: Cmp | null | undefined): LensImpact | null => {
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
    cost_pct: share(c?.d_cost_pct, c?.d_cost),
    time_pct: share(c?.d_time_pct, c?.d_time_s),
  }
}

/** A group of a full result.json (schema 4+) in lens.json's shape, or null when it predates per-day impact. */
const fromResultGroup = (g: Record<string, unknown>): LensGroup | null => {
  const r = g as {
    harness: string; model: string; role: string; days: number; active_days?: number; no_action_needed: boolean
    effective_max?: number; max_window?: number; default: { threshold: number }
    current?: { threshold: number; configured?: number } | null
    profiles: Record<string, { threshold: number; flags: string[] }>
    comparison: Record<string, { action: string; vs_default: Cmp; vs_current?: Cmp | null }>
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

/** Age of a calibration as `3h`, `2.5d`; and whether it is stale (older than twice the interval, or a day). */
export const age = (seconds: number): string =>
  seconds < 3600 ? `${Math.max(0, Math.round(seconds / 60))}m` : seconds < 86400 ? `${(seconds / 3600).toFixed(seconds < 36000 ? 1 : 0)}h` : `${(seconds / 86400).toFixed(seconds < 864000 ? 1 : 0)}d`
export const isStale = (at: number, every: number | null, now: number): boolean => now - at > 2 * (every ?? 86400)

// ---------- width-aware layout: tokens are never split; rows break only between tokens ----------

/** Terminal columns a string takes: East Asian wide and fullwidth characters (and ⚠) count two. */
export const strWidth = (text: string): number => {
  let n = 0
  for (const ch of text) {
    const c = ch.codePointAt(0) ?? 0
    const wide = (c >= 0x1100 && c <= 0x115f) || (c >= 0x2e80 && c <= 0xa4cf) || (c >= 0xac00 && c <= 0xd7a3)
      || (c >= 0xf900 && c <= 0xfaff) || (c >= 0xfe30 && c <= 0xfe4f) || (c >= 0xff00 && c <= 0xff60)
      || (c >= 0xffe0 && c <= 0xffe6) || (c >= 0x20000 && c <= 0x3fffd) || c === 0x26a0
    n += c === 0xfe0f ? 0 : wide ? 2 : 1
  }
  return n
}

export type Press = 'fold' | 'copy' | 'p-cost' | 'p-balanced' | 'p-time'
/** One unbreakable piece of a row: a text, a dot bar (`cells`), or a button (`press`). */
export type Tok = {
  key: string; text: string; color?: string; dim?: boolean; bold?: boolean
  cells?: Cell[]; press?: Press; label?: string; boxed?: boolean; primary?: boolean
}

/** A boxed (non-plain) Button is drawn `[ label ]` on the terminal: four more columns. */
export const tokWidth = (t: Tok): number => (t.cells ? t.cells.length : strWidth(t.text)) + (t.boxed ? 4 : 0)
export const rowWidth = (row: readonly Tok[]): number =>
  row.reduce((a, t) => a + tokWidth(t), 0) + Math.max(0, row.length - 1)

/** A prose token wider than the row is broken at its spaces (a number keeps its unit: they share a word), and
 * a word still too wide, as CJK text without spaces is, between characters. Bars and buttons are never split. */
const fit = (t: Tok, cols: number): Tok[] => {
  if (t.cells || t.press || tokWidth(t) <= cols) return [t]
  const out: Tok[] = []
  const words = t.text.split(' ')
  words.forEach(word => {
    let part = ''
    for (const ch of word) {
      if (part && strWidth(part + ch) > cols) {
        out.push({ ...t, key: `${t.key}.${out.length}`, text: part })
        part = ''
      }
      part += ch
    }
    if (part) out.push({ ...t, key: `${t.key}.${out.length}`, text: part })
  })
  return out
}

/** Greedy rows of at most `cols` columns (one column between tokens), breaking only between tokens. */
export const pack = (toks: readonly Tok[], cols: number): Tok[][] => {
  const rows: Tok[][] = []
  let row: Tok[] = []
  for (const t of toks.flatMap(x => fit(x, cols))) {
    if (row.length && rowWidth([...row, t]) > cols) {
      rows.push(row)
      row = []
    }
    row.push(t)
  }
  if (row.length) rows.push(row)
  return rows
}

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))
const usdShort = (v: number): string =>
  Math.abs(v) < 0.5 ? '$0' : `${v < 0 ? MINUS : '+'}$${Math.round(Math.abs(v)).toLocaleString('en-US')}`

export type BandInput = {
  cols: number; isOpen: boolean; tokens: number; window: number; def: number | null; prof: number | null
  usd: number | null; min: number | null; cpct: number | null; tpct: number | null; compactions: number; calAge: string | null; calStale: boolean
  isOff: boolean; hasFacts: boolean; w: Words
}

/**
 * The band's rows for `cols` columns. Changes lead with the percent of the default (`▼−61%`, `◷▲+0.4%`); from 60
 * columns the per-day amount follows, dim (`(−$204.8/day)`). From 40: one row, percent only (a shorter bar) when it
 * fits; without a percent (older lens files) the amount leads instead. Below that, or when even the compact row does not fit: row one
 * holds the fold, the bar and the context, and the chips follow, wrapped between chips.
 */
export const bandRows = (b: BandInput): Tok[][] => {
  const { w } = b
  const fold: Tok = { key: 'fold', text: b.isOpen ? '▾' : '▸', press: 'fold', label: b.isOpen ? w.collapse : w.expand }
  const ctx: Tok = { key: 'ctx', text: k(b.tokens), bold: true }
  const chips = (full: boolean): Tok[] => [
    ...(b.def != null ? [{ key: 'def', text: `┊${k(b.def)}`, dim: true }] : []),
    ...(b.prof != null ? [{ key: 'prof', text: `┃${k(b.prof)}`, color: 'claude' }] : []),
    ...(b.usd != null ? (b.cpct != null
      ? [{ key: 'usd', color: ptone(b.cpct), text: `${parrow(b.cpct)}${pct(b.cpct)}` },
        ...(full ? [{ key: 'usd2', dim: true, text: `(${usd(b.usd)}${w.perDay})` }] : [])]
      : [{ key: 'usd', color: tone(b.usd),
        text: full ? `${arrow(b.usd)}${usd(b.usd)}${w.perDay}` : `${arrow(b.usd)}${usdShort(b.usd)}${w.perD}` }]) : []),
    ...(b.min != null ? (b.tpct != null
      ? [{ key: 'min', color: ptone(b.tpct), text: `◷${parrow(b.tpct)}${pct(b.tpct)}` },
        ...(full ? [{ key: 'min2', dim: true, text: `(${mins(b.min)}${w.perDay})` }] : [])]
      : [{ key: 'min', color: tone(b.min), text: `${arrow(b.min)}${mins(b.min)}${full ? w.perDay : w.perD}` }]) : []),
    { key: 'cmp', text: `⟳${b.compactions}`, dim: true },
    ...(b.calAge != null ? [{ key: 'cal', text: `◴${b.calAge}`, color: b.calStale ? 'warning' : undefined, dim: !b.calStale }] : []),
    ...(b.isOff ? [{ key: 'off', text: '⚠', color: 'warning' }] : []),
    ...(!b.hasFacts ? [{ key: 'hint', text: 'ⓘ', color: 'warning' }] : []),
  ]
  const bar = (cells: number): Tok => ({ key: 'bar', text: '', cells: dotBar(b.tokens, b.window, b.def, b.prof, cells) })
  if (b.cols >= 40) {
    for (const full of b.cols >= 60 ? [true, false] : [false]) {
      const rest = [ctx, ...chips(full)]
      const free = b.cols - rowWidth([fold, ...rest]) - 1
      const cells = Math.min(full ? 24 : 16, free)
      if (cells >= (full ? 8 : 6)) return [[fold, bar(cells), ...rest]]
    }
  }
  const cells = clamp(b.cols - tokWidth(fold) - tokWidth(ctx) - 2, 4, 24)
  return [[fold, bar(cells), ctx], ...pack(chips(b.cols >= 60), b.cols)]
}

export type CardInput = {
  cols: number; w: Words; name: LensProfileName; group: LensGroup; curve: readonly number[]; tokens: number
  window: number; isOff: boolean; here: number | null; isEnv: boolean; isStale: boolean; isDemo: boolean
  withSpark: boolean
}

/** The unfolded card's rows, every one within `cols`. */
export const cardRows = (c: CardInput): Tok[][] => {
  const { w, group: g } = c
  const p = g.profiles[c.name]
  const full = c.cols >= 60
  const unit = full ? w.perDay : w.perD
  const rows: Tok[][] = []
  if (c.withSpark) {
    const label: Tok = { key: 'curve', text: w.curve, dim: true }
    const width = clamp(c.cols - tokWidth(label) - 1, 4, 24)
    rows.push(...pack([{ key: 'spark', text: spark(c.curve.length ? c.curve : [c.tokens], c.window, width), color: 'claude' }, label], c.cols))
  }
  const line = (key: string, lab: string, im: LensImpact | null) => {
    if (!im) return
    const tg = -im.turn_gain_min_per_day
    rows.push(...pack([
      { key: `${key}-l`, text: lab, dim: true },
      ...(im.cost_pct != null
        ? [{ key: `${key}-u`, text: `${parrow(im.cost_pct)}${pct(im.cost_pct)}`, color: ptone(im.cost_pct) },
          { key: `${key}-u2`, text: `(${full ? usd(im.usd_per_day) : usdShort(im.usd_per_day)}${unit})`, dim: true }]
        : [{ key: `${key}-u`, text: `${arrow(im.usd_per_day)}${full ? usd(im.usd_per_day) : usdShort(im.usd_per_day)}${unit}`, color: tone(im.usd_per_day) }]),
      ...(im.time_pct != null
        ? [{ key: `${key}-m`, text: `◷${parrow(im.time_pct)}${pct(im.time_pct)}`, color: ptone(im.time_pct) },
          { key: `${key}-m2`, text: `(${mins(im.min_per_day)}${unit})`, dim: true }]
        : [{ key: `${key}-m`, text: `◷${arrow(im.min_per_day)}${mins(im.min_per_day)}${unit}`, color: tone(im.min_per_day) }]),
      { key: `${key}-w`, text: `${w.wait} ${arrow(im.wait_min_per_day)}${mins(im.wait_min_per_day)}`, dim: true },
      { key: `${key}-t`, text: `${w.turns} ${arrow(tg)}${mins(tg)}`, dim: true },
      { key: `${key}-c`, text: `⟳${im.compactions_per_day.toFixed(1)}${unit}`, dim: true },
    ], c.cols))
  }
  line('def', `${w.def} ${k(g.default_threshold)}`, p.vs_default)
  if (g.current_threshold != null) line('cur', `${w.cur} ${k(g.current_threshold)}`, p.vs_current)
  rows.push(...pack([
    ...PROFILES.map((n): Tok => ({ key: `p-${n}`, press: `p-${n}`, text: `${w[n]} ${k(g.profiles[n].threshold)}`,
      label: `${w.profile}: ${w[n]} ${k(g.profiles[n].threshold)}`, boxed: n === c.name, primary: n === c.name })),
    { key: 'copy', press: 'copy', boxed: true, text: `⧉ ${command(p.threshold)}`, label: `${w.copy} ${command(p.threshold)}` },
    ...(p.action === 'keep default' ? [{ key: 'keep', text: w.keep, dim: true }] : []),
  ], c.cols))
  if (c.isOff && c.here != null) {
    rows.push(...pack([
      { key: 'off', text: '⚠', color: 'warning' },
      { key: 'off-l', text: `${full ? w.live : w.liveShort} ${k(c.here)}`, color: 'warning' },
      { key: 'off-n', text: '≠', color: 'warning' },
      { key: 'off-p', text: `${w[c.name]} ${k(p.threshold)}`, color: 'warning' },
      ...(c.isEnv ? [{ key: 'off-e', text: w.env, color: 'warning' }] : []),
    ], c.cols))
  }
  if (c.isStale) rows.push(...pack([{ key: 'stale', text: `◴ ${w.stale}`, color: 'warning' }], c.cols))
  const caption = [...(c.isDemo ? [w.demo] : []), ...w.caption.split(' · ')]
  rows.push(...pack(caption.map((t, i) => ({ key: `cap-${i}`, text: i < caption.length - 1 ? `${t} ·` : t, dim: true })), c.cols))
  return rows
}
