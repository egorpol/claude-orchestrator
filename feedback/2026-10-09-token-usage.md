# Token usage: eu-pd-composers marathon session (2026-10-09)

One Claude Code session (`762dc86f`, Opus 5.5, running inside Cursor) orchestrated five releases (v3.1.0 → v3.4.2), with
16 delegate runs or rounds. Per-run records are in [`token_usage.jsonl`](token_usage.jsonl).

## Where the tokens went

| Who | Tokens | Notes |
|---|---|---|
| **Claude orchestrator** (this session) | **167.5M total**: 164.6M cache read (98%), 2.56M cache write, 398k output, 782 fresh input | **$53.67** API-equivalent (openusage). That is 57% of all Claude Code spend that day ($93.89 across 6 sessions) |
| Cursor auto delegates, 9 runs incl. 1 review | 981k input, 184k output, 6.40M cache read | ≈109k in / 20k out / 0.7M cache per run; 2–12 min each |
| Codex gpt-6.1-sol @xhigh, 4 sessions (6 rounds + 1 review) | ≈551k "tokens used" (final per session) | Codex reports a cumulative figure per resumed session (102k → 147k → 182k over three rounds) |

Pool movement over the day (`delegate quota`): Claude 7-day 84% → 67%, Codex 7-day 86% → 81%, Cursor Auto 77% → 75%.
The Claude 7-day pool is shared by all sessions that day.

**Conclusion: about 95% of all tokens, and most of the cost, came from the orchestrator re-reading its own long context
on every turn.** Delegate model choice (Sol vs Auto) is a second-order cost factor. Orchestrator context management is
the first-order one.

## Why the orchestrator was expensive

- **One very long session, about 13 hours and five work items.** Every tool call re-reads the whole conversation from
  cache. Late in the day each turn carried hundreds of thousands of tokens of history from finished work items (the
  v3.1.0 category crawl, the identity review, and so on).
- **Many small sequential tool calls:** polling status, re-running checks, grepping, small edits. Each is a full context
  re-read.
- **Full-file reads during review:** the 250–300-line `pipeline.py` and `imslp_identity.py`, a ~350-line assessment,
  long logs, and verbose dry-run reports. Necessary for quality, but they stay in context for the rest of the session.
- **Screenshots:** ~10 PNGs for layout QA. Small individually, but they stay in context and are re-read on every later
  turn.
- **Long briefs and long user-facing summaries** (output tokens are the most expensive per token). Briefs ran to
  1–2k words each.

## Optimisation proposals (by expected impact)

1. **One work item per orchestrator session**, with an explicit handoff: memory files plus a short `HANDOFF.md` (state,
   open PRs, next step). Starting fresh resets the per-turn re-read cost. This is the biggest lever. The skill could
   prompt a handoff after each merged PR.
2. **Batch independent tool calls into one turn.** Parallel calls in one assistant message cost one context re-read;
   sequential ones cost one each. Prefer a single Bash call that runs several checks and prints a compact summary.
3. **Push reading-heavy review to cheap workers.** Ask the delegate's final message (or a read-only Cursor auto
   "summariser" run) for a structured digest of its own diff: functions changed, rules implemented, risky spots. Then
   read only the flagged parts in full. Keep the full read for semantics-critical modules only.
4. **Keep heavy artifacts out of the main context.** Do visual QA through a subagent or a delegate that returns "overlaps
   at 820 px: X/Y" instead of loading screenshots into the orchestrator. Truncate logs with `tail`/`grep` by default
   (already mostly done).
5. **Template the briefs.** A reusable skeleton (`<task>`, `<repo_conventions>`, `<schema_contract>`,
   `<verification_loop>`) with project-specific facts in a file would cut output tokens per brief and reduce brief-gap
   rounds, each of which cost 30–50% of a run.
6. **Prefer Cursor auto for implementation once a brief is precise.** It cost ≈5× fewer tokens per run than Codex xhigh
   for comparable tasks, ran faster, and moved its pool by about 1 point over 9 runs. Use Codex Sol where rules still
   have to be discovered against data (the identity-check round-2 redesign, pipeline idempotency).
7. **Instrumentation for the skill:**
   - record per-round token deltas: Codex reports cumulative totals per resumed session; Cursor's `result` event has
     `usage`;
   - log each run's tokens into `runs.jsonl` automatically on `apply`;
   - add a `delegate cost` command that sums per repo/day and includes the orchestrator session from
     `openusage session --json`.

## Data caveats

- openusage's Codex and Cursor per-model metrics were empty for these runs; the delegate logs are the per-run source of
  truth. Cursor's billing-cycle totals moved by about +1.43M input, +9.05M cached, +0.27M output (≈ $9.19), but that
  includes the user's own Cursor usage that day.
- Costs are API-equivalent estimates, not subscription charges.
