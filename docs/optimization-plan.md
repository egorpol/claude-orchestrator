# Optimisation plan (2026-10-10)

Based on the 2026-10-09 eu-pd-composers session: 13 hours, 16 delegate runs, five releases. Sources:

- [`feedback/`](../feedback/): the field report, the token-usage report, and the run and usage JSONL files.
- **Transcript analysis** by Codex gpt-6.1-sol @xhigh, read-only, two rounds (run record `research-orch-cost`). It reconciled the transcript exactly against openusage: 164.59M cache read, 2.56M cache write, 398k output.
- **Design audit** of `delegate` against the reports by Cursor auto (`research-skill-design`).
- **Native instruction-file test** and a `codex exec --json` test, run by hand.

All percentages are shares of the orchestrator session's API-equivalent cost of about $54. Relative prices: input 1, cache read 0.1, 1h cache write 2, output 5.

## Status (2026-10-10)

Implemented and tested: 31 unittest cases with fake agents, plus one real Cursor run on a toy repo whose task notification carried the exit packet.

| lever | state |
|---|---|
| 1 sessions | **done**: SKILL.md "Sessions" section; `apply` prints a handoff nudge; `hooks/context-guard.py` is installed here (`./install.sh --hooks` elsewhere) and fired live at 304k |
| 2 exit packet | **done**: `run.sh` → `_finalize` prints at most 20 lines into the notification; compact `status NAME`, `--full` |
| 3 batching | **done**: SKILL.md guidance |
| 4 heavy reads | **done**: `delegate ask` (Cursor auto, about 10s); risk-based review policy (user decision) |
| 5 fewer rounds | **done**: `templates/`; `AGENTS.md` guidance; prefetch, integrated e2e and docs-claims steps |
| 6 measurement | **done**: Codex `--json` per-round usage; history bug fixed (plus backfill for old runs); `runs.jsonl` on `apply`/`--purge`; `delegate cost` |
| 7 routing and CLI | **done**: `prefer` / `--prefer`, `cursor-grok` (quota unknown), `followup --link`, verify log in the review packet, `verify`/`diff` without a worktree |
| 8 idle rebuilds (2026-10-12) | **done**: context guard holds the first typed prompt after 60 min idle at 100k+ once; `cost --session` lists rebuilds |

Also fixed along the way:
- `.pyc` files from test runs leaked into patches (`PYTHONDONTWRITEBYTECODE=1`);
- `ask --files` swallowed the question;
- `exit_code` was written only after verify (there is now a "verifying" state);
- `status --full` crashed on review jobs.

### Field results (2026-10-10)

Lever 1 held up. Measured with `delegate cost --session`; the work differs between sessions, so compare per call, not in total.

| session | work items | calls | context at end | cost units | units per call | Claude 7d (shared pool) |
|---|---|---:|---:|---:|---:|---|
| 762dc86f, eu-pd-composers marathon | 7 | 389 | 779k | 26.2M | 67k | 84 → 67 |
| 55a11d16, LLM style pass | 1 (from `HANDOFF.md`) | 88 | 158k | 1.9M | 22k | 60 → 58 |
| 9a59a1f6, audiospylt | 3 (assessment, plan, Phase 0) | 104 | 334k | 3.8M | 37k | 57 → 54 |
| fc63a0c8, eu-pd-composers v3.5.0 | 1 (from `HANDOFF.md`) | 67 | 162k | 1.2M | 19k | 58 → 54 |
| e06990b8, cache fix, cleanup, IMSLP rights | 4 | 103 | 251k | 2.5M | 24k | 54 → 51 |
| 48800fa0, rights evidence r016, gold set, merge | 3 | 112 | 283k | 3.0M | 27k | 51 → 48 |
| 96cb4576, gold-set form, annotation support, plan | 10 | 116 | 286k | 3.4M | 29k | 48 → 45 |
| 4994250f, identity guard, r017 | 1 large + 2 short | 141 | 290k | 4.1M | 29k | 45 → 41 |

One-item sessions cost 19–22k units per call, 68–72% below the marathon; the model predicted about 60%. The three-item audiospylt session sits in between at 37k: its last ~20 calls each re-read ~300k. Its report notes that a handoff after the plan would have kept Phase 0 near 40k context.

