# Field report: Webern Op. 27 edition, MEI 5.1 migration and docs (2026-10-11)

Orchestrator: Claude Opus 5.5 (session `7272c8d9`). Started after `/clear` from `camat_corpus_editions/webern_op27/HANDOFF.md` (local repo, no remote). The handoff's next steps were "choose how to get MEI 5.1" and "write the docs". The session did both, plus a repair pass that the docs work exposed. No delegates ran. Three local commits: `9572be9` (5.1 migration), `54e2992` (validation repairs), `ff66de8` (docs). Machine-readable rows: [`token_usage.jsonl`](token_usage.jsonl), [`runs.jsonl`](runs.jsonl).

## Flow and cost

| # | Step | Outcome |
|---|---|---|
| 1 | Read handoff and memory | Step 1 open: Verovio export vs downgrade transform |
| 2 | Cheap sample: bump `@meiversion` in a scratch copy, `xmllint --relaxng` against the pinned 5.1 RNG | I and III passed as-is; II failed in 2 places (`expansion` inside `choice/orig/reg`, `mRest@visible`) |
| 3 | Verovio round trip of movement I, plain and `{"basic": true}`, element counts before/after | Plain: still 6.0-dev and not well-formed (undeclared `xsi` on `madsCollection`). Basic: all 54 zones, most of the header and 87 IDs lost. The decision made itself; no question needed |
| 4 | `scripts/migrate_mei51.py`: asserted text edits, idempotent, 7 lines of diff | RNG passes; CAMAT facsimile model still resolves 143 zones |
| 5 | Picker: commit + next step | `9572be9`; docs chosen |
| 6 | Reading DdT's docs for the adaptation showed that CAMAT already has a full validator (`camat-validate-mei`, Schematron via `saxonche`) | Full run with a scratch `saxonche`: 5 broken `@startid`s (III), a one-note beam (Schematron error, II), an underfull pickup |
| 7 | Cropped the scan (PIL + image Read) for II bar 3 | The one-note beam is half of a cross-barline, cross-staff beam → `beamSpan`; also found a pitch error from Kern (grace G♭6, print G♮6) |
| 8 | Picker: print-error policy + commit | "Keep print, annotate"; `54e2992`; full validation passes |
| 9 | Wrote editorial policy, encoding profile, proofreading checklist, progress grid (42 systems × 11 layers) and log | The scan's footer ("Copyright renewed 1965") shows the base copy is a reprint from 1965 or later, not a 1937 copy; written into the policy |
| 10 | Picker: commit docs, editor names, `saxonche` install | `ff66de8`; handoff + memory refreshed at ~165k |

Session up to this log: 69 calls, 7.30M cache read, 154k cache write, 72k output; context 36k → 175k. Output is 25.7% of cost units, mostly the three docs.

Quota at the end: claude-5h 21%, claude-7d 26% (pace 1.0×), codex-7d 51% (0.9×), cursor-auto 68% (2.9×, unused).

## What worked

- **A cheap live sample settled step 1 without a question.** Bumping the version in a scratch copy and running the RNG showed the gap was 2 constructs in 1 file. Counting elements before and after a Verovio round trip showed what each export route would lose. The handoff framed this as a user decision; the evidence made it a non-decision, and the reply reported the comparison table instead of asking.
- **Targeted text edits instead of reserializing.** Regex edits with asserted match counts kept Verovio's formatting, so the migration diff was 7 lines and reviewable. An lxml rewrite would have touched every line.
- **Looking at the scan for a structural question.** A 600-pixel crop answered "what is this one-note beam?" and, as a side effect, found a wrong pitch. A crop of the page footer corrected the source description that the whole repo carried.
- **Three batched pickers, seven decisions.** Each call bundled the git step with the next design choice. The user took the recommended option every time it was marked.

## Friction

1. **"Validates" was claimed from the RELAX NG pass alone.** The 5.1 commit message and the reply said all three files validate. The handoff's "lean validation" wording led to writing a gate by hand (`xmllint`, then a failed lxml `isoschematron` attempt) instead of looking for an existing one. CAMAT, the library this edition is built for, already runs RNG, embedded Schematron, references, resources and hashes in one command. DdT's `validation-workflow.md` says so. The full run found 6 errors the RNG pass had accepted.
   - **Lesson:** before writing or running a validation gate, grep the project's own library and its sibling projects for an existing validator (`schematron`, `validate`), and say "passes RNG" rather than "validates" until the full gate has run.
2. **lxml cannot run MEI's Schematron.** MEI 5.1's embedded rules use XPath 2 (`eq`); `isoschematron` fails to compile them. CAMAT uses `saxonche`, its `validation` extra, which was not installed in the shared venv. A scratch install (`uv pip install --target <scratch>`, then `PYTHONPATH`) ran it without touching another repo's environment.
3. **The base source's real date sat in a note for two sessions.** `sources.json` already recorded "Scan also displays Copyright renewed 1965", but the README, the repo name and the IDs say "UE 1937". It surfaced only when the docs needed a source description.
   - **Lesson:** when a data note qualifies a headline value (here, the date of the base source), check that it reaches every document that states the value.
4. **A doc example used a real ID with an invented fact.** The first `annot` example in the editorial policy pointed at a real note with a made-up row argument. It was caught on rereading and replaced with a fictional ID.
   - **Lesson:** examples in docs must use clearly fictional IDs when their content is invented.
5. **zsh: an unquoted glob that matches nothing aborts the command.** `grep -rn … --include=*.py` failed with "no matches found" because zsh expands `*.py` before grep sees it.
   - **Rule for `rules/orchestrator.md` (Shell):** quote globs meant for the called program (`--include='*.py'`); zsh's `nomatch` aborts the command when a glob matches nothing.
6. **Three items in one session, ending at 175k.** The 5.1 migration was one item; the docs were a second, started at about 70k after the user picked "Docs". The repairs found during the docs work were a third. Reading two 15 KB DdT docs in full and writing three long docs drove the growth. The handoff was written at ~165k, after the docs, not at the item boundary between migration and docs.
   - **Lesson:** at an item boundary, even under 120k, estimate the next item's size. A docs item that reads 30 KB of templates and writes 25 KB of prose will not fit in the remaining room; hand off first.
7. **Nothing was delegated.** Claude's 7-day pool ended at 26%, the scarcest, while cursor-auto sat at 68% with pace 2.9×. The work was mostly design and data checks, which stay with the orchestrator. Three mechanical follow-ups were identified and specified in the handoff for delegation: a `validate.py` wrapper, `pb`/`sb` generation from `pages.json`, and `tempo/@mm` values.
