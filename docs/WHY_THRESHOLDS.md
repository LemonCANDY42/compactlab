# Why compaction thresholds matter

Agent harnesses re-send the whole conversation on every request. With prompt caching, most of that prefix is
billed at the cache-read rate, which is cheap per token but paid **on every turn for every token already in
context**. Three effects pull the auto-compact threshold in different directions.

## 1. Cache reads dominate long sessions (pushes the threshold down)

If a session adds roughly `g` tokens per turn, the prefix read on turn `n` is about `base + n·g`, so the total read
volume grows with the square of session length. In long sessions this term, not output, is usually most of the
bill. Compacting earlier caps the prefix. Each compaction is not free, though: it costs a summarizer call that
reads the whole context, a summary write, and a full cache rewrite of the new, shorter prefix.

## 2. Long contexts degrade (pushes the threshold down, gently)

Chroma's *Context Rot* report ([trychroma.com/research/context-rot](https://trychroma.com/research/context-rot),
code at [chroma-core/context-rot](https://github.com/chroma-core/context-rot)) found that model reliability drops as
input length grows, even on deliberately simple tasks, across the 18 models it tested. How fast it drops depends
on the model and the task. compactlab does not have measured curves for current models; its lite-mode recall is
an assumed shape you can replace.

## 3. Summaries lose information (pushes the threshold up)

A compaction replaces detailed history with a summary. Facts that did not make it in must be re-discovered
(re-reading files, re-running searches) or are lost, which shows up as user corrections ("I already told you...").
Summaries can also state things that were never verified ("all tests pass"). Fewer compactions mean fewer of
these losses. Each compaction also costs wall-clock: in real logs a compaction of a large context takes minutes,
not seconds. That is why compactlab's default `balanced` profile prices your time at $250/h and never goes below
40% of the window, which puts it well above the cost-only optimum.

## Putting it together

The best threshold is where the marginal saving from a smaller prefix stops paying for the extra compactions and
their re-discovery cost, subject to a floor on how much you are willing to lose. That point depends on how your
sessions grow, how often you pause (cache TTL expiry turns reads into writes), your model's prices, and how
lossy its summaries are. A single global default cannot know these; your history can.

Related: LangWatch's case study on optimal context windows for coding agents
([langwatch.ai/research/finding-the-optimal-context-window](https://langwatch.ai/research/finding-the-optimal-context-window))
measures this trade-off on its own agents. compactlab has not reproduced its numbers.
