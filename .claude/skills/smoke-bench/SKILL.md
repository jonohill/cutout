---
name: smoke-bench
description: Run the cutout chapter-generation smoke bench across the configured contender LLMs, then act as the frontier-agent baseline/judge — produce per-file reference annotations, score each model on the subjective criteria and on recall, and write a leaderboard report. Use when asked to benchmark, compare, or smoke-test chapter models.
---

# cutout chapter smoke bench

You are the **frontier-agent baseline/judge**. The deterministic half (transcribe,
run each model through the production prompt, programmatic checks) is a Python
harness; your job is to run it and then grade the subjective criteria and recall
that code can't.

## 1. Run the harness

The bench lives in the `cutout/` Python project. Default config:
`cutout/bench/config.toml` (copy from `config.example.toml` if absent — tell the
user it needs filling in and stop). Pick a timestamped out dir.

```sh
cd cutout && uv run python -m cutout.bench --config bench/config.toml --out bench-results/<YYYY-MM-DD-HHMM>
```

Pass `--audio`/`--models` straight through if the user named a subset. Then read
`<out>/manifest.json` — it lists, per file: `transcript`, `n_segments`, and each
model's `chapters`/`raw`/`checks`/`error` paths.

If a model has a non-null `error`, that's a hard format failure (couldn't honour
the schema or the call failed) — record it as a fail; **do not** relax the schema
to help weak models, the failure is the signal.

## 2. Build the reference (per file)

For each file, read `transcript.json`'s segments. If `<file>/reference.json`
already exists, treat it as **human-verified** and reuse it. Otherwise produce it:

```json
{
  "topic_boundaries": [0, 65, 540, ...],
  "ad_spans": [{"start": 65, "end": 130, "note": "discrete ProductX read"}]
}
```

- `topic_boundaries`: whole-second times where the episode genuinely shifts topic
  (your independent read of where chapters *should* break). This is the
  denominator for segmentation recall.
- `ad_spans`: spans that are **ads under cutout's own definition** — apply it
  exactly:

  > Set is_ad true only for discrete advertising segments — uninterrupted sponsor
  > reads or pre-recorded ad spots whose sole purpose is to promote a product or
  > service. Do NOT count show intros, outros, or editorial content that merely
  > mentions or thanks a sponsor (e.g. "you're listening to X, brought to you by
  > Y"); these are part of the show even when a sponsor is named. A mix of
  > intro/outro and sponsor mention is editorial (not an ad).

Write `reference.json` into the file's dir. **Ad spans drive the highest-stakes
metric (they cut real audio), so call out in your report that the user should
spot-check `ad_spans` before trusting the ad-recall numbers.** You may use tools
and a second pass here — the reference is allowed to be more thorough than any
contender.

## 3. Judge each contender

Work from the transcript + that model's `chapters.<name>.json`. Judge **blind to
model identity** where you can. Per model, score:

- **title accuracy** — for each chapter, is the title a fair description of what's
  actually said in that span? Judge against the transcript content, **not** against
  any reference title (don't penalise equally-good wording). Report a rate +
  examples of misleading titles.
- **ad-marker precision** — for each chapter flagged `is_ad=true`, is that flag
  correct per the definition above? Report false positives (editorial wrongly cut).
- **segmentation recall** — how many `reference.topic_boundaries` did the model
  capture (a chapter boundary near each, within a few seconds)? Flags lumping
  (one giant chapter) and over-splitting.
- **ad recall** — how many `reference.ad_spans` did the model catch? Flags missed
  ads (the dangerous miss: ad left in).

The programmatic results in `checks.<name>.json` already cover format/schema,
coverage/tiling, copied-vs-hallucinated timestamps, brevity, and sanity — fold
them in, don't re-derive them.

## 4. Write the outputs

Into the out dir:

- `scores.json` — machine rollup: per model, the programmatic check summary plus
  your judged rates (title accuracy, ad precision, segmentation recall, ad recall).
- `report.md` — human-readable leaderboard: a per-model scorecard table, then
  notable failures with `file @ HH:MM:SS` citations, then a short recommendation.
  Lead with the ad-spans spot-check caveat from step 2.

Keep the report scannable — the user uses it to pick a `CHAPTERS_MODEL`, so make
the trade-offs (cheap-but-misses-ads vs accurate-but-verbose, etc.) explicit.
