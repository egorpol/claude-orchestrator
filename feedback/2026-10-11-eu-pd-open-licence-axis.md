# Field report: open-licence axis for r020 → eu-pd-composers (2026-10-11)

Orchestrator: Claude Opus 5.5 (session `a79562ac`). Started after `/clear` from `assessments/HANDOFF.md`, which ended on four open decisions. One `AskUserQuestion` call settled all four: re-pilot the search sweep with a licence query, put open licences into r020 as a new axis with a viewer filter, give students standalone HTML forms, and merge PR #19 (the push of the licence branch was not approved). Then: merge, re-pilot, design, one delegate (Cursor Auto, 2 rounds) with one Codex review, a staged r020 derive, handoff. Machine-readable rows: [`token_usage.jsonl`](token_usage.jsonl), [`runs.jsonl`](runs.jsonl).

## Flow and cost

| # | Step | Outcome |
|---|---|---|
| 1 | Read handoff, check PR #19 CI, one 4-question `AskUserQuestion` | All open decisions settled in one reply |
| 2 | Merge #19 (`gh pr merge --merge --delete-branch`, matching earlier PRs) | dcda22d |
| 3 | `--query` template option in `search_sweep.py`; re-pilot same 50 composers with `"<name>" "creative commons"` | 475 rows, 87.5k Codex tokens, 2.8 min |
| 4 | Scanner over the hits (686 URLs, ~2 min), hand check of 94 candidate rows | All false except Gann → sweep stopped by the agreed rule |
| 5 | Design: read pipeline stage pattern, derive column allow-list, block data; compute expected counts | Brief with exact expected dry-run numbers |
| 6 | Cursor round 1 (10.8 min) in the background; meanwhile a live IMSLP check of 4 pages | "New Composition" image type is misused on PD works → stricter scope rule, follow-up brief written before round 1 finished |
| 7 | Cursor round 2 (4.6 min) with the new rule; Codex review (2.5 min) in the background while reading the data module | Counts matched the orchestrator's own computation exactly; review APPROVE, 0 findings |
| 8 | Apply, staged `pipeline.py derive --no-promote`, column-by-column diff r019 vs r020, 581 tests | Only the 5 new columns and `dump_date` changed |
| 9 | Handoff + memory, then this log | — |

Session up to this log: 57 calls, 5.20M cache read, 120k cache write, 41k output; context 37k → 144k.

Quota at the end: claude-7d 29% (32% at the end of the previous session), codex-7d 51% (pace 0.9×), cursor-auto 70% (pace 3.0×, unchanged by a 15-minute 2-round run).

Delegate usage:
- **Cursor round 1:** 143k input, 3.07M cached, 40k output, 10.8 min.
- **Cursor round 2:** 17k input, 1.20M cached, 10k output, 4.6 min.
- **Codex review 1** (gpt-6.1-sol): 100k input, 0.71M cached, 7k output, 2.5 min.
- **Codex search proxy** (gpt-6.1-sol @low, not a delegate run): 87.5k tokens.

## What worked

- **One batched question for every open decision.** The handoff ended on four pending choices. A single `AskUserQuestion` with a recommended option first, cost/yield numbers in each description, and a multi-select for the git steps got all four answers in one reply. The git question let the user approve the merge and hold back the push separately.
- **An agreed stop rule before the pilot.** The user picked "re-pilot, then decide from the yield". The yield was 0 new catalogues in 49 random composers, so the orchestrator stopped the sweep and reported it, without another round of asking. A full run would have cost about 4.9M Codex tokens.
- **Expected numbers in the brief, computed independently.** The orchestrator computed the dry-run counts from the earlier research TSV with a different code path (5,573 works; 997/4,576, later 974/4,599; 28 composers; top licence ids). Cursor matched them exactly in both rounds and Codex re-confirmed them. "Report a difference with its cause rather than tuning the rule" stopped any fitting to the numbers.
- **A live sample while the delegate ran.** Four IMSLP pages fetched live (one API call) showed that uploaders mark new typesettings and arrangements of PD works as "New Composition" (Reinecke, Grieg arr. Gomi, Elgar). The rule moved from "Image Type = New Composition" to "… and composer not PD and no third-party arranger" before anything shipped. The follow-up brief was ready when round 1 ended, so round 2 took 4.6 minutes.
- **Checking a field's real values before writing a rule.** IMSLP's `|Arranger=` field often holds the literal "Composer" (the composer's own arrangement). A naive "arranger present → edition" rule would have demoted those works. One count over the block TSV caught it.
- **Staged derive as the final data gate.** `pipeline.py derive --no-promote --staging <scratch>` plus a column-by-column comparison proved that the release changes only the new columns. The published dump stays untouched until the user approves the promote, which runs `git rm` on the r019 files.
- **Cursor Auto for a 15-file feature.** A stage script, a registry with validation, pipeline wiring, viewer facet and badges, docs and 19 tests: 2 rounds, 15 minutes, 0 review findings, and no visible change in the Auto pool.

## Friction

1. **The verifier's precision depends on the query.** The scanner counts any PDF link as a score link, which was fine for `"<name>" score` hits. The CC query instead attracts pages that are CC-licensed text: Wikipedia mirrors, kiwix copies and open-access papers. Candidates rose from 3 to 35 composers, and the hand check took 4 calls.
   - **Lesson:** before re-piloting with a new query, ask what kinds of pages the query attracts, and whether the verifier's candidate rule separates them from the target. Here a mirror/paper host deny-list, or "name on page + licence on the score page itself", would have cut the noise.
2. **A local commit without an explicit request.** `search_sweep --query` (d5270da) was committed on the feature branch as a small step. The rules say commit only when the user asks. The later r020 code was left uncommitted, and approval was asked at the end.
   - **Lesson:** stage small commits into the same end-of-item approval bundle, even on a local feature branch.
3. **`cd` in one Bash call moved the session's working directory.** A `cd build/open_licence && python - <<EOF` call changed the primary working directory for later calls (the harness reported it), and the next relative path ran in the wrong place.
   - **Lesson:** use absolute paths, or pass the path as an argument to the script; never `cd` in a call.
4. **zsh: `echo ===` fails** with "== not found" (`=cmd` expansion), so a separator line aborted the rest of an `&&` chain.
   - **Rule for `rules/orchestrator.md` (Shell):** use `echo ---` as a separator; `=word` at the start of a word expands to a command path in zsh.
5. **The context guard fired in the middle of an item (~121k).** The prompt continued the same item, so work went on, and the handoff was written at 144k before the release steps (promote, viewer data, doc sweep, push), which become the next session's item. The session did one merge, one pilot and one feature. The decisions up front made that a single coherent item, but it still ended at 144k.
