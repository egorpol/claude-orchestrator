# Field report: audiospylt assessment, revision plan and Phase 0 (2026-10-10)

One Claude Code session (`9a59a1f6`, Opus 5.5, VS Code extension) on `egorpol/audiospylt`: assessment of a software
paper after mixed ICMC 2026 reviews (code audit, paper critique, competitor survey), web literature research, a
revision plan built from the user's answers to 8 multiple-choice decisions, then Phase 0 of the refactor (CI and a
characterisation test suite) committed and pushed. Per-run records are in [`token_usage.jsonl`](token_usage.jsonl),
incidents and lessons in [`runs.jsonl`](runs.jsonl).

## Where the tokens went

| Who | Tokens | Notes |
|---|---|---|
| **Claude orchestrator** | **21.5M total**: 20.9M cache read (97%), 439k cache write, 134k output, 212 fresh input; 99 calls | **$9.07** API-equivalent (openusage). Context per call grew from 36k to 319k; the context guard fired at 261k |
| Codex gpt-6.1-sol @xhigh, 2 runs | 314k input, 6.90M cached, 71k output (26k reasoning) | `audit-code` 12.8 min (ran 11 notebooks, accuracy script, ruff, wheel build); `p0-tests` 16.2 min (130 tests, 3 Python versions) |
| Cursor auto, 2 runs + 1 review | 211k input, 1.24M cached, 38k output | `paper-critique` 2.8 min (read-only, paper inline in brief); `p0-ci` 3.3 min; review of `p0-tests` 5.1 min |

Quota snapshots (`delegate quota`), first ≈13:48 local and last ≈14:35: Claude 5h 89 → 67 %, Claude 7d 57 → 54 %,
Codex 5h 100 → 76 %, Codex 7d 60 → 56 %, Cursor auto 71 → 71 %. The first Codex reading was 249 min old, so the
Codex deltas are approximate. Cursor auto did not move a visible point for two implementation-size runs plus a review.

**Comparison:** $9.07 for assessment + plan + Phase 0, against $53.67 for the 2026-10-09 marathon. Still three work
items in one session; the last ~20 calls each re-read ~300k tokens.

## What worked

- **Split by what needs network.** Delegates have no network, so the orchestrator did the web part itself (~30
  WebSearch/WebFetch calls: competitor status, citations, venue facts) and gave delegates prefetched text:
  `pdftotext` of the paper and the reviews, inline in the Cursor brief (6.7k words) and as scratchpad paths for Codex.
  A PDF fetched with WebFetch arrives as binary; `pdftotext` on the saved file recovered it.
- **Orchestrator-prepared environments.** Three `uv` venvs in the scratchpad (Python 3.11 / 3.12 / 3.14) let
  `--verify` run the suite on all three. This exposed that 3.11 resolves librosa 0.11 and 3.14 librosa 1.0, so the
  brief required tolerances that survive both. GitHub CI later passed on 3.11–3.14 on the first push.
- **Strict-xfail tests as the bug ledger.** The brief listed each verified bug with file:line and the *wanted*
  behaviour; Codex wrote one strict xfail per bug and a golden-master suite for the rest. A fix flips its test.
- **Cross-vendor review** confirmed every xfail fails for the documented reason and found no vacuous tests.
- **Decisions via AskUserQuestion** (3 rounds, 10 questions) produced a plan the user could steer quickly.

## Incidents and lessons

1. **CRLF/mixed line endings break `delegate apply`.** The target repo was committed partly from Windows (LF, CRLF and
   mixed files). The `p0-ci` patch failed with `git apply` and with `--3way`. Workaround: confirm the main-tree files
   equal the base, then copy the changed files from the worktree. Fixed in the repo with `.gitattributes`
   (`* text=auto eol=lf`) plus `git add --renormalize .` as its own commit; notebooks were excluded (`*.ipynb -text`)
   so 40 MB CRLF notebooks were not re-added to history.
   **Proposal:** `launch` warns when the base has CRLF/mixed text files; `apply` falls back to a file copy when every
   touched main-tree file still equals its base blob.
2. **Copy-merged and read-only runs are invisible to `delegate cost --repo`.** Only `p0-tests` (merged with `apply`)
   reached `runs.jsonl`; `p0-ci` (copy-merged), both read-only runs and the review are missing, so the repo summary
   shows 1 of 5 runs. **Proposal:** log usage when a run or review finishes (or on `cleanup`), not only on `apply`.
3. **A wrong acceptance criterion in the brief survives review.** The brief asked for morph pairings that preserve
   Σamp². Codex encoded exactly that, and the review checked compliance with the brief and approved. On its own read
   the orchestrator saw the domain error: duplicated partials start phase-coherent, so amplitudes add at the onset,
   and satisfying Σamp² would make onsets ~10·log10(k) dB louder. Fixed by marking the test provisional and adding a
   decision to the plan. **Lesson:** review compares the patch to the brief, so it cannot catch errors in the brief.
   For domain criteria, add a review focus such as "challenge the brief's acceptance criteria", or put the criterion
   to the user first.
4. **Audit scratch inside the worktree swamps the exit packet.** `audit-code` wrote copies of 11 notebooks under
   `.audit/`, so the packet reported 1016 files / +18.3M lines. The report itself was fine. **Proposal:** the research
   template tells delegates to keep scratch under `.delegate/` (already excluded).
5. **Codex's sandbox blocks Jupyter kernel sockets.** `nbclient` fails with `PermissionError`; running extracted cells
   with the venv's `ipython` works offline. Worth a line in SKILL.md for notebook-heavy repos.
6. **zsh again.** `for f in $files` did not split (CLAUDE.md already warns); a `while read` loop over a file list worked.
7. **The orchestrator deleted a pre-existing gitignored `build/` directory** as cleanup after a local
   `--no-isolation` build, without looking at it first. It held only regenerable setuptools output, but cleanup
   should only remove directories the session itself created.

## Session-shape note

The session spanned ~03:05–14:35 local, with long idle gaps while the user answered questions. Assessment, planning
and Phase 0 were three work items. The handoff (`revision/HANDOFF.md` plus a memory file) was written only at the end
of Phase 0. Writing it after the plan, before starting implementation, would have kept Phase 0 in a ~40k-context
session.