New findings from 55a11d16:
- **Idle cache rebuilds.** Two wake-ups after 4.4h and 6.1h waits each re-wrote the whole context, about 120k tokens at 2×. Together they were 26% of the session's cost. SKILL.md now says to hand off before a multi-hour wait.
- **Cursor pools resolved.** openusage `plan_auto_percent_used` = (Auto + Grok + Composer spend) / $450, exact at two snapshots. So "Cursor Models" shares the Auto pool, and `cursor-grok` now routes on `cursor-auto` with real quota. Named Claude/GPT models bill `plan_api` ($22.50).
- **Bulk labelling** (6.5k rows, outside `delegate`): grok-4.7-low used about 2.7 points of Cursor Auto; gpt-6.1-sol@low used about 15 points of Codex 7d. Both had zero failures.
- **Lessons now in SKILL.md:** read-only is not blind (isolate the answer key), per-chunk quota gates, PID files for detached drivers.
- **Thinking share was unchanged** at about 60% of output under xhigh, which is consistent with the effort analysis in lever 4.

New findings from 9a59a1f6 and fc63a0c8, and what changed:
- **CRLF patches never applied: our bug, now fixed.** `make_patch` read `git diff` with `text=True`, and universal newlines turned every `\r\n` into `\n`. Patches and review diffs are now handled as bytes; a lifecycle test covers a CRLF base. The proposed file-copy fallback and launch warning are not needed.
- **`runs.jsonl` saw 1 of 5 runs.** Read-only, hand-merged and discarded runs are now logged once at `cleanup`, with their reviews.
- **Codex usage of resumed rounds was cumulative.** `turn.completed.usage` is the thread total. A round now records its own delta as `usage` and the raw total as `usage_thread`, and the exit packet shows the delta.
- **Reviews approved wrong briefs twice** (a physically wrong Σamp² criterion; a `dump_date` convention break). The review prompt now asks the reviewer to challenge the brief's criteria against the domain and repo conventions. The implement template names convention-stamped columns, and SKILL.md tells the orchestrator to check domain criteria before launch.
- **SKILL.md and templates:** hand off after an assessment or plan; measured numbers per option in decision questions; one bundled approval for outward git steps in auto mode; Jupyter needs `ipython` or `--verify` under Codex; clean up only paths the session created; zsh word splitting; research scratch under `.delegate/`.

### Field results, evening of 2026-10-10 (four eu-pd-composers sessions)

All four started from a handoff, and all four then sprawled. The user added items mid-session ("ok go on", "sure go on"), and the orchestrator started them in place.

| session | prompts that started a new item, at context |
|---|---|
| e06990b8 | 87k, 180k |
| 48800fa0 | 205k, 256k |
| 96cb4576 | 145k, 201k, then 7 support questions from 215k |
| 4994250f | 219k (mid-item), 275k |

- **The guard fired too late, but it works.** At 250k it fired only in three sessions, and each time after the boundaries had passed. Each time it fired, the session wrote a handoff. Clearing at every prompt above 120k (simulated on the transcripts, re-reads only, an upper bound) would have saved 20%, 18%, 33% and 21%. The hook now fires from 120k, then every 60k. Its text asks Claude to decide whether the prompt starts a new item, and if so to hand off and offer `/clear` before starting, even after "go on". Tested in `tests/test_context_guard.py`.
- **SKILL.md never reached these sessions.** None of the four invoked `delegate`, so its session and approval guidance went unread, and the zsh slip recurred. Two fixes:
  - `rules/orchestrator.md`, imported into the user-level `CLAUDE.md` by `./install.sh --rules`, carries the rules that apply everywhere;
  - the skill description now asks for unprompted use on mechanical work with a clear spec.
- **No delegation at all.** Claude 7d fell from 54% to 41% over the four sessions while Cursor Auto stayed at 71% (2.9× pace). Doc refreshes across five files, tests from a written spec and a near-copy fetcher were all delegable.
- **Smaller items:**
  - `status` flags an applied run whose worktree is over a day old;
  - `apply` ends by suggesting `cleanup`;
  - SKILL.md says a merge listed in a handoff is not approval, to start long network jobs first, to falsify a hypothesis with one cheap sample, and to check every changed headline value against a source.
- **Not in this repo's scope:**
  - Artifact lessons: take a headless-Firefox screenshot before the first publish, and read the `.d.ts` before writing capability code.
  - The project-side cache-lag bug in eu-pd-composers.

