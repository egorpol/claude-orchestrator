# Field report: Webern Op. 27 edition, MEI edition headers (2026-10-11)

Orchestrator: Claude Opus 5.5 (session `9b03e065`). Started after `/clear` from `camat_corpus_editions/webern_op27/HANDOFF.md` (local repo, no remote); the handoff's next step was "edition header per the profile". One item, one local commit: `e0ca024` (headers for the three movement files, CITATION.cff, profile and README updates). No delegates ran. The session ended with a short Q&A on the order of the next steps, recorded in the handoff. Machine-readable rows: [`token_usage.jsonl`](token_usage.jsonl), [`runs.jsonl`](runs.jsonl).

## Flow and cost

| # | Step | Outcome |
|---|---|---|
| 1 | Read handoff, memory, encoding profile, the old headers, DdT's header template | Spec: profile table + DdT template |
| 2 | Cropped the scan (PIL), stacked 8 footers and 6 page-number strips into 2 images | Paratext transcribed: "VARIATIONEN", numerals, "Anton Webern, Op. 27", copyright lines, plate number, printer "Waldheim-Eberle, Wien VII." on p. 11 |
| 3 | Authority and date checks on the web | Remembered GND 118630423 was **wrong**; Wikidata API (curl) gave 118629786, confirmed by Deutsche Biographie. DNB portal and lobid block bots (Anubis). Kern's "1934-1935" composition date was also wrong (IRCAM, Henle: 1935–1936) |
| 4 | Picker: funding, publisher/URI, DCMLab clef provenance, AI disclosure | 3 recommended options taken; the user chose the DdT DFG grant for projectDesc |
| 5 | One-off header generator in the scratchpad; text splice from file start to `</meiHead>` | Body diff: only `music/@decls` removed and `mdiv/@decls` added |
| 6 | RNG (lxml) | 1 error type: `application` takes `ptr` or `p`, not both; fixed |
| 7 | Full CAMAT gate (`camat.mei_validation`, scratch `saxonche`) | Passed, 0 findings |
| 8 | Verovio render of page 1 | Heading showed "Anton Webern / 1883–1945" from the Humdrum import's `pgHead`, not printed in UE; 3 probes showed Verovio 6.3 draws no heading from `meiHead` |
| 9 | Picker: pgHead + commit | pgHead now mirrors the printed head; gate rerun, 0 findings |
| 10 | Script cross-check of header values vs `sources.json`, `pages.json`, `scoreDef` | Kern and PDF hashes, page ranges, meters, `@decls` targets all match |
| 11 | Commit `e0ca024`, handoff + memory | Handoff offered at ~165k |
| 12 | User: "shouldn't I go over my edits for all pages first?" | Answer + order picker (breaks for II/III first, then the user's pass); recorded in the handoff |

Session up to this log: 59 calls, 6.70M cache read, 293k cache write, 62k output; context 36k → 177k. One call (the reply to the follow-up question) rewrote 141k of cache, half of the session's cache writes; the cause was not checked.

Quota at the end: claude-7d 24% (pace 0.9×; 26% at the end of the previous session), codex-7d 51% (0.9×), cursor-auto 64% (2.8×, unused).

## What worked

- **Last session's lessons held.** "Passes RNG" was not reported as "validates"; the full CAMAT gate ran before the commit and after the pgHead change. Edits were asserted text replacements, so the music bodies changed in exactly the two intended attributes.
- **A render check found what validation cannot.** The `pgHead` with Webern's life dates was valid MEI but contradicted the editorial policy (paratext belongs in the header, and these dates are not printed). Only rendering page 1 showed it.
- **Stacked crops.** Footers and page-number strips of 8 pages went into 2 images, so 2 image Reads instead of 14 transcribed all paratext.
- **A scripted cross-check of headline values before the commit.** Every hash, page range and meter in the new headers was compared by script with the repo's own source records and the files' `scoreDef`, as `rules/orchestrator.md` asks for data changes.
- **Three pickers, eight decisions.** The pgHead question went into the same picker as the commit, and the user's ordering question became a 3-option picker instead of a discussion.

## Friction

1. **An authority ID from memory was wrong.** The first draft would have used GND 118630423 for Webern; the real ID is 118629786. It was caught only because the data-change rule asks for a source check.
   - **Lesson:** never write an authority identifier (GND, VIAF, ISNI, Wikidata QID) from memory; look it up. When DNB (`d-nb.info`, `portal.dnb.de`) and lobid answer with an Anubis bot wall, the Wikidata API via curl with a User-Agent works (`Special:EntityData/<QID>.json`, property P227), and deutsche-biographie.de/<GND>.html is a WebFetch-able second source.
2. **Metadata inherited from a conversion is not a source.** Kern's `!!!ODT: 1934-1935` reached the old MEI header through Verovio and would have carried into the new `workList`.
   - **Lesson:** treat dates and names in imported headers as claims to check, like any other value.
3. **The first revision history was in the wrong order.** The draft listed movement I's hand edits (01:12) after the migration (02:42) because the script grouped changes by kind. Caught on rereading, before the run.
   - **Lesson:** build revision histories from timestamps (git log, file mtimes), not from categories.
4. **A sibling project's doc claim did not hold.** DdT's profile says "Verovio derives the first-page score heading from the metadata title". Removing the pgHead in this repo gave no heading at all with Verovio 6.3, whatever the header option or the title's `@type`. Three probes took one call.
   - **Lesson:** before relying on a renderer behaviour that a sibling doc states, probe it on the current file.
5. **The context guard fired on a Q&A follow-up.** The item was committed and the handoff offered at ~165k. The user's next question was about ordering, so it was answered with one picker and a handoff edit, and `/clear` was offered again. The log request came at ~177k.
6. **Nothing was delegated.** The item was design and data checks: header modelling, source verification, policy questions. The mechanical follow-ups are in the handoff for delegation: pb/sb for II and III, a validate.py wrapper, tempo `@mm`, fetch_sources.py. The user chose to run the pb/sb job first, before their own pass over all pages.
