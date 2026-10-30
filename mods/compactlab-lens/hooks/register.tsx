import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, RenderChildren, RenderElement } from 'claude-code'

import type { LensFacts, LensLive, LensProfileName } from '../types'
import {
  PROFILES, age, bandRows, cardRows, command, curveSvg, differs, factsFrom, isStale, k, langOf, livePoint, pack, words,
} from './lens'
import type { Cell, Lang, Tok, Words } from './lens'

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
    // Re-read every turn, not only when the model changes: `compactlab service` / `simulate` rewrite the file
    // (new calibration, new thresholds) while the session runs. It is one small local file, read only.
    await load($, lensPath, await measure($, true))
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
    const els = $.ui.resolve(e)
    const g = f?.status === 'ok' ? f.group : null
    const p = g?.profiles[name] ?? null
    const def = g?.default_threshold ?? null
    const here = def != null ? livePoint(l.compactWindow, l.source, def) : null
    const isOff = here != null && p != null && !g?.no_action_needed && differs(here, p.threshold)
    const now = (await $.clock.now()) / 1000
    const c = f?.status === 'ok' ? f.calibration : null
    const stale = c ? isStale(c.at, c.every_s, now) : false
    const cols = Math.max(20, e.props.bodyColumns)
    const rows = bandRows({
      cols, isOpen: open, tokens: l.tokens, window: l.window, def, prof: p?.threshold ?? null,
      usd: p?.vs_default?.usd_per_day ?? null, min: p?.vs_default?.min_per_day ?? null,
      cpct: p?.vs_default?.cost_pct ?? null, tpct: p?.vs_default?.time_pct ?? null, compactions: l.compactions,
      calAge: c ? age(now - c.at) : null, calStale: stale, isOff, hasFacts: Boolean(g), w,
    })
    if (open) rows.push(...card(w, l, f, name, isOff, here, stale, cols, true))
    return draw($, els, e.surface, rows, f, name, w)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const w = words(await langFor($, setting))
    const l = await read($, live)
    const f = await read($, facts)
    const name = await profileFor($, fallback)
    const table = $.ui.resolve(e)
    const { Box, Text } = table
    const g = f?.status === 'ok' ? f.group : null
    const p = g?.profiles[name] ?? null
    const here = l && g ? livePoint(l.compactWindow, l.source, g.default_threshold) : null
    const isOff = here != null && p != null && !g?.no_action_needed && differs(here, p.threshold)
    const now = (await $.clock.now()) / 1000
    const stale = f?.status === 'ok' && f.calibration ? isStale(f.calibration.at, f.calibration.every_s, now) : false
    const cols = Math.max(20, e.props.bodyColumns)
    const hasSvg = e.surface === 'desktop' && 'Svg' in table
    const chart = hasSvg && l && g && p
      ? (() => {
          const { Svg } = table as typeof table & { Svg: (props: { source: string; alt: string; width?: number }) => RenderChildren }
          return (
            <Svg
              source={curveSvg(l.curve.length ? l.curve : [l.tokens], l.window, g.default_threshold, p.threshold)}
              alt={`${w.curve}: ${k(l.tokens)} · ${w.def} ${k(g.default_threshold)} · ${w[name]} ${k(p.threshold)}`}
              width={Math.min(320, cols * 8)}
            />
          )
        })()
      : null
    return (
      <Box flexDirection="column" gap={1}>
        <Text bold wrap="truncate">{l?.model ?? '–'}</Text>
        {chart}
        {l ? draw($, table, e.surface, card(w, l, f, name, isOff, here, stale, cols, !chart), f, name, w) : <Text dimColor>…</Text>}
      </Box>
    )
  })
}

/** The unfolded card as rows, or the one-line reason there are no figures. */
function card(
  w: Words, l: LensLive, f: LensFacts | null, name: LensProfileName, isOff: boolean, here: number | null,
  stale: boolean, cols: number, withSpark: boolean,
): Tok[][] {
  if (!f || f.status !== 'ok') {
    const msg = !f || f.status === 'missing' ? w.hint : f.status === 'no-group' ? w.noGroup : w.unreadable
    return pack([{ key: 'why-i', text: 'ⓘ', color: 'warning' }, { key: 'why', text: msg, color: 'warning' }], cols)
  }
  return cardRows({
    cols, w, name, group: f.group, curve: l.curve, tokens: l.tokens, window: l.window, isOff, here,
    isEnv: l.source === 'env', isStale: stale, isDemo: f.isDemo, withSpark,
  })
}

const cellColor = (c: Cell): string | undefined =>
  c.tone === 'profile' || c.tone === 'both' ? 'claude' : c.tone === 'fill' ? 'text' : c.tone === 'default' ? 'subtle' : undefined

type Els = { Box: (p: any) => any; Button: (p: any) => any; Text: (p: any) => any } // eslint-disable-line @typescript-eslint/no-explicit-any

/** Rows of tokens as Boxes that never wrap: the rows were packed to the width, so each token stays whole. */
function draw($: EngineInterface, { Box, Button, Text }: Els, surface: string, rows: Tok[][], f: LensFacts | null,
  name: LensProfileName, w: Words): RenderElement {
  const p = f?.status === 'ok' ? f.group.profiles[name] : null
  const act = (t: Tok): (() => unknown) => {
    if (t.press === 'fold') return () => update($, isOpen, v => !v)
    if (t.press === 'copy' && p) {
      return async () => {
        const r = await $.ui.copy({ text: command(p.threshold), surface: surface as never })
        $.ui.toast(r.isCopied ? `${w.copied}: ${command(p.threshold)}` : w.noClip)
      }
    }
    const n = (t.press ?? '').replace('p-', '') as LensProfileName
    return () => update($, chosen, () => n)
  }
  const node = (t: Tok) =>
    t.cells ? (
      <Text>{t.cells.map(c => <Text color={cellColor(c)} dimColor={c.tone === 'empty'}>{c.ch}</Text>)}</Text>
    ) : t.press ? (
      <Button key={t.key} label={t.label ?? t.text} plain={t.boxed ? undefined : true}
        variant={t.primary ? 'primary' : undefined} onPress={act(t)}>{t.text}</Button>
    ) : (
      <Text color={t.color} dimColor={t.dim} bold={t.bold} wrap="truncate">{t.text}</Text>
    )
  return (
    <Box flexDirection="column">
      {rows.map(row => <Box flexDirection="row" gap={1} flexWrap="nowrap">{row.map(node)}</Box>)}
    </Box>
  )
}
