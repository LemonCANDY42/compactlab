import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, RenderChildren } from 'claude-code'

import type { LensFacts, LensLive, LensProfileName } from '../types'
import {
  PROFILES, arrow, command, curveSvg, differs, dotBar, factsFrom, k, langOf, livePoint, mins, spark, tone, usd, words,
} from './lens'
import type { Cell, Lang } from './lens'

// compactlab-lens: this session's context against the harness default and the compactlab-optimised auto-compact
// threshold, with the per-day savings `compactlab simulate` estimated for this model. A one-row band above the
// prompt (expandable) and, on desktop, a pane. It only reads the engine's own figures and one local file: no
// model calls, no network, no processes, and it never writes settings. "Copy" puts `/autocompact <n>` on the
// clipboard for the person to paste.

const PANE = 'compactlab-lens'
const facts = atom({ plugin: 'compactlab-lens', key: 'facts' } as const, null as LensFacts | null, { shape: 'facts-1' })
const live = atom({ plugin: 'compactlab-lens', key: 'live' } as const, null as LensLive | null, { shape: 'live-1' })
const chosen = atom({ plugin: 'compactlab-lens', key: 'profile' } as const, null as LensProfileName | null)
const isOpen = atom({ plugin: 'compactlab-lens', key: 'isOpen' } as const, false)
const CURVE_MAX = 120

async function langFor($: EngineInterface, setting: string): Promise<Lang> {
  return langOf(setting, (await $.env.get('LC_ALL')) || (await $.env.get('LANG')))
}

async function profileFor($: EngineInterface, fallback: LensProfileName): Promise<LensProfileName> {
  return (await read($, chosen)) ?? fallback
}

/** Reads the lens file (or a full result.json) for this model; read only, never written. */
async function load($: EngineInterface, lensPath: string, model: string): Promise<void> {
  const home = (await $.env.get('HOME')) ?? ''
  const given = lensPath.trim().replace(/^~(?=\/|$)/, home)
  const paths = given ? [given] : [`${home}/.compactlab/lens.json`, `${home}/.compactlab/result.json`]
  let found: LensFacts = { status: 'missing', path: paths[0] ?? '' }
  for (const path of paths) {
    if (!(await $.fs.exists(path))) continue
    try {
      found = factsFrom(await $.fs.read(path), path, model)
    } catch {
      found = { status: 'unreadable', path }
    }
    break
  }
  await update($, facts, () => found)
}

/** Context figures and the compaction window in force; `breakdown: 'summary'` estimates locally and sends nothing. */
async function measure($: EngineInterface, isTurnEnd: boolean): Promise<string> {
  const u = await $.session.usage({ breakdown: 'summary' })
  const model = await $.session.model()
  const b = u.context.breakdown
  await update($, live, l => {
    const tokens = u.context.tokens ?? l?.tokens ?? 0
    const curve = isTurnEnd && tokens > 0 ? [...(l?.curve ?? []), tokens].slice(-CURVE_MAX) : (l?.curve ?? [])
    return {
      model, tokens, window: u.context.window || l?.window || 0, curve, compactions: l?.compactions ?? 0,
      compactWindow: b ? b.rawMaxTokens : (l?.compactWindow ?? null),
      source: b ? b.autocompactSource : (l?.source ?? null),
    }
  })
  return model
}