### Field results, 2026-10-11 (three handoff sessions)

All three started after `/clear` from a `HANDOFF.md` and ended at the next decision point. Numbers from `delegate cost --session`, which includes the calls that wrote each report, so they run a few calls above the reports' own figures.

| session | work | delegates | calls | context at end | cost units | units per call | Claude 7d |
|---|---|---|---:|---:|---:|---:|---|
| 52e13958, eu-pd open-licence scanner + live scans | 1 item | Cursor round 3, Codex reviews 2–3 | 66 | 160k | 1.21M | 18.3k | → 32 |
| a79562ac, eu-pd open-licence axis for r020 | 1 item (merge, pilot, feature) | Cursor 2 rounds, 1 Codex review | 65 | 158k | 1.15M | 17.7k | 32 → 29 |
| 7272c8d9, Webern Op. 27 MEI 5.1 + docs | 3 small items | none | 71 | 183k | 1.48M | 20.9k | → 26 |

- **Lever 1 held.** 17.7–20.9k units per call is the lowest yet: the one-item sessions of 2026-10-10 ran 19–22k, the sprawling evening ones 24–29k. The rules file reached these sessions; two reports cite it.
- **The 120k guard fired mid-item once** (a79562ac, ~121k). The prompt continued the item, so the session carried on and handed off at 144k. That is the intended reading of the guard text.
- **Webern ran three items to 183k.** The docs item started at ~70k, under the threshold, and read and wrote about 55 KB. The rules now say to hand off below 120k when the next item is large.
- **Delegation improved, but Claude 7d still fell about 3 points a session** (32 → 26 over the three). Cursor Auto built a 15-file feature in 2 rounds with 0 review findings and no visible pool change (70 → 70; 68 at the end of the batch).
- **What worked, now in SKILL.md and the templates:**
  - one batched question for all of a handoff's open decisions;
  - a stop rule agreed before a pilot (0 of 49 ended a 4.9M-token search);
  - expected dry-run numbers computed by the orchestrator on a separate path; Cursor matched them exactly;
  - reading the core while the cross-review runs;
  - fixing the last review's 1–10 line findings inline instead of a fourth round.
- **Friction, and what changed:**
  - Two prescribed fixes handled the reported example and failed the adjacent case. Briefs that prescribe a mechanism now state the invariant and one adversarial case; the review template checks invariants against adjacent cases.
  - Injected-response tests never touched the real `requests` adapter, where a header-case bug sat. The implement template asks network code for one fake-session test of the adapter.
  - A hand-written wait loop exited at `verifying`. New `delegate wait RUN`, run in the background, blocks until the round and its reviews have finished, including verify.
  - `cost --since` took a UTC date, so a run applied at 01:09 local time fell on the previous day. It now takes a local date.
  - Rules: a local commit is not approved either; name the check that passed rather than "validates"; carry qualifying notes into every doc; use fictional IDs in doc examples; zsh `=word`, unmatched globs, `cd` in a call, and killing chained commands.
  - This session itself: "continue from the handoff" found no `HANDOFF.md` here, because the previous session had pointed at this plan instead. Six calls went into reading transcripts. The rules now say to always write the file.
- **Not in this repo's scope:** lxml cannot compile MEI's XPath 2 Schematron (CAMAT uses `saxonche`); the eu-pd scanner's test fake answers unmapped robots URLs with 404.

### Field results, 2026-10-11/12 (three handoff sessions)

All three started after `/clear` from a `HANDOFF.md`. Numbers from `delegate cost --session`, including the calls that wrote each report.

| session | work | delegates | calls | context at end | cost units | units per call | idle rebuild | Claude 7d |
|---|---|---|---:|---:|---:|---:|---|---|
| 9b03e065, Webern MEI edition headers | 1 item, short Q&A | none | 67 | 192k | 1.78M | 26.6k | 91 min, 141k written: 15.9% | 26 → 24 |
| 12c68dfa, Webern breaks, then proofreading Q&A | 1 item, ~6 questions | none | 129 | 223k | 3.04M | 23.5k | 464 min, 184k: 12.1% | 24 → 20 |
| 032191ec, table grid, site audit, eu-pd note | 3 items | Codex ×3, Cursor ×1 | 80 | 175k | 1.50M | 18.8k | 102 min, 68k: 9.0% | 11 → 10 |

