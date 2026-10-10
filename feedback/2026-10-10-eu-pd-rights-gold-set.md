# Field report: IMSLP rights evidence (r016), gold-set tooling, merge → eu-pd-composers (2026-10-10)

Orchestrator: Claude Opus 5.5 (session `48800fa0`). Started from `assessments/HANDOFF.md` (Phase 1 of the paper plan); the user then added two items ("ok go on", "PR and merge"). No delegates ran. Machine-readable rows: [`token_usage.jsonl`](token_usage.jsonl), [`runs.jsonl`](runs.jsonl).

## Flow and cost

| # | Item | Calls | Cache read | Context at end | Outcome |
|---|---|---:|---:|---:|---|
| 1 | Phase 1: map 6 IMSLP rights categories, derive `has_files` + new `imslp_file_hosts` from the file cache, gates, viewer badges, dump r016, rights-basis analysis script | 79 | 9.9M | 205k | 412 tests; r016 gates OK; diff reviewed; docs + CHANGELOG; 2 commits pushed |
| 2 | Gold-set sampler, annotation guide, scorer; r016 sample drawn | 15 | 3.5M | 256k | 422 tests; 1 commit, new branch pushed |
| 3 | PR and merge four stacked branches | 8 | 2.1M | 267k | PRs #10–#13 opened, CI green, merged in order with retargeting |
| 4 | This usage dump | ≈ 8 | ≈ 2M | 277k | — |

Session total: 109 calls, 17.4M cache read, 250k cache write, 125k output; context 36k → 277k. Quota: claude-7d 51 → 48%; codex (56%) and cursor-auto (71%, pace 2.8×) untouched.

Items 2–4 ran at 205–277k context: about 7.6M cache read where fresh sessions (handoff read ≈ 36k, growing to ≈ 80k) would have used roughly 1.5M. That is ≈ 35% of the session, the "one item per session" lever measured a third time (`fc63a0c8` 6.3M single item; `e06990b8` 14.0M for four items; this one 17.4M for three plus the dump).

## What worked

- **The diff review caught out-of-scope drift before anything shipped.** The prescribed `pipeline.py derive` changed 12 composer cells that the item never touched. Checking the cache-file dates showed stale local Wikidata and IMSLP caches (2026-09-24, older than the r014 crawl). r016 was rebuilt as a single-stage replay, the same route r015 took. A second look at the new meta caught an inherited absolute path (`/home/…` ledger path) that the cleanup branch had just removed.
- **Profile the data before coding the rule.** One scratch script over 29k cached file lists (extensions, server tokens, prefixes) fixed the `has_files` rule (any non-image file) and the host mapping (`PMLP` nested token = main server) before any code was written. Counts matched the plan's estimates.
- **Network calls only with explicit consent, batched into one question.** The remaining 63 uncached pages (2 requests) were offered together with the commit/push choice in a single `AskUserQuestion`. Both were approved and done in minutes.
- **Stacked PRs merged cleanly.** Open all four with stacked bases, wait for CI on all in one blocking `gh pr checks --watch` loop (≈ 3 min), then `gh pr edit --base main` + `gh pr merge --merge` in order. No conflicts, no re-runs.
- **Blindness designed into the gold set.** Sheets carry links only; strata and weights live in separate design files; item ids are assigned after shuffling. The handoff tells the next session not to look up dump values while guiding annotation.

## Friction

1. **Session sprawl again (third session in a row).** After item 1, at 205k context, "ok go on" started item 2 in place. The context guard only fired at ≈ 256k, during item 3.
   - **Proposal (SKILL.md, CLAUDE.md):** treat any new item above ≈ 120k as a handoff point. Reply with the handoff and a one-line `/clear` offer *before* starting, even when the user says "go on". The guard threshold (250k) is too late to help; consider a first reminder at ≈ 150k.
2. **No delegation, though cursor-auto had 71% left at 2.8× pace.** Mechanical parts fitted a delegate: five doc files updated to r016, the export and viewer-badge tests, the gold-set unit tests from a written spec.
   - **Proposal:** when `delegate quota` shows a pool with pace > 2×, route tests-from-spec and multi-file doc refreshes there by default, and keep the orchestrator on design, data checks and review.
3. **`derive` is not reproducible when local caches lag a refresh crawl.** The refresh ran with a separate cache directory that no longer exists, so the default `data/cache/` silently lags it.
   - **Proposal (project-side, logged in its handoff):** the refresh could sync its cache back into `data/cache/`, or `derive` could warn when a cache entry is older than the source revision's crawl date.
4. **Small slips.**
   - A float comparison in a new release gate (`1.0 − 0.95 > 0.05`) failed its own boundary test; fixed with rounding.
   - A shell heredoc nesting trick (`python - <<EOF || python3 - <<EOF2`) swallowed a script silently. Write scratch scripts to files.
