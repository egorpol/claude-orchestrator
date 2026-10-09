# Field report: delegate skill on eu-pd-composers v3.2.0 (2026-10-09)

Orchestrator: Claude Opus 5.5 inside Cursor. Task: repo item #5 of a data project. The task was to verify IMSLP composer
matches against IMSLP's life dates, resolve category collisions, add a hand-reviewed overrides mechanism, and build two
dump revisions. Shipped as PR egorpol/eu-pd-composers#3 → v3.2.0. Machine-readable run data: [`runs.jsonl`](runs.jsonl).

## Setup

| Run | Agent / model | Job | Rounds | Wall time | Verify |
|---|---|---|---|---|---|
| `imslp-identity` | Codex gpt-6.1-sol @xhigh, workspace-write, `--link data/cache` | identity module + remap CLI + release gate + tests | 3 | 10.9 + 6.8 + 4.9 min | pass ×3 |
| `composer-overrides` | Cursor auto, sandboxed | overrides loader/applier + CLI + tests + data file | 3 | 3.9 + 4.0 + 1.9 min | pass ×3 |
| review of overrides | Codex gpt-6.1-sol @xhigh, read-only | cross-review | — | 4.7 min | — |
| review of identity | Cursor auto, `--mode ask` | cross-review | — | 12.1 min | — |

The two runs owned disjoint files and ran in parallel. Both patches applied cleanly (`apply --check`) on top of each
other, and the combined suite passed (274 tests).

Quota cost for the whole item (openusage): Codex 5h pool about −19 points, Codex 7d about −3, Cursor Auto about −1. Claude's
own 5h pool was the scarcest resource: the orchestrator wrote briefs, did all network fetching, reviewed and adjudicated.

## What worked

- **A real-data dry run inside `--verify` was the most valuable piece.** The semantic problems showed up in the dry-run
  reports, not in unit tests. They included my too-blunt date rule (it rejected Alan Bush over a wrong IMSLP death
  year) and wrong Wikidata P839 links that my brief declared trustworthy. Every brief should name the real-data command
  and its expected outcomes.
- **The contract's "flag conflicts with real data" clause works.** In round 1 Codex applied my rule but flagged
  Bush/Teixidor/Bracco as contradicted by their agreeing Wikipedia links. It did not silently change the rule and did
  not silently obey it. That flag led to the round-2 redesign.
- **Cross-vendor review found real bugs in both directions:**
  - Cursor (ask mode) found that 208 of 1,277 IMSLP pages use `{{wp|Title}}` biography links, which Codex's parser
    ignored. It found this by reading the cached data, which is impressive without shell access. Severity: high.
  - Codex found that the public apply function skipped the loader's validation (`_drop=false` would delete a row).
  - One finding from each review was wrong or out of scope: the "stamp `dump_date` on works" demand would have added a
    column. Adjudication is required, as the skill says.
- **Follow-up rounds resuming the same session were cheap and accurate.** Rounds 2 and 3 took 2–7 minutes, and each
  changed only what the brief named. Re-running the previous round's dry run and diffing the output proved that
  nothing else moved; Cursor's round 3 output was byte-identical, as asked.
- **Summaries were accurate this time.** Both agents' last messages matched the patches. I still verified every claim
  with my own dry run and diff.

## Friction and failures (mostly mine)

1. **Brief gaps caused rounds that a better brief would have avoided:**
   - The overrides brief re-keyed rows but did not say to re-derive the Wikidata fields from the new entity. That was
     my design oversight, and it cost round 2.
   - The identity brief's single "conflict → reject; P839 never demoted" rule was wrong for the data. Strong vs. weak
     conflict should have come from inspecting the data before writing the brief: 5 minutes of my own exploration
     would have saved a round.
   - "Set `dump_date` in both files" was ambiguous against the repo convention ("only if the column exists"). Codex
     implemented it literally, adding a column, and I only caught it by reading the patch. Cursor's reviewer, reading
     the same brief, flagged the *other* agent for following the convention.
   - **Proposal:** add brief-template sections for `<repo_conventions>` (how existing scripts behave; cite them) and
     `<schema_contract>` (columns that may be added or changed; everything else must round-trip byte-for-byte).
2. **Delegates have no network.** All fetching fell to the orchestrator: IMSLP composer pages, new Wikidata entities,
   pageviews. That worked, but it should be an explicit **prefetch phase** in the skill workflow, done before writing
   briefs. Without it, a delegate would discover mid-run that data is missing.
3. **`--link` can only be set at launch.** Round 2 of the Cursor run needed `data/cache`, so I symlinked it into the
   worktree by hand (the exclude was already in place). **Proposal:** `delegate followup NAME --link PATH`.
4. **Cursor ask-mode reviews are slow (12 min) and cannot run the verify command.** **Proposal:** put the latest
   `verify.log` and dry-run output into the review packet next to `review-N.patch`, so the reviewer can check claims
   against real output.
5. **Bookkeeping:**
   - `meta.json` reported `rounds: 3` but its `history` listed only rounds 1–2.
   - Codex's `tokens used` looks cumulative across a resumed session (102k → 147k → 182k), so per-round cost is
     unclear. **Proposal:** record per-round deltas in status.
