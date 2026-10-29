# compactlab-lens (Claude Code mod)

An independent open-source mod for Claude Code, part of compactlab. It is not made, endorsed or supported by
Anthropic, and it is not affiliated with any other mod.

It shows, for the session you are in, where its context stands against two auto-compact thresholds: the harness
default and the one compactlab picked for your model and role (profile `balanced` unless you choose another). It also
shows what `compactlab simulate` estimated that choice saves or adds per day.

## What it draws

**Terminal: a band above the prompt, laid out for the columns it has.** Rows break only between pieces, never
inside a number and its unit. Wide or CJK glyphs count as two columns. From 60 columns the band is one row with full
units. From 40 it is one row in a compact form (`▼−$205/d`, a shorter bar) when that fits. Below that, row one holds
the bar and the context, and the chips follow.

120 or 80 columns:

```
▸ ●●●●●●●●●●·┃··········┊· 412k ┊967k ┃500k ▼−$204.8/day ▲+4.8m/day ⟳0 ◴2.0h ⚠
```

55 columns:

```
▸ ●●┃··┊ 412k ┊967k ┃500k ▼−$205/d ▲+4.8m/d ⟳0 ◴2.0h ⚠
```

30 columns:

```
▸ ●●●●●●●●●··┃·········┊· 412k
┊967k ┃500k ▼−$205/d ▲+4.8m/d
⟳0 ◴2.0h ⚠
```

| Glyph | Meaning |
| --- | --- |
| `●●●·` | the context in use, as a share of the model's window |
| `┊967k` | the harness default threshold (dim tick) |
| `┃500k` | the profile's threshold (accent tick) |
| `▼−$204.8/day` | money per calendar day vs the default; green ▼ saves, amber ▲ adds |
| `▲+4.8m/day` | wall-clock minutes per day vs the default |
| `⟳0` | compactions so far in this session |
| `◴2.0h` | age of the calibration; amber once older than twice the interval |
| `⚠` | the auto-compact window in force differs from the profile's threshold |
| `ⓘ` | no compactlab figures for this model yet: run `compactlab simulate` |

`▸` unfolds the card (here at 40 columns):

```
▾ ●●●●●●●●●●·┃··········┊· 412k
┊967k ┃500k ▼−$205/d ▲+4.8m/d ⟳0 ◴2.0h
⚠
▄ context per turn
default 967k ▼−$205/d ◷▲+4.8m/d
wait +6.8m turns −2.0m ⟳3.0/d
current 400k ▲+$1/d ◷▼−0.3m/d wait +1.7m
turns −2.0m ⟳3.0/d
cost 200k [ balanced 500k ] time 575k
[ ⧉ /autocompact 500k ]
⚠ live 967k ≠ balanced 500k
vs harness default ·
list-price estimate ·
recall is a lite-mode proxy
```

The figures are from the mod's test fixture, not from anyone's history; the tests check every one of these widths
(30, 40, 55, 80 and 120 columns, in English and Chinese, folded and unfolded): no row wider than the band, no number
cut from its unit.

**Desktop: a pane** (opened at session start, or with `/compactlab-lens` anywhere). It has the same rows, packed to
the pane's width, and draws the context curve as a small SVG with the default (dashed) and the profile (accent) as horizontal lines. Labels use
one language, following `LANG`/`LC_ALL` or the `language` option (`auto`, `en`, `zh`).

The profile buttons change only what the card shows. `⧉ /autocompact 500k` copies that command to the clipboard;
you paste it yourself. If `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is set, it wins over `/autocompact`, and the warning
line says so.

## What it reads, and what it never does

- **Reads:** one local file, `~/.compactlab/lens.json`, or the path in the `lensPath` option. A full `result.json`
  also works if it is under 4 MiB. `compactlab simulate` writes `lens.json` by default (`--lens-out`, `''` to skip):
  thresholds and per-day impact per model and role, with no session ids, paths or transcript text.
- **Reads from the engine:** the session's model, its context tokens and window, and the compaction window in force.
  These come from `$.session.usage({ breakdown: 'summary' })`, which estimates locally and sends no request. It also
  counts compactions of the main conversation.
- **Never:** writes settings or files, calls a model, uses the network, or starts a process. It adds nothing to the
  conversation, so it costs no model tokens.

## Install

At the prompt of a Claude Code terminal session (once the repository is published):

```
/plugin install compactlab-lens --marketplace <owner>/compactlab
```

To try it from a checkout:

```bash
claude --plugin-dir ./mods/compactlab-lens
```

Options (in `/config` once installed): `lensPath`, `profile` (`cost` / `balanced` / `time`), `language`.

## Developing

```bash
claude plugin validate mods/compactlab-lens
claude plugin test mods/compactlab-lens
```

The pure helpers (`hooks/lens.ts`) hold the formatting, the dot bar, the sparkline and the lens-file parsing. The hooks
module (`hooks/register.tsx`) holds the band, the pane and the events. The state contract is `types/index.d.ts`.
