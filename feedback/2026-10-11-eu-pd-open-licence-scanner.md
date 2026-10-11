# Field report: open-licence homepage scanner and live scans → eu-pd-composers (2026-10-11)

Orchestrator: Claude Opus 5.5 (session `52e13958`). Started after `/clear` from `assessments/HANDOFF.md`, item 1b step A: review and apply round 2 of the `homepage-licences` delegate, run the live scans, report hit rates. One delegate (cursor auto) ran three rounds across two sessions, with a Codex review after each. Round 3 and reviews 2–3 ran in this session. Machine-readable rows: [`token_usage.jsonl`](token_usage.jsonl), [`runs.jsonl`](runs.jsonl).

## Flow and cost

| # | Step | Outcome |
|---|---|---|
| 1 | Read the round-2 patch (1,393-line scanner) while Codex review 2 ran in the background | 2 gaps found by reading: transient errors cached forever, responses without Content-Type dropped |
| 2 | Review 2: 2 high, 6 medium | Round-3 brief: one decision per finding, plus the 2 gaps and a TSV-ingestion test |
| 3 | Round 3 (cursor, 4 min, 543 tests) | Core read again: an unreachable robots.txt was reported as `robots_disallowed` |
| 4 | Review 3 in the background while applying and fixing inline | 3 medium (1 already fixed inline); the other 2 fixed inline instead of a round 4; commit 79817d7, 562 tests |
| 5 | Live scans: 613 P856 homepages, then 575 pilot search URLs | 8 → 24 workers after the first minutes; 4m41s + ~3 min |
| 6 | Hand check of candidates, handoff refresh, report | 2 true catalogues (Gann, Lenot), 1 false (Dorman); pilot 0 of 49 → advised against the 4.9M-token full search |
| 7 | This log | — |

Session total up to this log: 58 calls, 5.68M cache read, 120k cache write, 38k output; context 37k → 145k.

The one-item rule held. The session started fresh from the handoff, stopped at the user's decision point, and the user clears after this log. Quota at the end: claude-7d 32% left (pace 1.2×), codex-7d 55 → 53%, cursor-auto 70% (pace 3.0×).

Delegate usage in this session:
- **Cursor round 3:** 40k input, 1.58M cached, 21k output, 4.0 min.
- **Codex reviews 2 and 3** (gpt-6.1-sol xhigh): 79k and 80k input, about 0.5M cached each, 15k and 10k output, 5.7 and 3.6 min.

## What worked

- **Reading the core while the review runs.** The orchestrator read the fetch, robots and cache code (~1,000 lines) while Codex review 2 ran. It found two gaps the review did not list, and both went into the next brief.
- **One decision per finding in the follow-up brief.** The round-3 brief restated each review finding as a concrete rule with a required regression test, e.g. "sha256 cache key, miss on URL mismatch; test `/a/b.html` against `/a_b.html`". Cursor implemented all 11 items in 4 minutes with every test green and no clarification loop.
- **Cross-vendor review caught bugs the unit tests could not.** Across three reviews Codex found:
  - a redirected robots.txt treated as permission;
  - cache-key collisions: `common.cache_path` maps punctuation to `_` and truncates at 180 characters;
  - (review 3) every real PDF would have been streamed, up to 3 MB, before rejection. The real `requests` adapter looked up `content-type` before header names were lower-cased.

  The tests injected `RawResponse` objects, which normalise their own headers, so the adapter path had no coverage.
- **The last small findings fixed inline instead of a round 4.** Review 3's findings were 1–10 line changes, and one was already fixed. Fixing them inline with tests took about 6 calls. A round 4 plus a re-review would have cost another round trip and about 0.6M Codex tokens.
- **A cheap live sample before the expensive run.** The 50-composer search pilot cost about 94k Codex tokens (previous session) plus a 3-minute scan. It found 0 hits among 49 random composers; the full run would have cost about 4.9M tokens and about 30k URLs.
- **A hand check before reporting counts.** Of 3 homepage candidates, 1 was false: a CC BY-SA credit for a map dataset in a page widget. The counts alone would have reported it as a hit.
- **Worker count raised mid-run without losing work.** The first run with 8 workers fetched about 40 pages a minute. A restart with 24 parallel hosts kept the per-host pacing at 1 request per second, reused the cache, and finished 613 homepages in 4m41s.

## Friction

1. **The orchestrator's prescribed fixes were wrong twice.** Review 3 flagged two "brief issues". Two round-3 rules fixed the reported example but failed nearby cases:
   - parse text windows forward from the match: `CC BY 4.0 … CC0` still merged into one licence;
   - always reuse `robots_disallowed` records: a robots.txt returning 503 blocked the page forever.

   **Lesson (SKILL.md, brief writing):** when a brief prescribes a mechanism, also state the invariant and one adversarial case it must survive. Examples: "two adjacent declarations of different licences both survive"; "a transient robots failure is never cached as a ban". The reviewer can then check the invariant, not just the mechanism.
2. **Tests through an injectable transport skip the adapter.** Three rounds of tests never touched `_default_get_once`, the function that talks to `requests`.
   - **Proposal (brief template for network code):** require one fake-session test of the real adapter, with mixed-case raw headers and a non-HTML body that must not be read, besides the injected-response tests.
3. **Waiting on a delegate needs a loop the harness accepts.** `sleep 60; tail` is blocked. A hand-written `until` loop exited early, because `delegate status` moves from `running` to `verifying` before `done`, so a second loop was needed for the verify step.
   - **Proposal:** `delegate wait RUN [--timeout]`, which blocks until the run is finished, including verify, and prints the status line. Run it with `run_in_background` for a single notification.
4. **Killing a chained background command.** Killing only the Python child let the `zsh -c` chain go on to the next scan, so the parent shell had to be killed first. The harness then reports the task as failed (exit 144), which is expected.
   - **Lesson:** kill the chain's shell first, then the child, or run each long command as its own background task.
5. **`delegate cost --since` takes a UTC date.** The run was applied at 01:09 local time, which is 23:09 UTC the day before. So `--since 2026-10-11` showed no runs, and per-round usage had to be read from `meta.json` by hand.
   - **Proposal:** accept a local date, or show per-round usage from `meta.json` in `delegate status --json`.
6. **Minor:** the test fake answers unmapped robots URLs with 404. The first regression test assumed a timeout and failed once; reading the fake before writing the test would have saved a run.
