# Sharing estimated savings (opt-in)

compactlab's savings are replay estimates at list prices on one person's history. Pooling anonymous records from many
people shows how far they vary by model, role and history length, whether the replay tracks the logged cost, and how
often a profile ends in "keep default". Nothing is collected automatically: `compactlab share` writes a file on your
machine, you read it, and you post it yourself if you want to.

## How

```bash
compactlab simulate --out result.json
compactlab share --result result.json      # writes compactlab-share.json; --copy puts it on the macOS clipboard
```

Then open the **Share my estimated savings** issue form, paste the file and submit. A workflow checks the record
and labels the issue `valid-record` or `invalid-record` with the reason. Edit the issue to fix a rejected record.
`compactlab share --check FILE` runs the same check on your machine first. Synthetic `demo` results are refused.

## What is in a record

Per harness / model / role group, with figures rounded to 2–3 significant digits:

| Field group | Content |
| --- | --- |
| Identity of the setting | harness, public model name (`other` for a name not in the bundled price table), role |
| Size of the history | sessions, turns, days, active days, observed compactions (rounded) |
| The settings compared | model window, harness default threshold, your current threshold, each profile's threshold, action and flags |
| Estimated change | per day, for each profile **vs the harness default** and **vs your current setting**, and your **current setting vs the default**: dollars, percent of cost, wall-clock minutes, added compaction wait, compactions, recall delta |
| Trust markers | cache TTL and where it came from, whether the time model used your own samples, pooled or prior data, whether penalties were observed, replay cost over logged cost |

Not in a record, by construction: session ids, file or project paths, timestamps and dates, context curves,
histograms, transcript text, settings file contents, user or machine names. The checker is an allow-list: any extra
field, any string outside a fixed vocabulary, a number with more digits than `share` writes, a repeated JSON field, a
repeated group or a non-finite number rejects the whole record rather than being scrubbed. A rejection message
never repeats what was submitted. Dollar figures reveal scale (your spend per day), so skim them before you post; the file is plain JSON.

"Your current vs default" is the nearest thing to "what applying it saved": it compares the threshold you run
(read from your settings) with the harness default on the same history. It is still a counterfactual replay.

## Adding what happened afterwards

```bash
compactlab share --result result.json --verify     # needs a change made with compactlab apply
```

`--verify` runs `compactlab verify` on your logs and adds a `verification` block to each group it could
check: the old and new threshold, how many sessions and active days it had on each side, the verdict, whether
compactions really landed near the threshold the harness honours, and three pairs of figures: the change in cost
per 1k turns (percent) that the replay predicted, that the logs showed (with an 80% interval) and that a replay of
the later sessions gives, and the same three for the change in compactions per 1k turns. These are changes, not
absolute costs. Rounded like the rest; no dates. This is the schema `compactlab.share/2`; the checker still accepts
`/1` records. The pool then shows, per model and role, the median prediction error in percentage points and how
many applied settings were not in effect.

## Reading the pool

```bash
python scripts/aggregate_shares.py                # open issues labelled data-contribution (needs gh)
python scripts/aggregate_shares.py --dir records/ # or a folder of *.json
```

Every record is validated again, the newest record per author counts, and a "keep default" pick counts as no
saving. The output is medians per harness / model / role, with the count `n`. Treat small `n` as anecdote.

## What this cannot tell you

Self-selected and public. Without `--verify` the replay is the only source of "saved", so the pool shows spread and
replay error, not the true effect of a change. With it, a record carries logged cost per 1k turns before and after a
real change, which is still observational: the work you did may differ between the two periods, which is what the
interval and the replay-on-later-sessions figure are there to show. Cost and compactions only, not wall-clock.

## Why an issue form

No server, no keys and no new network path in the tool: GitHub already authenticates the author, keeps the
record public and editable, and runs the check. If volume or the "public" part becomes a problem, the next step is a
small endpoint that accepts only this schema; the schema (`compactlab.share/1`) would not change.
