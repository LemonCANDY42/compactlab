import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

// The test runner prints console output; the hooks environment's types declare none.
declare const console: { log: (text: string) => void }

import { command, dotBar, factsFrom, livePoint, spark, strWidth, usd } from '../hooks/lens'

const impact = (u: number, m: number, cp: number | null = null, tp: number | null = null) => ({
  usd_per_day: u, usd_per_active_day: u * 1.4, min_per_day: m, wait_min_per_day: m + 2,
  turn_gain_min_per_day: 2, compactions_per_day: 3, ref_compactions_per_day: 1, cost_pct: cp, time_pct: tp,
})
const LENS = {
  schema: 1,
  demo: false,
  groups: [{
    harness: 'claude-code', model: 'claude-opus-5-5', role: 'main', default_threshold: 967_000,
    current_threshold: 400_000, effective_max: 967_000, max_window: 1_000_000, days: 30, active_days: 21,
    no_action_needed: false,
    profiles: {
      cost: { threshold: 200_000, action: 'set', flags: ['below_quarter_window'], vs_default: impact(-340, 93.5), vs_current: impact(-20, 30) },
      balanced: { threshold: 500_000, action: 'set', flags: [], vs_default: impact(-204.8, 4.8, -0.61, 0.004), vs_current: impact(0.7, -0.3, 0.012, -0.002) },
      time: { threshold: 575_000, action: 'set', flags: [], vs_default: impact(-169, 4.2), vs_current: impact(1.2, -0.5) },
    },
  }],
}

const BAND = {
  plugin: 'compactlab-lens',
  component: 'AbovePrompt',
  props: { hasSurvey: false, isWorking: false, maxRows: 8, bodyColumns: 120, scroll: { offset: 0, bodyRows: 8 }, view: {} },
} as const
const PANE = {
  plugin: 'compactlab-lens',
  component: 'Pane',
  requestId: 'compactlab-lens',
  props: { title: 'compactlab lens', isFocused: false, bodyColumns: 48, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
} as const

type Seed = { lens?: unknown; source?: string; rawMax?: number; tokens?: number; lang?: string; now?: number }

/** Stands in for the engine: usage, model, env and a read-only file system holding the lens file (or not). */
function engine(on: On, seed: Seed = {}) {
  const copies: string[] = []
  mock.clock(on, { now: (seed.now ?? 1_800_000_000) * 1000 })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('turn.complete', () => ({ text: '' }))
  on('session.model', () => ({ value: 'claude-opus-5-5[1m]' }))
  on('session.usage', () => ({
    value: {
      startedAt: 0, rateLimits: [],
      context: {
        tokens: seed.tokens ?? 412_000, window: 1_000_000, percent: 41,
        breakdown: { categories: [], totalTokens: 412_000, maxTokens: seed.rawMax ?? 1_000_000,
          rawMaxTokens: seed.rawMax ?? 1_000_000, autocompactSource: seed.source ?? 'auto', percentage: 41, gridRows: [] },
      },
    },
  }) as never)
  on('env.get', (_, e) => ({ value: e.name === 'HOME' ? '/home/u' : e.name === 'LANG' ? (seed.lang ?? 'en_US.UTF-8') : undefined }))
  on('fs.exists', (_, e) => ({ value: seed.lens !== undefined && e.path === '/home/u/.compactlab/lens.json' }))
  on('fs.read', () => ({ value: JSON.stringify(seed.lens) }))
  on('command.register', () => ({ value: { command: 'compactlab-lens' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.copy', (_, e) => {
    copies.push(e.text)
    return { value: { isCopied: true } }
  })
  on('ui.toast', () => ({ value: undefined }))
  return copies
}

const text = async (ui: { find: (q: object) => Promise<unknown> }) => JSON.stringify(await ui.find({ type: 'Box' }))

test('the band shows context, both thresholds and per-day chips, on terminal and desktop', async ($, on) => {
  engine(on, { lens: LENS })
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  for (const surface of ['terminal', 'desktop'] as const) {
    const band = await $.ui.mount({ ...BAND, surface })
    const all = await text(band)
    expect(all).toContain('412k')
    expect(all).toContain('┊967k')
    expect(all).toContain('┃500k')
    expect(all).toContain('▼−61%') // percent of the default first, the amount after it
    expect(all).toContain('(−$204.8/day)')
    expect(all).toContain('◷▲+0.4%')
    expect(all).toContain('⚠') // the session still runs at the default, not the profile
    await band.unmount()
  }
})

test('an older lens file without percents leads with the amounts', async ($, on) => {
  const old = JSON.parse(JSON.stringify(LENS)) as typeof LENS
  for (const p of Object.values(old.groups[0]!.profiles)) { p.vs_default.cost_pct = null; p.vs_default.time_pct = null }
  engine(on, { lens: old })
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const all = await text(band)
  expect(all).toContain('▼−$204.8/day')
  expect(all).not.toContain('%')
  await band.unmount()
})

test('without a lens file the band shows the hint icon and the card says to run simulate', async ($, on) => {
  engine(on)
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await text(band)).toContain('ⓘ')
  await band.press({ key: 'fold' })
  expect(await text(band)).toContain('compactlab simulate')
  await band.unmount()
})

test('switching profile changes only the display; copy puts /autocompact on the clipboard', async ($, on) => {
  const copies = engine(on, { lens: LENS })
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  for (const surface of ['terminal', 'desktop'] as const) {
    const band = await $.ui.mount({ ...BAND, surface })
    await band.press({ key: 'fold' })
    await band.press({ key: 'p-cost' })
    expect(await text(band)).toContain('┃200k')
    await band.press({ key: 'copy' })
    expect(copies.at(-1)).toBe('/autocompact 200k')
    await band.press({ key: 'p-balanced' })
    await band.press({ key: 'fold' })
    await band.unmount()
  }
})

test('no warning once the live window matches the profile', async ($, on) => {
  engine(on, { lens: LENS, source: 'settings', rawMax: 500_000 })
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await text(band)).not.toContain('⚠')
  await band.unmount()
})

test('the pane draws an Svg curve on desktop and glyphs on the terminal', async ($, on) => {
  engine(on, { lens: LENS })
  await $.session.start({ cwd: '/w', surface: 'desktop', isInteractive: true })
  const desk = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const tree = await text(desk)
  expect(tree).toContain('"type":"Svg"')
  expect(tree).toContain('500k')
  await desk.unmount()
  const term = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await text(term)).toContain('/autocompact 500k')
  await term.unmount()
})