- **Idle cache rebuilds are the second-largest lever: 12.7% of all logged orchestrator cost.** Each of the three sessions had one: the user came back after a break, and the first prompt rewrote the whole context at 2×. Without it, units per call were 22.4k, 20.7k and 17.1k, in line with the one-item sessions. A scan of all 14 sessions in `token_usage.jsonl` found rebuilds in 7 of them, 7–26% of each, 7.2M of 56.3M units in total. See lever 8.
- **Q&A after a finished item is still where sessions sprawl.** 12c68dfa answered six proofreading questions after its build was pushed and ran on to 223k. The guard fired at ~125k and ~204k, and `/clear` was offered four times as a closing sentence. The rules and SKILL.md now say to make it an option in the picker that closes the first answer.
- **A six-line delegation cost more than doing it inline:** about 8 orchestrator calls against about 3. The rules and SKILL.md now keep changes under ~10 lines inline unless the user asks to delegate.
- **`delegate wait` worked as built** (0ecc1c8): two detached read-only audits ran while the note was researched and written, with one wait and no polling.
- **Nothing was delegated in the two Webern sessions.** Both were design and data items, and the break placement needed judgement. Claude 7d fell from 26% to 20% over them while Cursor Auto kept 2.8× spare pace; the mechanical follow-ups are queued in that project's handoff. Claude 7d then fell from 20% to 11% before 032191ec began, in sessions with no report.
- **Lessons now in the rules:** never write an authority identifier from memory (a remembered GND for Webern was wrong); dates inherited from a Kern-to-MEI conversion are claims to check; a sibling project's renderer claim did not hold, so probe such claims and keep the minimal example next to any you write; check public claims against what is deployed, not the checkout; tally categories by exact value (a substring test counted 252 instead of 65); show the first check and ask before widening a test matrix; `pkill -f` can kill its own shell.
- **SKILL.md and templates:** visual checks use orchestrator-made screenshots handed to every reviewer, because Codex's headless Firefox crashed in its sandbox; research briefs for public writing ask for a privacy-flags section and a second count per headline number, capped at about 15 KB.
- **Not in this repo's scope:** a dark-mode `user.js` pref for headless Firefox, Brave's `--no-sandbox`, Verovio SVG position parsing, the Wikidata API as a fallback when DNB and lobid show a bot wall, revision histories from timestamps.

### Field results, 2026-10-12/13 (two camat_v2 handoff sessions)

Both started after `/clear` from the project's `HANDOFF.md`. Numbers from `delegate cost --session`, including the calls that wrote each report.

| session | work | delegates | calls | context at end | cost units | units per call | idle rebuild | Claude 7d |
|---|---|---|---:|---:|---:|---:|---|---|
| 9ce4e364, PDF facsimile module from a written guide | 1 item | Codex build, Cursor review | 60 | 224k | 1.67M | 27.8k | none | 7 → 6 |
| 6a4f5d31, upstream issues: re-check, post, follow up | 1 item | none | 79 | 193k | 1.67M | 21.1k | none | 98 at end (weekly refill) |

- **Round 1 passed on a data module.** Codex @xhigh built `camat.pdf_facsimile` (887 lines, 48 tests) in 16 min. Expected values came from an acceptance fixture that the orchestrator built from the edition's own files, independently of the code being ported; all checks passed with no fix round. Cursor's review and the orchestrator's full read each found one bug the other missed.
- **27.8k units per call** in 9ce4e364 came from staying past the ~150k picker for the review, up to 224k. It was the same item, so that is the intended use, and the late calls' re-reads are its price.
- **Verify must match CI.** A bare `pytest -q` collected a release-only test folder, so the run reported verify FAIL, and both medium review findings were about the command. SKILL.md now says to copy `--verify` from the CI workflow, and to edit the spec before launch when a brief deliberately narrows it.
- **Status-update prompts:** the harness asked for a status update 9 times across the two sessions, each time during a run of several reads or probes. The rules now ask for one line before such a run.
- **A stray `cd` broke `cost --session latest`** at the end of 6a4f5d31. It now walks up to the nearest parent folder that has a project dir.
- **Decisions in pickers (user request, 2026-10-13).** Decisions had often been asked in prose. The rules now say discrete choices go into `AskUserQuestion` with the recommended option first. Lasting decisions also get Codex's and Cursor's picks through `delegate ask --agent both`. First use, on how to open-source this repo: both picked the same option, their risks did not overlap, and Cursor corrected a fact in the question.
- **Not in this repo's scope:**
  - Re-read every cited thread in full before posting a day-old draft: it found 4 errors.
  - `gh api markdown -f mode=gfm` as a pre-post render probe, and diffing the posted body after posting.
  - Issue images at 2× with `<img width>` set to half the pixel width.
  - Verovio's Python log capture through fd redirection.
  - Sanity-check an automated schema traversal against a known member: it first reported 326 elements instead of 17.

