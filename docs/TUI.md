# Terminal views and `compactlab top`

All terminal drawing lives in `src/compactlab/term/` and uses only the standard library (`curses` and ANSI escape
codes). No network is used, and nothing here writes settings.

## When the views are used

| Output | When |
| --- | --- |
| Terminal view (boxes, colour, gauges) | stdout is a terminal, or `--color always` |
| Full plain tables (the previous output) | stdout is piped or redirected, or `--plain` |
| No colour, terminal layout | `--color never`, `NO_COLOR` set, or `TERM=dumb` |

Colour depth is detected: truecolor (`COLORTERM=truecolor`/`24bit`), 256 colours (`TERM=*256*`), else the 16 basic
colours. Layout is measured on display width (CJK and other wide glyphs take two columns) and fits 30 to 200
columns. The palette and glyphs follow the report and the lens mod: clay accent, green ▼ saves, amber ▲ adds, red
for a broken guardrail, grey for the default. Changes lead with their percent of the harness default (cost against
the default's window cost, time against its wall-clock); `--unit usd` leads with $ / day instead.

## Commands

- `demo`, `simulate`: a fastfetch-style header (history window, harnesses, models, sessions, turns, calibration,
  the change if the "set" models are applied), then a panel per model and role that needs a change:
  - a threshold gauge from the grid's low end to the window, with the default `┊`, the current setting `╎`, and the
    profiles `$` cost, `◆` balanced, `◷` time; `░` in red where the recall proxy is below the floor;
  - the answer line (`450k  ▼ −45% cost  ▼ −15% time  −$44.9 · −12.3 min per day vs default 967k`);
  - was / now bars for cost and time (grey was, accent now, green `░` saved, amber added);
  - a sparkline of the cost over thresholds with the profile's cell highlighted;
  - the three profiles with their action (`[set]`, `[keep default]`, `[no change]`).
  Models that never compact are listed on one line, then the recommendations table with keep-default reasons.
- `recommend`: the snippets (plain, ready to paste), then the recommendations table.
- `doctor`: the header plus a panel per harness with ✓ / ✗ per CLI, log root and config file.
- `serve`: a banner with the report URL, the token path, the next calibration and the auto-apply state. The service
  also writes `~/.compactlab/service/endpoint.json` (loopback address and port) for `top`, removed on exit.

## `compactlab top`

```bash
compactlab top                 # the latest result: the service's, ./compactlab-result.json, ~/.compactlab
compactlab top --demo          # synthetic data
compactlab top --once          # one frame and exit (also what happens when stdout is not a terminal)
```

Layout: a title row; meters (total change if the "set" models are applied, calibration age and next run, service
up/down, the profile shown); the tree (harness > model and role, a was/now mini bar and the change); the selected
model's detail (the answer line, a braille chart of cost and time as % of the default over the threshold with the
default / current / profile marks, was/now bars, the three profiles). Under 80 columns the tree sits above the
detail.

| Key | Does |
| --- | --- |
| ↑ ↓, j k, mouse wheel | select |
| ← → , h l, Enter | fold / expand a harness |
| p | cycle the profile: cost, balanced, time, each model's own |
| % | lead with % of the default or $ per day |
| r | recalibrate: the running service if its endpoint answers (with its token), else an in-process lite run |
| c | copy the selected model's setting to the clipboard (pbcopy, wl-copy or xclip; otherwise it is shown) |
| / | filter models; Enter keeps the filter, Esc clears it |
| ? | key help; any key closes it |
| q | quit |
| click | select a row in the tree |

Options: `--result`, `--demo`, `--profile`, `--interval` (seconds between reloads of the result file and the service
state, default 5), `--once`, `--width` / `--height` (for `--once`), `--state-dir`.

## Design: a pure frame

`top.frame(state, cols, rows)` returns the whole screen as lines of `(text, role)` segments and
`top.handle(state, key)` updates the state; the curses loop only draws and reads keys. The same frame is printed by
`--once`, captured for `docs/assets/top.svg`, and tested without a terminal (`tests/test_term.py`, with golden
snapshots of the widgets in `tests/golden/`; `UPDATE_GOLDEN=1` regenerates them).

## Assets

`scripts/make_assets.py` runs the real CLI with `--color always` at a fixed width, converts the ANSI output to SVG
(each run of non-blank text placed at its own column, x = column × cell width, and stretched to its cell width,
so spacing never depends on whitespace handling; text is visible by default and the fade-in is additive, so
renderers without CSS animation such as rsvg-convert and Quick Look show the whole frame) and writes `docs/assets/demo.svg` and `docs/assets/top.svg`. Synthetic data only.
