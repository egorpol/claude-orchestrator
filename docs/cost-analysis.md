# Cost analysis (baseline 2026-10-09)

Based on the 2026-10-09 eu-pd-composers session: 13 hours, 16 delegate runs, five releases. Sources:

- `feedback/` (local field reports, gitignored since 0.1.0): the field report, the token-usage report, and the run and usage JSONL files.
- **Transcript analysis** by Codex gpt-6.1-sol @xhigh, read-only, two rounds (run record `research-orch-cost`). It reconciled the transcript exactly against openusage: 164.59M cache read, 2.56M cache write, 398k output.
- **Design audit** of `delegate` against the reports by Cursor auto (`research-skill-design`).
- **Native instruction-file test** and a `codex exec --json` test, run by hand.

All percentages are shares of the orchestrator session's API-equivalent cost of about $54. Relative prices: input 1, cache read 0.1, 1h cache write 2, output 5.

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

What was built:
- **SKILL.md** "Sessions" section: after `apply` + commit/PR of a work item, write the handoff and suggest a fresh session.
- **`delegate apply`:** prints a one-line handoff nudge.
- **`hooks/context-guard.py`:** a `UserPromptSubmit` hook that reads the transcript's last `usage`. From about 120k context it asks Claude to hand off before starting a new work item. Installed here with `./install.sh --hooks`.
- **`rules/orchestrator.md`:** user-level rules imported by `./install.sh --rules`, so the guidance reaches sessions that never invoke the skill.

Later sessions that started from a handoff and stayed to one item were observed at 17.7–22k units per call against the marathon's 67k; see [field-results.md](field-results.md#2026-10-10) and [field-results.md](field-results.md#2026-10-11).

### 2. Launcher exit packet, no premature polling: about 8% removable (15.3% gross)

40 check calls cost 15.3% in total:
- 16 checked a run that was still going (4.1%);
- 17 were the first check after a run finished (9.3%);
- 7 others (1.9%).

The cost is the context re-read per call, not the size of the output. `launch` hands over to `run.sh` (`os.execvp`), so the background-task notification carried no result, and a `status` call was always needed after it.

What was built: each job's `run.sh` calls `_finalize`, which prints a compact packet (at most 20 lines) into the notification — state, exit code, verify pass/FAIL, `diff --numstat` top files, the Digest conflicts/risks section, usage, artifact paths. Compact `delegate status NAME` shows that packet; `--full` adds the log tail. SKILL.md says never to poll a run started with `run_in_background`; wait for the notification.

### 3. Batch independent tool calls: about 5%

307 of 386 calls had exactly one tool use. 28 responses could have been merged. This became SKILL.md guidance. `delegate status` and `delegate wait` also take several run names in one call.

### 4. Keep heavy reading and scripting out of the main context: about 4% measured, more possible

- **File reads are the largest result category.** The two full assessment documents alone cost 9.4M re-read tokens, about 3.9%. "Read and summarise" work was pushed to a side context:
  - `delegate ask`: Cursor auto `--mode ask`, a nearly free pool, which answered a trivial native-file question in 4s (about 10s in later checks);
  - or a Claude subagent;
  - then read only the flagged spots.
- **The orchestrator's own inline Bash scripts** (102k tokens) stay in context too. Repeated analyses became `delegate` subcommands (notably `delegate cost`) or files, not heredocs.
- **Retained thinking** (21% of the final context) only moves with the orchestrator's effort setting. **Measured 2026-10-10: keep xhigh.**
  - Thinking is retained across calls: each call's context grows by at least the previous call's full output.
  - About 260k thinking tokens over 387 calls cost **29%** of the session, but 21.6 points of that were re-reads caused by the session's length. With one work item per session (lever 1), the same thinking costs about 10%. A lower effort would save only a fraction of that: about 3% if it cut thinking by a third, which is a guess, since high was never measured.
  - xhigh already scales thinking to the turn. Routine steps got little: `delegate status` averaged 170 tokens, `Read` 215, replies 262. Thinking went to judgement: briefs and docs (`Write`, mean 1.6k), `Edit` and commits (1.5k each). The top 10% of calls held 41% of it. A lower effort would mostly cut those turns.
  - Brief gaps caused 3 of the 6 extra rounds (lever 5). One avoided round outweighs the remaining thinking cost.
  - The real waste is upstream: 107 inline `.venv/bin/python` / heredoc calls carried 27% of all thinking. That is the orchestrator doing worker jobs (lever 4).
- **Review policy (decided):** risk-based. Full patch read for data and semantic modules (parsers, validation, matching, accounting, safety guards); digest plus targeted reads for UI, docs and plumbing. Written into SKILL.md.