Still open:
- ~~Cursor's "Models" quota has no openusage metric.~~ Resolved above: it is the Auto pool.
- ~~Composer's billing pool is unverified.~~ Settled 2026-10-10: Composer and named Grok bill "Cursor Models", which is the Auto pool.
- Re-measure on the next long session with `delegate cost --session latest`.
- Orchestrator effort: measured above (lever 4), keep xhigh. For proof, run one routine work item at `high` and compare its thinking tokens and rounds against an xhigh item.

## What the orchestrator session cost

- **Volume:** 386 API calls. Context grew from 34k to 774k tokens with **zero compactions**. Cache re-reads were 98.3% of tokens and 70.5% of cost; cache writes were 21.0% and output 8.5%.
- **Cost grows quadratically with session length**, because each call re-reads everything before it. Small fixes late in the day were the most expensive per call:

| work item | calls | mean context: actual → fresh session | cost share: actual → fresh |
|---|---:|---|---|
| v3.1 categories | 114 | 208k → 208k | 14.5% → 14.5% |
| v3.2 identity | 93 | 400k → 131k | 18.4% → 8.5% |
| v3.3 automation | 70 | 555k → 118k | 22.9% → 7.5% |
| v3.4 viewer | 57 | 662k → 89k | 21.7% → 4.2% |
| v3.4.1 rate cap | 14 | 710k → 46k | 4.4% → 1.0% |
| v3.4.2 cold-cache fix | 19 | 728k → 48k | 11.5% → 1.2% |
| LLM tooling / feedback | 19 | 754k → 54k | 6.7% → 1.5% |

- **What filled the 774k context at the end:**

| source | share | tokens |
|---|---:|---:|
| retained thinking | 21% | 166k |
| file Reads | 19% | 146k |
| the orchestrator's own Bash command text (inline scripts, heredocs) | 13% | 102k |
| Write content (briefs, docs) | 6% | 50k |
| grep, git, web, dry-run and other results | 13% | 98k |
| images | 2% | 18k |
| scaffold | 4% | 34k |
| **all `delegate` CLI output combined** | **2.6%** | **20k** |

- **Delegates were cheap by comparison.** Cursor auto used about 5x fewer tokens per run than Codex @xhigh, and moved its pool by about 1 point over 9 runs.

## Levers, ranked by measured impact

### 1. One work item per orchestrator session: about 60%

At each merged PR or release, write a handoff (memory files plus `HANDOFF.md`: state, open PRs, next step), then `/clear` or start a new session. The modelled saving is 59.8–61.6% for this day.

The skill can push for this in three ways:
- **SKILL.md:** "after `apply` + commit/PR of a work item, write the handoff and suggest a fresh session".
- **`delegate apply`:** print a one-line handoff nudge.
- **Optional user-level hook:** a `UserPromptSubmit` hook (or a status-line field) that reads the transcript's last `usage`. Past a threshold (e.g. 250k context), it injects "context is N k; each turn re-reads it; if the item is done, hand off". This needs a change to `~/.claude/settings.json`.

### 2. Launcher exit packet, no premature polling: about 8% removable (15.3% gross)

40 check calls cost 15.3% in total:
- 16 checked a run that was still going (4.1%);
- 17 were the first check after a run finished (9.3%);
- 7 others (1.9%).

The cost is the context re-read per call, not the size of the output. `launch` hands over to `run.sh` (`os.execvp`), so the background-task notification carries no result, and a `status` call is always needed after it.

**Fix:** `run.sh` prints a compact packet when it exits:
- state, exit code, verify pass/FAIL;
- `diff --numstat` top files;
- the delegate's conflicts/risks section;
- usage;
- artifact paths.

The notification then *is* the status. SKILL.md: "never poll a run started with `run_in_background`; wait for the notification".

