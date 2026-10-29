import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

// The test runner prints console output; the hooks environment's types declare none.
declare const console: { log: (text: string) => void }

import { command, dotBar, factsFrom, livePoint, spark, usd } from '../hooks/lens'

const impact = (u: number, m: number) => ({
  usd_per_day: u, usd_per_active_day: u * 1.4, min_per_day: m, wait_min_per_day: m + 2,
  turn_gain_min_per_day: 2, compactions_per_day: 3, ref_compactions_per_day: 1,
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
      balanced: { threshold: 500_000, action: 'set', flags: [], vs_default: impact(-204.8, 4.8), vs_current: impact(0.7, -0.3) },
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
    expect(all).toContain('▼−$204.8')
    expect(all).toContain('▲+4.8m')
    expect(all).toContain('⚠') // the session still runs at the default, not the profile
    await band.unmount()
  }
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