6. **`route implement` picked `codex-astra`, but the user had pinned Sol and Cursor auto for this work.** **Proposal:**
   add a user-preference override in `catalog.json` (or read it from memory) so `route` reflects stated preferences.
7. **Orchestrator time went mostly into briefs and adjudication.** Briefs ran to about 1–2k words each. That is fine for
   data-semantics work, but it is where most of Claude's 5h pool went. For mechanical tasks, a shorter brief template
   would do.
8. My own error, not the skill's: a wrong date format in a pageviews call produced about 30 retried 400 requests.
   `request_json`-style retry loops should not retry on 400.

## Model notes

- **Codex gpt-6.1-sol @xhigh.**
  - Most careful on data semantics: it parsed nested template parameters correctly, checked each parsed year against
    the raw cache fields, and raised an explicit error on ambiguous P839 collisions rather than picking one.
  - Literal: it implements the brief as written, including its mistakes, but flags contradictions it can see in data.
  - Slower in round 1 (11 min) and writes extensive tests: 54 new tests in round 1, 102 by round 3.
  - Best for rule-heavy, data-dependent logic, and as a reviewer of other agents' validation logic.
- **Cursor auto.**
  - Fast (2–4 min per round) and cheap (about 1 point of the Auto pool for four runs).
  - Clean code, sensible defaults, and it disclosed unrequested design choices in its final message.
  - First-round weak spots: a slow per-cell restore loop and duplicated validation. Both were fixed on request in under
    4 minutes.
  - As a reviewer it found the highest-severity bug of the session, by checking the brief's claims against cached data.
  - Verdict: excellent for well-specified implementation and a strong reviewer. The user's positive experience matches.
    I would default to Cursor auto for implementation once the brief is precise, and use Codex Sol where the rules
    themselves are still being worked out against data.

## Suggested skill changes (priority order)

1. Brief template: add `<repo_conventions>`, `<schema_contract>`, and a mandatory real-data dry run with expected
   outcomes in `<verification_loop>`.
2. Workflow step 0: a "prefetch" phase (delegates have no network), plus "inspect the data yourself before writing
   rules".
3. `followup --link PATH`.
4. Review packet: include the last `verify.log` and dry-run output.
5. Status: per-round token deltas; fix `history` missing the last round.
6. `route`: honour pinned or preferred delegates.
7. On `apply`, append a run record (rounds, durations, verify codes, review verdicts, accepted/rejected findings) to a
   JSONL log automatically. `runs.jsonl` here was written by hand.

## Addendum: v3.3.0 automation round (same day)

- Four runs. One Codex run (gpt-6.1-sol @xhigh, 24 min) built the pipeline. Three Cursor auto runs (3–7 min each) built
  the work-evidence columns, the viewer and the refresh workflow. Three of the four ran in parallel; every patch
  applied cleanly.
- **A shared CLI contract in two briefs worked.** Cursor wrote the workflow against `pipeline.py` flags and a
  `REVISION=` output line that did not exist yet. Codex implemented exactly that contract. Only one detail
  mismatched, the staging-dir path, because the contract did not specify it.
- **One real end-to-end run by the orchestrator found the bug that no delegate or mocked test caught:** the viewer
  exporter hard-coded the data dir, so a staged run could not export. **Proposal:** after merging multi-delegate work,
  run one real integrated pass as a mandatory workflow step, not as an option.
- **An over-strict contract backfired.** "derive must not add columns" was meant as an idempotency check. Codex built a
  schema-preserving mode, so `derive` skipped the new columns by default, and I had to add a flag. **Lesson:** write the
  intent ("prove derive reproduces r012") and let the delegate pick the mechanism.
- **Good unprompted judgement from Cursor auto:**
  - a bounded `RoST` regex, which avoided a false positive on Rostropovich;
  - pure, exported JS helpers with node self-tests;
  - a `v=1` URL marker so a "cleared filters" link does not snap back to defaults.
- **A promise in docs was not enforced in code.** Both the refresh-workflow docs (written by Cursor) and the IMSLP note
  (written by me) said "≤ 1 request/s". `build_dump.py` actually paced IMSLP at 0.1–0.15 s. I only caught it while
  watching the first GitHub-runner refresh and cancelled it before it reached IMSLP. The fix enforces the cap in the one
  HTTP helper (`request_json`, per-host interval) so no path can bypass it. **Proposal:** before any outward-facing
  action (an email to a third party, a scheduled crawl), check every quantitative claim in the docs or brief against
  the code that will execute.
- **My "end-to-end" test used a warm cache, so it did not prove the cold path.** The first real runner refresh crawled for
  2 hours and then failed: the offline overrides step needs Wikidata entities that a cold crawl never fetches. The local
  e2e had them only because I had fetched them by hand earlier. **Proposals:**
  - e2e verification of a pipeline must use the same cache state as production (cold, if production is cold);
  - expensive pipelines should validate every input they will need offline as a *preflight*, before the long network
    phase.
- **Pool attribution correction from the user:** Grok models in Cursor (`grok-4.7-*`) draw on Cursor's **"Models" quota**,
  not the premium/API pool. SKILL.md and catalog.json currently say every named Cursor model bills premium. Grok should be
  routed as cheap and available. Verify the other named models before assuming premium.