### 3. Batch independent tool calls: about 5%

307 of 386 calls had exactly one tool use. 28 responses could have been merged. This is a SKILL.md guidance item. Bundled CLI forms help too: `delegate status A B`, and a `check` that runs verify + digest in one call.

### 4. Keep heavy reading and scripting out of the main context: about 4% measured, more possible

- **File reads are the largest result category.** The two full assessment documents alone cost 9.4M re-read tokens, about 3.9%. Push "read and summarise" work to a side context:
  - `delegate ask` (new): Cursor auto `--mode ask`, a nearly free pool, which answered a trivial native-file question in 4s;
  - or a Claude subagent;
  - then read only the flagged spots.
- **The orchestrator's own inline Bash scripts** (102k tokens) stay in context too. Repeated analyses should become `delegate` subcommands or files, not heredocs.
- **Retained thinking** (21% of the final context) only moves with the orchestrator's effort setting. **Measured 2026-10-10: keep xhigh.**
  - Thinking is retained across calls: each call's context grows by at least the previous call's full output.
  - About 260k thinking tokens over 387 calls cost **29%** of the session, but 21.6 points of that were re-reads caused by the session's length. With one work item per session (lever 1), the same thinking costs about 10%. A lower effort would save only a fraction of that: about 3% if it cut thinking by a third, which is a guess, since high was never measured.
  - xhigh already scales thinking to the turn. Routine steps got little: `delegate status` averaged 170 tokens, `Read` 215, replies 262. Thinking went to judgement: briefs and docs (`Write`, mean 1.6k), `Edit` and commits (1.5k each). The top 10% of calls held 41% of it. A lower effort would mostly cut those turns.
  - Brief gaps caused 3 of the 6 extra rounds (lever 5). One avoided round outweighs the remaining thinking cost.
  - The real waste is upstream: 107 inline `.venv/bin/python` / heredoc calls carried 27% of all thinking. That is the orchestrator doing worker jobs (lever 4).
- **Trade-off (needs a decision):** SKILL.md currently mandates reading the whole patch. A digest + targeted-read review would save tokens, but it weakens that guarantee. A middle ground: keep the full read for modules with semantic rules or data rules, and use digest + targeted reads for UI, docs and plumbing.

### 5. Fewer rounds: better briefs and native repo instructions (quality lever)

Brief gaps caused 3 of the 6 extra rounds, and each round also costs orchestrator turns to adjudicate.

- **Repo conventions in `AGENTS.md` (verified 2026-10-10):**
  - Headless `codex exec` loads `AGENTS.md`, and only that file. It ignored `CLAUDE.md` and `.cursor/rules`.
  - `cursor-agent -p` loads `AGENTS.md`, `CLAUDE.md` and `.cursor/rules/*.mdc` (`alwaysApply`).
  - So stable conventions, schema rules and the real-data dry-run command go in the target repo's `AGENTS.md`, written once. Briefs cite it and carry only the deltas. A `CLAUDE.md` can import it with `@AGENTS.md`.
- **Brief templates** (`templates/{implement,mechanical,review,research}.md`): `<task>` stating intent over mechanism (the "derive must not add columns" lesson), `<repo_conventions>` (deltas only), `<schema_contract>`, and `<verification_loop>` (a real-data dry run with expected outcomes). The mechanical template stays short.
- **Workflow steps:**
  - **Step 0, prefetch:** delegates have no network. Also inspect the data before writing rules.
  - **After merging multi-delegate work:** run one real integrated pass, with the same cache state as production (cold if production is cold) and a preflight for offline inputs.
  - **Before outward-facing actions:** check every quantitative claim in the docs against the executing code.

### 6. Measurement, so the next report is automatic

- **Codex `--json` reports exact per-turn usage** (verified): `turn.completed.usage` has `input_tokens`, `cached_input_tokens`, `output_tokens` and `reasoning_output_tokens`. Switching the launcher means three changes:
  - `session_id` must come from `thread.started`;
  - `log_tail` must render events;
  - non-JSON lines such as "Reading additional input from stdin..." must be skipped.

  `-o last-message.md` keeps working. Alternatively, difference the cumulative "tokens used" between rounds.
