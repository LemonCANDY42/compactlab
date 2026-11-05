# Does it work? A check on real logs

One developer's machine, Claude Code, Opus 5.5 and Sonnet 5.5 main sessions, the auto-compact window set to 650k
by hand. This is one case, not a benchmark; `compactlab share` is how more cases get collected
([SHARING.md](SHARING.md)).

## Result

Against what the same sessions would have cost at the harness default (about 967k), the logged list-price cost was
lower, by a wide margin, in the days after the change and not before it.

| Model | Before the change: logged vs default replay | After the change: logged vs default replay |
|---|---|---|
| Opus 5.5 | −0.5% | −24% |
| Sonnet 5.5 | −2% | −38% |

- The "before" column is the control. The setting was the default then, so logged and replayed cost should agree, and
  they do (within 2%). That is the evidence that the replay is accurate enough to be trusted for the "after" column.
- Both columns replay the same turns and the same context growth, so a busier or longer week does not read as a
  saving. This is what `verify` alone could not rule out: comparing "after" with all earlier days gave an interval
  that included zero.
- A replay at exactly 650k predicted a smaller saving than was logged, because compaction happened earlier than the
  setting alone explains (next section).
- About six days and a few dozen main sessions after the change. Costs are list prices, not an invoice. The saving
  does not include the wait during compactions or any loss of recall after them.

## What actually happened to compaction

After the change the automatic compactions came in two groups:

- **Near the setting (about 620–640k)**: reached while working, a minute or so after the previous turn.
- **Well below it (200–500k)**: every one of them came after the session had been idle for more than an hour
  (typically two to three). These are Claude Code compacting a long idle session, not the threshold.

So part of the saving is the setting and part is idle-session compaction. The logs cannot separate the two: the
idle compactions first appear in the same days as the setting (and around a Claude Code update), and the data has no
run with one but not the other.

## What this does not show

- That 650k is the best window for this user. The sweep recommends a lower one; nobody has tried it.
- That quality held. Lite-mode recall is an assumed curve; use `--mode deep` to measure it.
- That the saving will repeat on another machine or workload.

## How it was checked

1. `simulate` on the days before the change, to compare logged cost with the default-threshold replay (the control).
2. The same on the days after.
3. Each automatic compaction's size and the idle time before it, from the logs.

While doing this, three counting problems were found and fixed ([CHANGELOG](../CHANGELOG.md)): hand-typed `/compact`
counted as the threshold, compaction records re-written later in a log counted again, and history copied into a
resumed session counted twice. Earlier numbers from the tool, including its "already in effect" warnings, were
affected by them.
