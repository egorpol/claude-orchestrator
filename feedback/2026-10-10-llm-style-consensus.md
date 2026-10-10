# Field report: LLM style consensus → eu-pd-composers v3.5.0 (2026-10-10)

Orchestrator: Claude Opus 5.5 (session `fc63a0c8`). Work item from `assessments/HANDOFF.md`: decide how the finished
all-composer LLM style labels reach the dump and viewer, implement, ship. Shipped as PR egorpol/eu-pd-composers#9 →
v3.5.0 (dump r015). Machine-readable rows: [`runs.jsonl`](runs.jsonl), [`token_usage.jsonl`](token_usage.jsonl).

## Flow and cost

| Step | Who | Notes |
|---|---|---|
| Measure options, ask user | orchestrator | consensus / single model / union / two sources, with F1 vs Wikidata + coverage; 4 questions, one round |
| Implement | Codex gpt-6.1-sol @xhigh, 2 rounds | 14 min round 1; round 2 = one revert. Usage r1: 114k in, 2.2M cached, 28k out (r2 cumulative 139k / 3.1M / 33k) |
| Cross-review | Cursor auto, ask mode | 6.6 min, APPROVE, no findings |
| Build r015, PR, release | orchestrator | stage run alone on r014; check_release vs r014; merge + tag + GitHub release |

Orchestrator: 63 calls, 6.3M cache read, 49k output, context 35k → 151k. Quota: claude-7d 58 → 54%, codex-7d
60 → 56%, cursor-auto unchanged. Compare the 2026-10-09 marathon (167.5M) and the 2026-10-10 pass session (7.6M): one
item per session keeps holding.

## What worked

- **Numbers in the decision question.** Computing each candidate policy's precision/recall/F1 against the reference and
  its coverage before asking turned four design choices into one `AskUserQuestion` reply.
- **Expected real-data counts in the brief.** The brief gave the exact counts the stage must produce (2,842 consensus,
  2,748 periods, 3,360 both-labelled) and said "report mismatches rather than bending the code". All matched.
- **Conflict flagging again.** Codex found that a *full* pipeline derive with the local cache rewrites ~170 unrelated
  cells (63 missing cache entries; one remap clears values on a cache miss). It flagged this as a conflict instead of
  "fixing" it. The orchestrator built r015 from the new stage alone and logged the bug.
- **Code-only patches.** Asking the delegate to delete generated dumps and restore exported viewer data kept the patch
  reviewable (no 10 MB TSVs); the orchestrator regenerated them after apply.

## Friction

1. **Literal brief → convention break, and the reviewer endorsed it.** The brief asked for a "byte-identical"
   re-derive. In this repo every stage stamps `dump_date` with the output revision, and tests compare modulo `dump_date`.
   Codex made derive keep the *source* date to satisfy the check. Cursor's review marked it "met". Only reading the
   pipeline diff caught it. Same class as the 2026-10-09 `dump_date` incident.
   - **Proposal (template):** `<schema_contract>` should list convention-stamped columns excluded from identity checks.
   - **Proposal (review template):** ask the reviewer "does any brief requirement contradict an existing repo
     convention? If so, flag the brief, not just the code."
2. **Auto-mode classifier blocked `git rm` of tracked dump files, then the commit.** Recovery was cheap (handoff with
   the drafted message; the user approved commit + rm + push/PR in one reply).
   - **Proposal (SKILL.md, Merge step):** in auto mode, collect destructive or outward git steps into one explicit
     approval request at the end of the item.
3. **Codex usage is still cumulative across resumed rounds** (r2 shows totals). Per-round deltas would make `cost`
   exact (open since 2026-10-09).
