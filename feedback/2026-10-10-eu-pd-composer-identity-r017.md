# Field report: composer identity guard, GND / VIAF ids, dump r017 → eu-pd-composers (2026-10-10)

Orchestrator: Claude Opus 5.5 (session `4994250f`). Started from `assessments/HANDOFF.md` (S0: merge PR #14; S1: refresh caches, identity guard, authority ids, redump r017). No delegates ran. Machine-readable rows: [`token_usage.jsonl`](token_usage.jsonl), incidents in [`runs.jsonl`](runs.jsonl).

## Flow and cost

| # | Item | Outcome |
|---|---|---|
| 1 | Resume; `gh pr merge 14` | Blocked by the auto-mode classifier ("Merge Without Review") although the handoff listed it; the next read-only command was blocked too. Told the user, moved on |
| 2 | Cold cache warm-up crawl (background, 1 h 49 min, IMSLP ≤ 1 req/s) while writing S1 | `authorities.py`, `remap_authorities.py`, `refetch_authorities.py`, list-year flag in `build_dump.py`, `identity_candidates.py`, viewer link, gates, README tagline + LLM section, 25 new tests |
| 3 | Trial runs on r016 with the fresh cache | 34 suspects → 1 wrong identity (Jean Perrin); GND 2,889 + 7 lobid; 14 GND death conflicts |
| 4 | User: merge #14; "where do I fix Jean Perrin?" | Exact wikitext fix (`dab=composer`), Wikidata claim history (SweetBot 2016 PetScan added "composer" to the physicist) |
| 5 | r017 refresh v1 (warm cache, ~30 min) | Gates OK, but 2 PD flips came from a new rule |
| 6 | Checked the 2 flips against article leads | "after 1912" / "after 1914": list death years are floruits. Rule restricted to births; r017 rebuilt; 0 PD flips |
| 7 | Docs, CHANGELOG, PR #15, handoff, memory | — |
| 8 | User fixed Wikipedia + Wikidata; recheck, reviewer field, merge #15, this dump | Both upstream edits verified via API |

Session total: 138 calls, 23.5M cache read, 475k cache write, 129k output; context 36k → 284k. Per-item calls were not measured. Quota: claude-7d 45 → 41%; codex-7d (56%) and cursor-auto (71%) untouched.

The context guard fired at about 257k, after the PR was open. Items 4 and 8 were short user requests answered in place. Most of the growth came from items 2–6: one work item (S1) that included a 2.5 h network job, two dump builds and a debugging loop.

## What worked

- **Overlapping the long crawl with coding.** The warm-up crawl ran `build_dump.py` into a fresh cache under `build/` (git-ignored) with a scratch data dir. The final `pipeline.py refresh --cache-dir` then took ~30 min instead of a second cold crawl, and a rebuild after the fix cost the same.
- **Trial runs on the previous dump before building.** Running the new stage and the review tool on r016 with the fresh cache gave all the figures (coverage, conflicts, suspects) and found the Perrin case before any dump existed.
- **Existing tests caught a policy break.** `test_remap_composers` encodes "coarse dates use the interval's last year"; it failed when the refinement also applied to prior-dump years. The fix (refine only list years, and only for `*_from_list` rows in remaps) came from reading why the test existed.
- **Checking every PD flip against a source before the PR.** `diff_dumps.py` listed 2 flips (Galeotti, Calosso, death 2000 → 1912/1914). Their enwiki leads say "after 1912/1914" and GND gives Calosso †1962. The rule would have shipped two false-PD claims; it now applies to births only.
- **Root cause, not only the override.** The Wikidata item history showed the wrong "composer" occupation came from a 2016 bot batch built from the same list. That explained why `no_composer_occupation` missed the physicist, and gave the user two precise upstream edits, both verified via API afterwards.

## Friction

- **A merge in the handoff is not approval.** The classifier blocked `gh pr merge` and then a read-only `head` in the next call. The user approved one message later. Saved as memory `merge-needs-explicit-ok`: check CI, ask in one line, continue with independent work.
- **`sleep` before a check is blocked.** Waiting for a background job has to rely on the completion notification; one wasted call.
- **Rate estimate.** I first predicted ~2.5 h from the first 100 composers (4–5 s each); the heavy composers come first, and the run averaged 1.9 s per composer (1 h 49 min).

## Lessons for the workflow

1. Any rule that can change PD status (or another headline field) needs each changed row checked against a source before the PR, not only the gates. Gates passed on both r017 builds.
2. For a work item with a long network job, start the job first, write code during it, and run the new code on the previous dump to get figures early.
3. Splitting at the PR would have saved most of the late cost: items 6–8 ran at 230–284k context.
