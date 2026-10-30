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
field, any string outside a fixed vocabulary, or a non-finite number rejects the whole record rather than being
scrubbed. Dollar figures reveal scale (your spend per day), so skim them before you post; the file is plain JSON.

"Your current vs default" is the nearest thing to "what applying it saved": it compares the threshold you run
(read from your settings) with the harness default on the same history. It is still a counterfactual replay.

## Reading the pool

```bash
python scripts/aggregate_shares.py                # open issues labelled data-contribution (needs gh)
python scripts/aggregate_shares.py --dir records/ # or a folder of *.json
```

Every record is validated again, the newest record per author counts, and a "keep default" pick counts as no
saving. The output is medians per harness / model / role, with the count `n`. Treat small `n` as anecdote.

## What this cannot tell you

Self-selected, public, and the replay is the only source of "saved". It shows spread and replay error, not the true
effect of a change. The better check is local: after applying a threshold, `compactlab verify` compares the cost and
compactions the replay predicted with what the logs show since (cost only, not wall-clock). A later schema version
could carry its predicted *and* observed figures in a record.

## Why an issue form

No server, no keys and no new network path in the tool: GitHub already authenticates the author, keeps the
record public and editable, and runs the check. If volume or the "public" part becomes a problem, the next step is a
small endpoint that accepts only this schema; the schema (`compactlab.share/1`) would not change.