Thinking share was later observed still at about 60% of output under xhigh; see [field-results.md](field-results.md#2026-10-10).

### 5. Fewer rounds: better briefs and native repo instructions (quality lever)

Brief gaps caused 3 of the 6 extra rounds, and each round also costs orchestrator turns to adjudicate.

What was built:
- **Repo conventions in `AGENTS.md` (verified 2026-10-10):**
  - Headless `codex exec` loads `AGENTS.md`, and only that file. It ignored `CLAUDE.md` and `.cursor/rules`.
  - `cursor-agent -p` loads `AGENTS.md`, `CLAUDE.md` and `.cursor/rules/*.mdc` (`alwaysApply`).
  - So stable conventions, schema rules and the real-data dry-run command go in the target repo's `AGENTS.md`, written once. Briefs cite it and carry only the deltas. A `CLAUDE.md` can import it with `@AGENTS.md`.
- **Brief templates** (`skills/delegate/templates/{implement,mechanical,review,research}.md`): `<task>` stating intent over mechanism (the "derive must not add columns" lesson), `<repo_conventions>` (deltas only), `<schema_contract>`, and `<verification_loop>` (a real-data dry run with expected outcomes). The mechanical template stays short.
- **Workflow steps** in SKILL.md and the templates:
  - **Step 0, prefetch:** delegates have no network. Also inspect the data before writing rules.
  - **After merging multi-delegate work:** run one real integrated pass, with the same cache state as production (cold if production is cold) and a preflight for offline inputs.
  - **Before outward-facing actions:** check every quantitative claim in the docs against the executing code.

### 6. Measurement, so the next report is automatic

What was built:
- **Codex `--json` reports exact per-turn usage** (verified): `turn.completed.usage` has `input_tokens`, `cached_input_tokens`, `output_tokens` and `reasoning_output_tokens`. The launcher takes `session_id` from `thread.started`, renders events in `log_tail`, and skips non-JSON lines such as "Reading additional input from stdin...". `-o last-message.md` still works. Each round records its own usage delta (the raw thread total is cumulative on resume).
- **Cursor:** the `result` event already carries `usage` (`inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`) and `duration_ms`.
- **History:** each round's `meta.json` history entry is written when that round finishes, including duration, verify result and usage (older runs lacked the current round because `followup` only appended the round it archived).
- **Run log:** one JSONL record per run in `~/.cache/claude-delegate/runs/runs.jsonl`, written at `apply`, or at `cleanup` for read-only, hand-merged or discarded runs.
- **`delegate cost [--since] [--repo] [--session ID]`:** sums the runs and computes the orchestrator's own cost from its transcript. Sol's dedupe-by-`message.id` script is in the `research-orch-cost` run log.

### 7. Routing and small CLI fixes

What was built:
- **`route` honours preferences:** a catalog `prefer` list or `--prefer`. On eu-pd-composers it had picked `codex-astra` despite the user's pins.
- **`cursor-grok`:** Grok in Cursor bills a "Models" quota, not premium. openusage's `plan_auto_percent_used` is Auto + Grok + Composer over $450, so `cursor-grok` routes on the `cursor-auto` pool with real quota. Named Claude/GPT models bill `plan_api`.
- **`followup --link PATH`.**
- **Review packet:** the last `verify.log` (tail) sits next to `review-N.patch`, so ask-mode reviewers, who can't run shell, can still check claims.
- **`verify` / `diff` without a live worktree:** `verify` prints a tail and a summary; the full output stays in `verify.log`. Both commands work after the worktree is gone.

### 8. Idle cache rebuilds: 12.7% of logged cost (found 2026-10-12)

By measured impact this ranks second, after lever 1. The prompt cache expires after 60 minutes without a call. The next call rewrites the whole context at 2× the input price, about as much as 20 ordinary calls at the same size. Over the 14 sessions in `token_usage.jsonl`, 7 had at least one such rebuild, 7–26% of each session's cost and 7.2M of 56.3M units in total. Of the 11 rebuilds, 7 followed a user prompt after a break (3.6M units) and 4 followed a background run's notification after a long run (3.6M).

What was built:
- **Context guard:** after ≥60 min idle at ≥100k context, `hooks/context-guard.py` holds the first typed prompt once and tells the user that `/clear` plus "continue from the handoff" starts at ~37k; sending the prompt again goes ahead. The user chose blocking over an inform-only nudge, because only blocking saves the rebuild itself. `CONTEXT_GUARD_IDLE_MIN` and `CONTEXT_GUARD_IDLE_CONTEXT` override the thresholds. UserPromptSubmit also fires for task notifications, `/loop` turns and subagent reports, and a held prompt never reaches Claude, so the hook lets through any prompt that starts with `<` or `/`. Built by Cursor Auto (run `idle-guard`), reviewed by Codex.
- **Measurement:** `delegate cost --session` lists idle rebuilds and their share of cost units.
- **Guidance:** hand off before a wait of over an hour (in SKILL.md since 2026-10-10, and in `rules/orchestrator.md`). This is the only lever for the notification half: the hook runs on user prompts, so it cannot hold a wake-up from a finished run.

Idle rebuild share across later sessions is tabulated in [field-results.md](field-results.md); the 14-session figure above is the scan that ranked this lever.

### Not worth doing for tokens

- **Capping CLI output:** 0.05%.
- **Screenshots through a text-returning worker:** 0.5%.
- **Codex `--output-schema` for final messages:** it risks losing the free-form conflict flags that saved rounds. A soft `## Digest` block appended to the contract is enough for the exit packet.

All eight levers were built between 2026-10-10 and 2026-10-13; see the [CHANGELOG](../CHANGELOG.md).

## Open questions

- Re-measure on the next long session with `delegate cost --session latest`.
- Run one routine work item at effort `high` against an xhigh item and compare thinking tokens and rounds.
