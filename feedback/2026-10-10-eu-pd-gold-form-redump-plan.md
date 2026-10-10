# Field report: gold-set annotation form, annotation support, r017 redump plan → eu-pd-composers (2026-10-10)

Orchestrator: Claude Opus 5.5 (session `96cb4576`). Started from `assessments/HANDOFF.md` (guide the user through gold-set annotation). The user picked the web form over TSV, then annotated in it while asking rule questions, and ended with a redump plan. No delegates ran. Machine-readable row: [`token_usage.jsonl`](token_usage.jsonl), incident in [`runs.jsonl`](runs.jsonl).

## Flow and cost

| # | Item | Calls | Cache read | Context at end | Outcome |
|---|---|---:|---:|---:|---|
| 1 | Walkthrough: build and publish the claude.ai form (db store, owner-only rules), research 4 practice items, worked answers | 36 | 3.6M | 146k | Artifact v1, store probe passed |
| 2 | Rule question (harp + string orchestra) + "keep the form in the repo, buildable locally" | 32 | 5.8M | 202k | `scripts/gold_form.html`, `gold_set.py form`, local mode, 2 tests; 2 CSS bugs found by headless Firefox; artifact v2 |
| 3 | Push/PR + parse the user's first saved answers | 8 | 1.7M | 215k | `import-form` + test; PR #14 |
| 4–5 | Two rights-basis questions (file Copyright line vs categories, `us_only`) | 8 | 1.7M | 223k | guide + form labels clarified; artifact v3, v4 |
| 6 | Recheck practice + first 5 composers; GND as a bare number | 6 | 1.4M | 233k | GND ids verified via lobid; bare-number convention; artifact v5 |
| 7 | "GND link in the viewer next" | 3 | 0.7M | 235k | handoff note |
| 8 | Wrong identity (Jean Perrin → physicist) | 10 | 2.4M | 247k | root cause in the Wikipedia list; decided case in the guide |
| 9 | Feedback assessment + redump plan (4 questions, all recommended options taken) | 7 | 1.8M | 270k | r017 plan in the handoff, new memory |
| 10 | This usage dump | 3+ | 0.8M+ | 274k | — |

Session total: 113 calls, 19.8M cache read, 250k cache write, 146k output; context 36k → 274k. Quota: claude-7d 48 → 45%; codex-7d (56%) and cursor-auto (71%, pace 2.9×) untouched.

Items 2–10 ran at 146–274k context: 16.2M cache read. Fresh sessions per cluster (build, annotation support, planning) would have started near 40k each; a rough estimate is 4–5M for the same work, so about 11M (55% of the session) went to carrying context. That makes four sessions in a row where item sprawl is the largest cost.

## What worked

- **An Artifact form with a db store as the annotation tool.**
  - The user annotated in it within minutes.
  - `ArtifactData list --out_dir` saved one JSON file per item, and `gold_set.py import-form` turned those into the TSV sheets in a repeatable command.
  - Owner-only write rules were checked with a probe document: write, read, a read at `as_level: view` (empty), then delete.
- **Blindness kept while helping.**
  - Rule questions about sheet items were answered from the user's description, without opening the item.
  - The recheck of the user's composers verified only their cited GND records, never dump values.
  - Planning numbers were aggregates only.
  - One unblinding hazard was found and fixed: the guide said "score anytime", but scoring a handful of items shows per-item agreement with the dump.
- **Headless Firefox as the fallback preview.** `firefox --headless --screenshot` of the local build caught a class collision: the status class `empty` matched the empty-list style `.list .empty`, which ballooned every status dot. It also showed stretched option cards. The artifact preview tool wasn't available, and v1 had already shipped with the bug.
- **Quantify before asking.** Two scratch scripts fed four `AskUserQuestion` choices, and the user took every recommended option without follow-up:
  - authority-ID coverage from the entity cache: GND 89%, VIAF 97%, none of the four 3%;
  - list-row years vs Wikidata years: 11 rows off by 10+ years, while the existing occupation flag caught only 1.
- **Each rule clarification shipped where the user looks.** Every answer became a "Decided cases" line in the guide, a form label or help text, a rebuild, a republish and a small commit on the open PR. Open tabs updated without losing state.

## Friction

1. **Session sprawl, fourth time.** The build item ended at 146k. The annotation-support Q&A and the planning then ran in the same session up to 274k. The Q&A did benefit from shared context (form internals, rules), but that fits in a page of handoff.
   - **Proposal (SKILL.md / CLAUDE.md):** when a build item turns into interactive support (the user starts using the tool and asking questions), write a "support mode" handoff and offer `/clear`. The handoff covers tool URL, store layout, decided cases and the blindness rules. Fold the repo-integration request into the next session rather than starting it at 200k.
2. **First publish without a look.** v1 shipped with two layout bugs because no `ArtifactCheck` existed, though headless Firefox did.
   - **Proposal:** when no preview tool exists, take one `firefox --headless --screenshot` of a local copy before the first publish. The artifact contract's "one look" rule allows it.
3. **Same confusion twice.** The rights questions on W002 and W004 both came from reading the file box's Copyright line instead of the work's categories. The first fix changed only the help text; the second also fixed the `us_only` label.
   - **Lesson:** when a rule question shows a misleading source on the page, fix every label that can mislead in the same pass.
4. **API assumptions corrected late.**
   - `user.isOwner()` returns a Promise.
   - `window.claude.hot` isn't in the type definitions.
   - Both were caught by reading the `.d.ts` files after writing the page. Read the types before writing capability code.
5. **Small slips.**
   - The Bash tool runs zsh, not the user's fish shell. A fish-style `(cmd)` substitution failed with an `(eval)` error.
   - A test regex matched the wrong JS block (`SHEET_COLS` instead of `FIELDS`). Slicing between named markers fixed it.
6. **No delegation, though cursor-auto had 71% left at 2.9× pace.** The tests from spec and the practice-item research were candidates. The tight UI loop (edit, rebuild, screenshot, republish) suited inline work.
