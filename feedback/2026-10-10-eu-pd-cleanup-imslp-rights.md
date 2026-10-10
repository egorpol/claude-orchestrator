# Field report: cache-miss fix, repo cleanup, IMSLP rights investigation → eu-pd-composers (2026-10-10)

Orchestrator: Claude Opus 5.5 (session `e06990b8`). Started from `assessments/HANDOFF.md`; the user then added three more items in the same session. No delegates ran: all work was done inline. Machine-readable rows: [`runs.jsonl`](runs.jsonl), [`token_usage.jsonl`](token_usage.jsonl).

## Flow and cost

| # | Item | Outcome |
|---|---|---|
| 1 | Handoff bug: `remap_work_evidence.py` cleared evidence on a cache miss (flagged by Codex in the previous session) | Fixed and tested; a full offline derive cut works drift from 159 cells to 4 (genuinely newer cache). The remaining 12 composer cells were diagnosed as a stale cache, not a bug. Branch `fix/work-evidence-cache-miss` |
| 2 | Repo cleanup + README + gitignored `paper/` + "reassess what we achieved" | 9 legacy scripts moved to `scripts/legacy/`; 6 local and 5 remote merged branches deleted, plus a stale delegate worktree; README rewritten (figures, use cases, maintainer task table, limits); 5 docs moved to r015; private status notes in `paper/`. Branch `chore/repo-cleanup`, stacked |
| 3 | "Tackle the IMSLP issue + plan the paper" | 3,307 "unflagged" works explained by IMSLP hosting policy (category definitions fetched from IMSLP); a phased plan with 4 user decisions in one question; a resumable file-list fetcher, run live (≈ 600 requests, 12.5 min, background) |
| 4 | File-level analysis | File-name server tokens (`PMLUS`, `PMLASIA`) decoded and checked against death years; all but ≈ 200 works explained |

Orchestrator: 100 calls, 14.0M cache read, 221k cache write, 111k output, context 36k → 243k (just under the 250k context-guard threshold). Quota: claude-7d 54 → 51%; codex and cursor untouched. Compare: the single-item session `fc63a0c8` cost 6.3M cache read in 63 calls. Four items in one session doubled the cost, which is the "one item per session" lever measured again.

## What worked

- **A delegate's conflict flag became the next session's item and closed cleanly.** The previous report logged "derive rewrites ~170 cells". This session reproduced it with a per-stage cell-diff script, attributing every changed cell to a stage. The fix was verified against real data, and the residue was classified (stale cache vs bug) before any code changed.
- **Falsify the hypothesis with one cheap request before writing it down.** The working hypothesis ("pages without a rights category host no files") was about to go into the plan. One batched API call on 12 sampled pages showed 6 of 6 *with* files and revealed the server tokens, which became the main finding.
- **Cite the source's own definitions.** Three WebFetches of IMSLP category pages turned guessed semantics into quotable definitions for the paper.
- **Long network job in the background.** The fetch ran via `run_in_background` while the orchestrator committed, pushed and wrote the handoff; the completion notification triggered the analysis.
- **Destructive git steps batched into one question with evidence.** All branches were checked as ancestors of `main`. The leftover worktree's uncommitted files were compared with the merged commit (an earlier draft of `874c53d`) before `--force` removal.
- **Stacked branches when the user says "no PR yet".** The three branches each edit `CHANGELOG [Unreleased]`, so stacking avoided guaranteed conflicts; the handoff says one PR from the top branch covers all three.

## Friction

1. **Session sprawl: four items, 243k context.** Each new item was user-initiated mid-session, and the orchestrator continued instead of offering a handoff and `/clear` at the item boundary. The handoff was kept current after each item, so a clear would have cost nothing.
   - **Proposal (SKILL.md / CLAUDE.md guidance):** when a new item arrives after one is finished and the context is above ~120k, write the handoff first and offer `/clear` in one line before starting it.
2. **No delegation, even where it fitted.** The doc refresh across 5 files (stale revision ids, a missing stage, legacy paths) and the fetcher (a near-copy of an existing script plus a refactor) had clear specs and would have suited Cursor auto (71% left, 2.8× pace, i.e. spare quota). Instead all of it spent Claude quota (51% left, 1.5× pace).
   - **Proposal (route guidance):** mechanical multi-file doc updates and "clone this script for a sibling API prop" are delegable even mid-session; check `delegate quota` pace before doing them inline.
3. **A stale delegate worktree outlived its applied run** (`viewer-layout-style`, applied as `874c53d` the day before). It held uncommitted edits that looked like unmerged work until diffed.
   - **Proposal (runs.py):** `status` should flag worktrees whose run is applied and older than a day; `apply` could suggest `cleanup` in its last line.
4. **Small tool slips.**
   - `git stash` / `stash pop` used to compare pre-change behaviour unstaged 11 `git mv` renames (recovered by re-adding). Use `git show HEAD:path > tmp` instead.
   - A `sed` edit on a wrapped Markdown line silently matched nothing. Python replace with an `assert count == 1` caught every other prose edit.