test('pure helpers: dot bar ticks, sparkline, sign convention, result.json groups', () => {
  const bar = dotBar(412_000, 1_000_000, 967_000, 500_000, 20).map(c => c.ch).join('')
  expect(bar.indexOf('┃')).toBe(9)
  expect(bar.indexOf('┊')).toBe(18)
  expect(bar.startsWith('●●●●●●●●')).toBe(true)
  expect(spark([0, 500_000, 1_000_000], 1_000_000)).toBe('▁▅█')
  expect(usd(-204.8)).toBe('−$204.8')
  expect(usd(0.7)).toBe('+$0.7')
  expect(command(450_000)).toBe('/autocompact 450k')
  expect(livePoint(1_000_000, 'auto', 967_000)).toBe(967_000)
  expect(livePoint(300_000, 'settings', 967_000)).toBe(300_000)
  const per = (x: number) => ({ per_day: x, per_active_day: x })
  const imp = { usd: per(-1), minutes: per(2), wait_minutes: per(3), turn_gain_minutes: per(1), compactions: per(4), ref_compactions: per(1) }
  const full = {
    groups: [{
      harness: 'codex', model: 'gpt-5-codex', role: 'main', days: 5, active_days: 4, no_action_needed: false,
      default: { threshold: 360_000 }, current: null,
      profiles: { cost: { threshold: 300_000, flags: [] }, balanced: { threshold: 350_000, flags: [] }, time: { threshold: 350_000, flags: [] } },
      comparison: {
        cost: { action: 'set', vs_default: { impact: imp }, vs_current: null },
        balanced: { action: 'keep default', vs_default: { impact: imp }, vs_current: null },
        time: { action: 'keep default', vs_default: { impact: imp }, vs_current: null },
      },
    }],
  }
  const f = factsFrom(JSON.stringify(full), '/r.json', 'gpt-5-codex')
  expect(f.status).toBe('ok')
  if (f.status === 'ok') expect(f.group.profiles.cost.vs_default?.usd_per_day).toBe(-1)
  expect(factsFrom('{', '/x', 'm').status).toBe('unreadable')
  expect(factsFrom(JSON.stringify(LENS), '/l', 'claude-sonnet-5-5').status).toBe('no-group')
})

