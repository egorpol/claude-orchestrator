# Field report: CAMAT PDF facsimile module, Plan A step 2a (2026-10-12)

Orchestrator: Claude Opus 5.5 (session `9ce4e364`). Started after `/clear` from `camat_v2/pdf-workflow-test/HANDOFF.md`, next step "Plan A step 2, library promotion". One item: the new module `camat.pdf_facsimile`, specified by a user guide written in the previous session. One delegate run (Codex `gpt-6.1-sol` @xhigh, run `pdf-facsimile`), one Cursor Auto cross-review, one commit: `771d8c8` on `feature/pdf-facsimile` (not pushed). Machine-readable rows: [`token_usage.jsonl`](token_usage.jsonl), [`runs.jsonl`](runs.jsonl).

## Flow and cost

| # | Step | Outcome |
|---|---|---|
| 1 | Read handoff, memory, the guide (15 KB spec), the study scripts and CAMAT's detector/integrator code | API fixed by the guide; open details listed |
| 2 | Probe: ElementTree parse with comments/PIs + `tostring` without re-indent, on the 3 edition MEIs and 15 test-corpus files | Edition files byte-identical; others differ only in in-tag spacing (`<x/>` → `<x />`). Settled "stdlib only, no global re-indent" in the brief with evidence |
| 3 | `delegate route implement` | Claude 7d at 7%; Codex chosen over the routed Cursor Auto for a data module (quality 0.91 vs 0.76) |
| 4 | Built a real-data acceptance fixture from the edition's own files (9 pages, new-schema `pages.json`, original detector boxes, MEI with facsimile stripped) plus `expected.json` and `check.py`, by a path independent of the module | Expected: 143 regions, 54/23/66 measures, 3/1/5 surfaces, 14/5/23 breaks, zone boxes and break positions equal to the edition's |
| 5 | Brief: guide as contract, 8 settled details (ID scheme, review statuses, return types, pairing rule), 8 adversarial cases, owned/off-limits files | Launched in the background with `--link .venv --link <fixture>` |
| 6 | While Codex ran: 1-call probe for the next item (6 Verovio 6.0-dev files vs the 5.1 RNG) | 5 of 6 pass after the version bump; the 6th fails only on the 2 constructs an existing script already rewrites. Next item scoped |
| 7 | Handoff written; picker "clear or stay" at ~150k | User chose to stay |
| 8 | Codex finished in 16 min: +1,587 lines, 48 tests, acceptance 0 failures, 9/9 render hashes | verify FAIL rc=2, only because the verify command's bare `pytest -q` collected `tests/release` (needs `CAMAT_REPO_ROOT`) |
| 9 | Re-verify with `--ignore=tests/release` (455 passed) and Cursor review, in parallel; read the whole 887-line module meanwhile | Review APPROVE, 2 medium (both about the verify command), 1 low (stale labels); own read found 1 cosmetic whitespace bug |
| 10 | Applied; fixed both bugs inline (~15 lines, 2 tests), aligned 4 guide sentences, CHANGELOG, strict docs build | 50 tests, acceptance 0 failures, render hashes equal |
| 11 | Picker: git + next item | Feature branch, Plan A commit only (changelog staged without the unrelated viewer entry via `git hash-object` + `update-index`); `/clear` before step 2b |

Session up to this log: 56 calls, 8.03M cache read, 186k cache write, 70k output; context 37k → 211k; 1.53M cost units (cache read 52.6%, cache write 24.4%, output 23.0%). No idle rebuilds.

Quota: claude-7d 7% → 6% (pace 0.5×), claude-5h 60% at the end, codex-7d 93% at the end (`route` showed 72% for the Codex pool at the start), cursor-auto 60% → 60% (3.0×).

## What worked

- **Acceptance data from an independent path.** The expected values came from the edition's MEI, not from the module or the study code it ports. Codex passed all checks in round 1; no fix round. The script also served as the delegate's debug loop.
- **A 1-call probe turned a design question into a brief constraint.** "Does ElementTree reformat hand-edited MEI?" was answered before the brief, so the brief could state the invariant ("no global re-indent; these three files round-trip byte for byte") instead of a mechanism.
- **Settling the spec's open details in the brief.** ID scheme, statuses, return types and the split-measure pairing rule were decided once, here. Codex followed them literally, and the 8 listed adversarial cases each got a test.
- **Two reviewers found different bugs.** Cursor found the stale-labels gap (integration trusted `alignment.json` labels after `pages.json` edits); the orchestrator's full read found a whitespace bug when a trailing break is removed. Neither found the other's.
- **Inline fixes instead of a followup round.** Both fixes were under 10 lines each; SKILL.md's rule held.

## Friction

1. **The verify command did not match CI.** `--verify` used bare `pytest -q`; the project's CI runs `pytest tests --ignore=tests/release`. The run reported verify FAIL, and both medium review findings were about that command, not the code.
   - **Lesson:** copy the test command for `--verify` from the project's CI workflow (`.github/workflows/*.yml`), not from memory.
2. **The brief narrowed the spec without updating it.** Two guide sentences were broader than the brief (a surface for every music page; every `pb` gets `@facs`). The brief said "flag contradictions", Codex did not, Cursor did, and the orchestrator then edited the guide after the build.
   - **Lesson:** when a brief deliberately narrows the spec document, edit the spec in the same step, before launch.
3. **Wait estimate too long.** The picker said "30–90 minutes"; Codex @xhigh delivered a 1.6k-line module with tests in 16 min. Data point for future estimates: a well-specified module of this size is a ~15–20 min Codex round.
4. **The session stayed past 120k.** The item boundary picker at ~150k offered `/clear`; the user chose to stay for the review. The session ended at 211k, inside the same item, which is the intended use.
5. **Status lines between long read phases.** The harness prompted for a status update four times, each during multi-call read phases (spec + prior art, patch review). One line before each read phase would have avoided it.