export const register: Register = (on, options) => {
  const setting = String(options.language ?? 'auto')
  const lensPath = String(options.lensPath ?? '')
  const fallback: LensProfileName = (PROFILES as readonly string[]).includes(String(options.profile))
    ? (options.profile as LensProfileName) : 'balanced'

  on('session.start', async ($, e, next) => {
    const done = await next(e)
    const model = await measure($, false)
    await load($, lensPath, model)
    await $.command.register({ name: 'compactlab-lens', description: 'Open the compactlab lens pane' })
    if (e.surface === 'desktop') void $.ui.open({ id: PANE, title: 'compactlab lens' })
    return done
  })

  on('command.run', { command: 'compactlab-lens' }, async $ => {
    await load($, lensPath, await measure($, false))
    const { isPlaced } = await $.ui.open({ id: PANE, title: 'compactlab lens', focus: true })
    return { text: isPlaced ? 'compactlab lens opened.' : 'compactlab lens: no surface can place the pane right now.' }
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    const before = (await read($, live))?.model
    const model = await measure($, true)
    if (model !== before) await load($, lensPath, model)
    return done
  })

  on('session.compact', async ($, e, next) => {
    const done = await next(e)
    if (!e.agentId) await update($, live, l => (l ? { ...l, compactions: l.compactions + 1 } : l))
    return done
  }).catch(($, e, next) => next(e)) // counting is cosmetic: a failure here must never stand in a compaction's way

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const l = await read($, live)
    if (e.props.hasSurvey || !l || l.window <= 0) return next(e)
    const w = words(await langFor($, setting))
    const f = await read($, facts)
    const name = await profileFor($, fallback)
    const open = await read($, isOpen)
    const { Box, Button, Text } = $.ui.resolve(e)
    const g = f?.status === 'ok' ? f.group : null
    const p = g?.profiles[name] ?? null
    const def = g?.default_threshold ?? null
    const prof = p?.threshold ?? null
    const cells = Math.max(10, Math.min(24, Math.floor((e.props.bodyColumns - 60) / 2)))
    const bar = dotBar(l.tokens, l.window, def, prof, cells)
    const im = p?.vs_default ?? null
    const here = def != null ? livePoint(l.compactWindow, l.source, def) : null
    const isOff = here != null && prof != null && !g?.no_action_needed && differs(here, prof)

    const head = (
      <Box key="row" flexDirection="row" gap={1}>
        <Button key="fold" plain label={open ? w.collapse : w.expand} onPress={() => update($, isOpen, v => !v)}>
          {open ? '▾' : '▸'}
        </Button>
        <Text>{bar.map(c => <Text color={cellColor(c)} dimColor={c.tone === 'empty'}>{c.ch}</Text>)}</Text>
        <Text bold>{k(l.tokens)}</Text>
        {def != null && <Text dimColor>┊{k(def)}</Text>}
        {prof != null && <Text color="claude">┃{k(prof)}</Text>}
        {im && <Text color={tone(im.usd_per_day)}>{arrow(im.usd_per_day)}{usd(im.usd_per_day)}{w.perDay}</Text>}
        {im && <Text color={tone(im.min_per_day)}>{arrow(im.min_per_day)}{mins(im.min_per_day)}{w.perDay}</Text>}
        <Text dimColor>⟳{l.compactions}</Text>
        {isOff && <Text color="warning">⚠</Text>}
        {!g && <Text color="warning">ⓘ</Text>}
      </Box>
    )
    if (!open) return head
    return (
      <Box flexDirection="column">
        {head}
        {detail($, { Box, Button, Text }, e.surface, w, l, f, name, isOff, here)}
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const w = words(await langFor($, setting))
    const l = await read($, live)
    const f = await read($, facts)
    const name = await profileFor($, fallback)
    const table = $.ui.resolve(e)
    const { Box, Button, Text } = table
    const g = f?.status === 'ok' ? f.group : null
    const p = g?.profiles[name] ?? null
    const here = l && g ? livePoint(l.compactWindow, l.source, g.default_threshold) : null
    const isOff = here != null && p != null && !g?.no_action_needed && differs(here, p.threshold)
    const chart = e.surface === 'desktop' && 'Svg' in table && l && g && p
      ? (() => {
          const { Svg } = table as typeof table & { Svg: (props: { source: string; alt: string; width?: number }) => RenderChildren }
          return (
            <Svg
              source={curveSvg(l.curve.length ? l.curve : [l.tokens], l.window, g.default_threshold, p.threshold)}
              alt={`${w.curve}: ${k(l.tokens)} · ${w.def} ${k(g.default_threshold)} · ${w[name]} ${k(p.threshold)}`}
              width={Math.min(320, e.props.bodyColumns * 8)}
            />
          )
        })()
      : null
    return (
      <Box flexDirection="column" gap={1}>
        <Text bold>{l?.model ?? '–'}</Text>
        {l ? detail($, { Box, Button, Text }, e.surface, w, l, f, name, isOff, here, chart) : <Text dimColor>…</Text>}
      </Box>
    )
  })
}

