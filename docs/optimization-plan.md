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

Also fixed along the way:
- `.pyc` files from test runs leaked into patches (`PYTHONDONTWRITEBYTECODE=1`);
- `ask --files` swallowed the question;
- `exit_code` was written only after verify (there is now a "verifying" state);
- `status --full` crashed on review jobs.

### Field result (2026-10-10, session 55a11d16: one item, LLM style pass)

Lever 1 held up. The session started after a `HANDOFF.md` and ran one work item: a 4.5h labelling run by a detached driver, with background waiters instead of polling.

| | marathon 762dc86f | 55a11d16 |
|---|---:|---:|
| calls | 389 | 88 |
| context at end | 779k | 158k |
| cost units | 26.2M | 1.9M (14× less) |
| units per call | 67k | 22k (−68%) |
| Claude 7d pool | 84 → 67 (shared with other sessions) | 60 → 58 |

Per call the saving is −68%, close to the −60% the model predicted. The work differs, so compare per call, not in total.

New findings:
- **Idle cache rebuilds.** Two wake-ups after 4.4h and 6.1h waits each re-wrote the whole context, about 120k tokens at 2×. Together they were 26% of the session's cost. SKILL.md now says to hand off before a multi-hour wait.
- **Cursor pools resolved.** openusage `plan_auto_percent_used` = (Auto + Grok + Composer spend) / $450, exact at two snapshots. So "Cursor Models" shares the Auto pool, and `cursor-grok` now routes on `cursor-auto` with real quota. Named Claude/GPT models bill `plan_api` ($22.50).
- **Bulk labelling** (6.5k rows, outside `delegate`): grok-4.7-low used about 2.7 points of Cursor Auto; gpt-6.1-sol@low used about 15 points of Codex 7d. Both had zero failures.
- **Lessons now in SKILL.md:** read-only is not blind (isolate the answer key), per-chunk quota gates, PID files for detached drivers.
- **Thinking share was unchanged** at about 60% of output under xhigh, which is consistent with the effort analysis in lever 4.

Still open:
- ~~Cursor's "Models" quota has no openusage metric.~~ Resolved above: it is the Auto pool.
- ~~Composer's billing pool is unverified.~~ Settled 2026-10-10: the Cursor spending tab lists "Cursor Models · Includes Cursor Grok and Composer", so both named Grok and Composer bill the `cursor-models` pool.
- Re-measure on the next long session with `delegate cost --session latest`.
- Orchestrator effort: measured above (lever 4), keep xhigh. If you want proof, run one routine work item at `high` and compare its thinking tokens and rounds against an xhigh item.

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

- **`route` honours preferences:** a catalog `prefer` list or `--prefer`. On eu-pd-composers it picked `codex-astra` despite your pins.
- **Grok in Cursor bills a "Models" quota, not premium (per the user).** openusage exposes no such metric (only `plan_auto_percent_used`, `plan_api_percent_used`, `plan_percent_used`). So add a `cursor-grok` delegate with an unknown pool and fix the docs; don't route it against premium.
- **`followup --link PATH`.**
- **Review packet:** add the last `verify.log` (tail) next to `review-N.patch`, so ask-mode reviewers, who can't run shell, can still check claims.
- **`verify` / `diff` without a live worktree:** `verify` runs in `meta["worktree"]` and fails once it is gone, and it prints full test output. Print a tail and a summary instead; the full output stays in `verify.log`.

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
| D: context guard (optional) | orchestrator | user-level hook or status line | needs your OK, because it changes `~/.claude/settings.json` |