- **Cursor:** the `result` event already carries `usage` (`inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`) and `duration_ms`.
- **Bug (confirmed):** `meta.json` `history` lacks the current round, because `followup` only appends the round it archives. Write each round's entry when that round finishes, including duration, verify result and usage.
- **Run log:** append one JSONL record per run on `apply` (and on `cleanup --purge` of unapplied runs) to `~/.cache/claude-delegate/runs.jsonl`.
- **`delegate cost [--since] [--repo] [--session ID]`:** sums the runs and computes the orchestrator's own cost from its transcript. Sol's dedupe-by-`message.id` script is in the `research-orch-cost` run log.

### 7. Routing and small CLI fixes

- **`route` honours preferences:** a catalog `prefer` list or `--prefer`. On eu-pd-composers it picked `codex-astra` despite the user's pins.
- **Grok in Cursor bills a "Models" quota, not premium (per the user).** openusage exposes no such metric (only `plan_auto_percent_used`, `plan_api_percent_used`, `plan_percent_used`). So add a `cursor-grok` delegate with an unknown pool and fix the docs; don't route it against premium.
- **`followup --link PATH`.**
- **Review packet:** add the last `verify.log` (tail) next to `review-N.patch`, so ask-mode reviewers, who can't run shell, can still check claims.
- **`verify` / `diff` without a live worktree:** `verify` runs in `meta["worktree"]` and fails once it is gone, and it prints full test output. Print a tail and a summary instead; the full output stays in `verify.log`.

### 8. Idle cache rebuilds: 12.7% of logged cost (found 2026-10-12)

By measured impact this ranks second, after lever 1. The prompt cache expires after 60 minutes without a call. The next call rewrites the whole context at 2× the input price, about as much as 20 ordinary calls at the same size. Over the 14 sessions in `token_usage.jsonl`, 7 had at least one such rebuild, 7–26% of each session's cost and 7.2M of 56.3M units in total. Of the 11 rebuilds, 7 followed a user prompt after a break (3.6M units) and 4 followed a background run's notification after a long run (3.6M).

- **Context guard:** after ≥60 min idle at ≥100k context, the hook holds the first prompt once and tells the user that `/clear` plus "continue from the handoff" starts at ~37k; sending the prompt again goes ahead. The user chose blocking over an inform-only nudge, because only blocking saves the rebuild itself. `CONTEXT_GUARD_IDLE_MIN` and `CONTEXT_GUARD_IDLE_CONTEXT` override the thresholds. UserPromptSubmit also fires for task notifications, `/loop` turns and subagent reports, and a held prompt never reaches Claude, so the hook lets through any prompt that starts with `<` or `/`. Built by Cursor Auto (run `idle-guard`), reviewed by Codex.
- **Measurement:** `delegate cost --session` lists idle rebuilds and their share of cost units.
- **Guidance:** hand off before a wait of over an hour (already in SKILL.md since 2026-10-10, and in the rules). This is the only lever for the notification half: the hook runs on user prompts, so it cannot hold a wake-up from a finished run.

### Not worth doing for tokens

- **Capping CLI output:** 0.05%.
- **Screenshots through a text-returning worker:** 0.5%.
- **Codex `--output-schema` for final messages:** it risks losing the free-form conflict flags that saved rounds. A soft `## Digest` block appended to the contract is enough for the exit packet.

## Implementation jobs (disjoint file ownership)

| job | owner | files | content |
|---|---|---|---|
| A: run lifecycle | Codex gpt-6.1-sol | `scripts/runs.py`, `scripts/delegate` | exit packet in `run.sh`; compact `status`/`digest` with `--full`; per-round history and usage; Codex `--json`; `followup --link`; review packet with verify tail; `verify` tail and no-worktree handling; run log on `apply` |
| B: routing and cost | Cursor auto | `scripts/route.py`, `scripts/quota.py`, `scripts/catalog.json`, new `scripts/cost.py` | `prefer` / `--prefer`; Grok pool entry; `delegate cost` over `runs.jsonl` and the orchestrator transcript (the subcommand is registered in A's file, so B ships `cost.main` and A wires it, or A lands first) |
| C: workflow docs | Cursor auto | `SKILL.md`, `templates/*`, README | session handoff step; "wait for notification, don't poll"; batching; `delegate ask` usage; `AGENTS.md` guidance; prefetch / integrated e2e / docs-claims checks; review policy (pending decision) |
| D: context guard (optional) | orchestrator | user-level hook or status line | needs the user's OK, because it changes `~/.claude/settings.json` |