test('labels follow the locale, one language at a time', async ($, on) => {
  engine(on, { lens: LENS, lang: 'zh_CN.UTF-8' })
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await band.press({ key: 'fold' })
  const all = (JSON.parse(await text(band)) as { text: string }).text
  expect(all).toContain('均衡')
  expect(all).toContain('/天')
  expect(all).not.toContain('balanced')
  await band.unmount()
})

/** Rows of a drawn tree as plain text: a column Box puts children on lines, anything else joins inline. */
type Node = { type?: string; props?: { flexDirection?: string }; text?: string; children?: unknown[] }
const rows = (n: unknown): string[] => {
  if (typeof n === 'string' || typeof n === 'number') return [String(n)]
  const node = n as Node
  if (node?.type === 'Box' && node.props?.flexDirection === 'column')
    return (node.children ?? []).flatMap(rows)
  return [(node?.children ?? []).map(c => rows(c).join(' ')).join(node?.type === 'Box' ? ' ' : '')]
}

test('capture: the band folded and unfolded, as text', async ($, on) => {
  engine(on, { lens: LENS })
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  console.log('--- folded ---\n' + rows(await band.find({ type: 'Box' })).join('\n'))
  await band.press({ key: 'fold' })
  const tree = JSON.parse(await text(band)) as unknown
  console.log('--- unfolded ---\n' + rows(tree).join('\n'))
  await band.unmount()
})

test('calibration age shows as an icon, amber once older than twice the interval', async ($, on) => {
  const NOW = 1_800_000_000
  engine(on, { now: NOW, lens: { ...LENS, calibration: { at: NOW - 3 * 3600, every_s: 86400 } } })
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  const fresh = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await text(fresh)).toContain('◴3.0h')
  expect(await text(fresh)).not.toContain('compactlab recalibrate')
  await fresh.unmount()
})

test('a stale calibration says so in the card', async ($, on) => {
  const NOW = 1_800_000_000
  engine(on, { now: NOW, lens: { ...LENS, calibration: { at: NOW - 3 * 86400, every_s: 86400 } } })
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await band.press({ key: 'fold' })
  const all = await text(band)
  expect(all).toContain('◴3.0d')
  expect(all).toContain('compactlab recalibrate')
  await band.unmount()
})

type Drawn = { type?: string; props?: { flexDirection?: string; plain?: boolean }; children?: unknown[] }
const flat = (n: unknown): string =>
  typeof n === 'string' || typeof n === 'number' ? String(n) : ((n as Drawn)?.children ?? []).map(flat).join('')
/** The drawn rows: each row Box's children as [text, columns] pairs (a boxed Button draws `[ label ]`). */
const drawnRows = (tree: unknown): { text: string; width: number; parts: string[] }[] => {
  const out: { text: string; width: number; parts: string[] }[] = []
  const walk = (n: unknown) => {
    const d = n as Drawn
    if (d?.type === 'Box' && d.props?.flexDirection === 'row') {
      const kids = (d.children ?? []) as Drawn[]
      const parts = kids.map(flat)
      const width = kids.reduce((a, c, i) => a + strWidth(parts[i] ?? '') + (c.type === 'Button' && !c.props?.plain ? 4 : 0), 0)
        + Math.max(0, kids.length - 1)
      out.push({ text: kids.map((c, i) => (c.type === 'Button' && !c.props?.plain ? `[ ${parts[i]} ]` : parts[i])).join(' '), width, parts })
      return
    }
    for (const c of d?.children ?? []) walk(c)
  }
  walk(tree)
  return out
}