const cellColor = (c: Cell): string | undefined =>
  c.tone === 'profile' || c.tone === 'both' ? 'claude' : c.tone === 'fill' ? 'text' : c.tone === 'default' ? 'subtle' : undefined

type Els = { Box: (p: any) => any; Button: (p: any) => any; Text: (p: any) => any } // eslint-disable-line @typescript-eslint/no-explicit-any

/** The expanded card: curve, chips vs default and current, profile switch, copy, and one caption line. */
function detail(
  $: EngineInterface, { Box, Button, Text }: Els, surface: string, w: ReturnType<typeof words>, l: LensLive,
  f: LensFacts | null, name: LensProfileName, isOff: boolean, here: number | null, chart: RenderChildren = null,
): RenderChildren {
  if (!f || f.status !== 'ok') {
    const msg = !f || f.status === 'missing' ? w.hint : f.status === 'no-group' ? w.noGroup : w.unreadable
    return <Text color="warning">ⓘ {msg}</Text>
  }
  const g = f.group, p = g.profiles[name]
  const chips = (label: string, im: typeof p.vs_default) =>
    im && (
      <Box key={label} flexDirection="row" gap={1}>
        <Text dimColor>{label}</Text>
        <Text color={tone(im.usd_per_day)}>$ {arrow(im.usd_per_day)}{usd(im.usd_per_day)}{w.perDay}</Text>
        <Text color={tone(im.min_per_day)}>◷ {arrow(im.min_per_day)}{mins(im.min_per_day)}{w.perDay}</Text>
        <Text dimColor>({w.wait} {mins(im.wait_min_per_day)} · {w.turns} {mins(-im.turn_gain_min_per_day)})</Text>
        <Text dimColor>⟳ {im.compactions_per_day.toFixed(1)}{w.perDay}</Text>
      </Box>
    )
  return (
    <Box flexDirection="column">
      {chart ?? (
        <Box flexDirection="row" gap={1}>
          <Text color="claude">{spark(l.curve.length ? l.curve : [l.tokens], l.window)}</Text>
          <Text dimColor>{w.curve}</Text>
        </Box>
      )}
      {chips(`${w.def} ${k(g.default_threshold)}`, p.vs_default)}
      {g.current_threshold != null && chips(`${w.cur} ${k(g.current_threshold)}`, p.vs_current)}
      <Box flexDirection="row" gap={1}>
        {PROFILES.map(n => (
          <Button key={`p-${n}`} plain={n === name ? undefined : true} variant={n === name ? 'primary' : undefined}
            label={`${w.profile}: ${w[n]} ${k(g.profiles[n].threshold)}`} onPress={() => update($, chosen, () => n)}>
            {w[n]} {k(g.profiles[n].threshold)}
          </Button>
        ))}
        <Button key="copy" label={`${w.copy} ${command(p.threshold)}`}
          onPress={async () => {
            const r = await $.ui.copy({ text: command(p.threshold), surface: surface as never })
            $.ui.toast(r.isCopied ? `${w.copied}: ${command(p.threshold)}` : w.noClip)
          }}>
          ⧉ {command(p.threshold)}
        </Button>
        {p.action === 'keep default' && <Text dimColor>{w.keep}</Text>}
      </Box>
      {isOff && here != null && (
        <Text color="warning">
          ⚠ {w.live} {k(here)} {w.differs} {w[name]} {k(p.threshold)}{l.source === 'env' ? ` · ${w.env}` : ''}
        </Text>
      )}
      <Text dimColor>{f.isDemo ? `${w.demo} · ` : ''}{w.caption}</Text>
    </Box>
  )
}
