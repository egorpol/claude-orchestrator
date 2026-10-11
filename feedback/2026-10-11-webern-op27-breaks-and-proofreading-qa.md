# Field report: Webern Op. 27 edition, page and system breaks, remote, proofreading Q&A (2026-10-11)

Orchestrator: Claude Opus 5.5 (session `12c68dfa`). Started after `/clear` from `camat_corpus_editions/webern_op27/HANDOFF.md`, whose next step was "pb/sb for II and III from `pages.json`". One build item, then a run of support questions during the user's own proofreading pass. Six commits, all pushed to a new private remote (`egorpol/webern_op27`) at the user's request: `e0b5f26` (breaks), `bd81164` (`sb/@type="z"` dropped), `318f772` (repo URI in headers and CITATION.cff), `7fbc93f`, `8be37df` and `40a4429` (`docs/upstream-issues.md`). No delegates ran. Machine-readable rows: [`token_usage.jsonl`](token_usage.jsonl), [`runs.jsonl`](runs.jsonl).

## Flow and cost

| # | Step | Outcome |
|---|---|---|
| 1 | Read handoff, memory, `pages.json`, movement I's breaks, II/III section layout | The Kern breaks already sat at the printed bars, except III bar 43 (Kern `!!LO:LB`, print starts p. 10) |
| 2 | `scripts/set_breaks.py`: remove all `pb`/`sb`, rewrite from `pages.json` in movement I's form; dry run on scratch copies | I unchanged (the generator reproduces the hand-made model); second run idempotent; III bar 43 break placed before its `scoreDef` |
| 3 | editorialDecl, `revisionDesc`, profile and checklist edits; full CAMAT gate; Verovio systems per page | 0 findings; systems per page I [4,5,5], II [5], III [4,5,5,5,4] = the print |
| 4 | Picker: commit; user asked what `type="z"` means | Web check: Humdrum layout group name, arbitrary (`g=original` is Sapp's convention); user chose to drop it everywhere |
| 5 | Private GitHub repo (user request), repo URI in headers + CITATION.cff | `cffconvert --validate` passes; pushed |
| 6 | Q&A: where facsimile images load from | Relative `graphic/@target`, local gitignored PNGs |
| 7 | Q&A: denser rit. dashes | `@lsegs` = printed dash count (11, not 5); 5 render variants: Verovio 6.3 ignores `@lform`/`@lsegs`/`@lwidth` |
| 8 | "Is Verovio latest, maybe a bug?" + start an upstream-issues doc | 6.3.0 is the latest release; source read of develop: hard-coded dash pattern, "can be made a parameter" comment; `docs/upstream-issues.md` |
| 9 | Q&A: bar 21 dim. hairpin; applied on request | Same `@staff`/`@place` for hairpin and `p` makes Verovio link them; 4 variants measured in SVG |
| 10 | Q&A: bars 22–23 beamSpan, twice | First: E stem down puts the beam between staves (applied). Second: still 2 strokes; minimal beam vs beamSpan files show a Verovio beamSpan bug with mixed stems; logged as entry 2a |
| 11 | This log | — |

Session up to this log: 121 calls, 15.2M cache read, 370k cache write, 108k output; context 37k → 211k. Quota: claude-7d 24% → 20% (pace 1.0×), cursor-auto 64% → 62% (2.8×, unused), codex-7d refilled to 100% this morning (openusage data 8 h old).

## What worked

- **Running the generator on the hand-made example first.** `set_breaks.py` reported movement I "unchanged", which proved it writes exactly the user's model before it touched II and III. The `pages.json` cross-check (every bar listed once, every zone on its page's surface) found the single real difference from the Kern layout.
- **Render checks as acceptance tests.** Counting `class="system"` per page against `pages.json` gave a one-line pass/fail for the whole layout change.
- **Minimal examples to split "encoding" from "renderer".** The beamSpan question took three tiny MEI files (beam, beamSpan in one bar, across a barline). The difference showed in one image, and the file went straight into the upstream issue draft.
- **Reading the renderer's source to answer "is it a bug?"** One `gh api` fetch of `view_control.cpp` on develop showed a hard-coded dash pattern with a comment inviting a parameter, which turns it into a feature request.
- **The user's working copy stayed theirs.** Their uncommitted movement I edits were never staged. MEI changes were made only when the user said "add it" or picked "apply". Every commit and push went through a picker.

## Friction

1. **The session ran to 211k on support questions.** After the build item was committed and pushed, the user kept asking proofreading questions. The context guard fired at ~125k and ~204k, and `/clear` was offered four times as a closing line, but each new question was answered in place. The handoff was refreshed after every answer, so nothing would have been lost.
   - **Lesson:** once Q&A support starts after a finished item, put "clear and continue from the handoff" into the picker that closes the first answer, as a real choice, instead of a closing sentence the user can scroll past. Each renderer probe at 150k+ context costs 5–8 calls of full re-reads.
2. **A handoff claim about the renderer was wrong.** The handoff said "Verovio 6.3 preserves but does not draw `beamSpan`". It does draw cross-staff beamSpans; the real fault is narrower. The first upstream-issues draft carried the claim as "to recheck", which was the right hedge, and it was corrected the same session.
   - **Lesson:** when a renderer limitation goes into a handoff or doc, keep the minimal example that shows it next to the claim.
3. **Probe scripts needed retries.** SVG parsing for positions missed nested `<g>` groups twice, a crop missed its target once, and `spacingLinear=2.0` was out of range (maximum 1.0). That made four extra calls at high context.
   - **Lesson:** for Verovio position checks, read `transform="translate(x, y)"` from `<use>` inside the element's group; render small excerpts with `select({"measureRange": ...})` and crop from the element's coordinates, not by eye.
4. **Nothing was delegated.** The handoff had marked the pb/sb job as delegate-able. It was done inline: 28 break lines and one script, where placement inside nested sections and before a `scoreDef` needed judgement. Cursor Auto kept 2.8× spare pace. The remaining mechanical items (validate.py wrapper, fetch_sources.py) do not touch the MEI and can run during the user's pass.