for (const lang of ['en_US.UTF-8', 'zh_CN.UTF-8']) {
  for (const cols of [30, 40, 55, 80, 120]) {
    test(`band fits ${cols} columns (${lang.slice(0, 2)}), folded and unfolded, no token split`, async ($, on) => {
      engine(on, { lens: { ...LENS, calibration: { at: 1_800_000_000 - 7200, every_s: 86400 } }, lang })
      await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
      const band = await $.ui.mount({ ...BAND, surface: 'terminal', props: { ...BAND.props, bodyColumns: cols } })
      for (const fold of [false, true]) {
        if (fold) await band.press({ key: 'fold' })
        const rows = drawnRows(JSON.parse(await text(band)) as unknown)
        expect(rows.length).toBeGreaterThan(0)
        for (const r of rows) expect(r.width).toBeLessThanOrEqual(cols)
        const parts = rows.flatMap(r => r.parts)
        // numbers keep their units: thresholds and the per-day chips are each one drawn piece
        expect(parts).toContain('┃500k')
        expect(parts).toContain('┊967k')
        // changes lead with the percent of the default, each one drawn piece; the amount follows dim when wide
        expect(parts).toContain('▼−61%')
        expect(parts).toContain('◷▲+0.4%')
        if (cols >= 60 && rows[0]?.parts.includes('(−$204.8/day)')) expect(rows[0]?.parts.indexOf('(−$204.8/day)')).toBe(rows[0]?.parts.indexOf('▼−61%') + 1)
        if (fold) expect(parts.some(x => /^\(−\$20[45](\.8)?\/(day|d|天)\)$/.test(x))).toBe(true)
        for (const x of parts) expect(x).not.toMatch(/^(k|y|ay|\/d|\/day)$/) // no stray unit fragments
        if (fold) expect(parts.some(x => x.includes('/autocompact 500k'))).toBe(true)
        // the wait / turn-time split is a delta too: arrow and sign
        if (fold) expect(parts.some(x => /▲\+6\.8m$/.test(x)) && parts.some(x => /▼−2\.0m$/.test(x))).toBe(true)
        if (cols >= 60 || (cols >= 55 && lang.startsWith('en'))) expect(rows[0]?.parts.includes('⟳0')).toBe(true) // one row
        if (lang.startsWith('en')) console.log(`--- ${cols} cols${fold ? ', unfolded' : ''} ---\n` + rows.map(r => r.text).join('\n'))
      }
      await band.unmount()
    })
  }
}

test('the desktop pane keeps its SVG and fits a narrow pane', async ($, on) => {
  engine(on, { lens: LENS })
  await $.session.start({ cwd: '/w', surface: 'desktop', isInteractive: true })
  const pane = await $.ui.mount({ ...PANE, surface: 'desktop', props: { ...PANE.props, bodyColumns: 34 } })
  const tree = JSON.parse(await text(pane)) as unknown
  expect(JSON.stringify(tree)).toContain('"type":"Svg"')
  for (const r of drawnRows(tree)) expect(r.width).toBeLessThanOrEqual(34)
  await pane.unmount()
})

test('a recalibrated lens file is picked up on the next turn, for the same model', async ($, on) => {
  const NOW = 1_800_000_000
  const seed: Seed = { now: NOW, lens: { ...LENS, calibration: { at: NOW - 3 * 3600, every_s: 86400 } } }
  engine(on, seed)
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await band.press({ key: 'fold' })
  expect(await text(band)).toContain('┃500k')
  expect(await text(band)).toContain('◴3.0h')
  // `compactlab simulate` rewrites the file: new balanced threshold, fresh calibration; the model is unchanged
  const g = LENS.groups[0]
  seed.lens = {
    ...LENS,
    groups: [{ ...g, profiles: { ...g.profiles, balanced: { ...g.profiles.balanced, threshold: 450_000 } } }],
    calibration: { at: NOW - 60, every_s: 86400 },
  }
  await $.turn.complete({ reason: 'answer', answer: '', durationMs: 1, isAborted: false, turnId: 't1' } as never)
  const all = await text(band)
  expect(all).toContain('┃450k')
  expect(all).not.toContain('┃500k')
  expect(all).not.toContain('◴3.0h')
  await band.unmount()
})
