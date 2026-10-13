# Field report: CAMAT upstream issues, Verovio and MEI (2026-10-13)

Orchestrator: Claude Opus 5.5 (session `6a4f5d31`). Started after `/clear` from `camat_v2/pdf-workflow-test/HANDOFF.md` with "add the links to the issues I shall post". One item: three upstream texts drafted in the previous session (`pdf-workflow-test/upstream/DRAFTS.md`), re-checked, posted by the user one at a time. No delegate runs, no code changes, nothing committed (the folder is gitignored). Result: rism-digital/verovio#4492 and music-encoding/music-encoding#1846 posted by the user; a comment on verovio#4116 paste-ready. Machine-readable row: [`token_usage.jsonl`](token_usage.jsonl).

## Flow and cost

| # | Step | Outcome |
|---|---|---|
| 1 | Add post-at and cited-issue links to DRAFTS.md; look up all 5 cited issues with `gh api` | Titles, states, dates matched the drafts; #729 is a PR (merged 2021-04-29, same day #473 closed) |
| 2 | "Reassess #1 before posting": PyPI/GitHub release, develop commits on the touched files, dev schema build date, duplicate search, fresh conversion, validation, expansion tests | All 3 errors reproduce on 6.3.0; no newer release or schema build. **New:** #1918 comments from 2025-11 already call `choice/orig\|reg` invalid and suggest `app/lem\|rdg` |
| 3 | Root cause from Verovio source (`expansionmap.cpp#L116`, `doc.cpp` expand defaults); tested the `app` variant and the fully fixed file | `app` validates but Verovio can't expand it either; siblings are the only form that validates and works. Draft #1 rewritten accordingly |
| 4 | User: add a picture? Rendered the example (Verovio → SVG → `rsvg-convert`), looked at it | 900×331 PNG, image slot in the draft |
| 5 | User pasted the post: picture too big | `gh api markdown` (mode gfm) probe: newlines render as `<br>` in issues. Unwrapped body, `<img width="450">`, removed a visible `#NNNN` placeholder; word-level diff against the user's text: same words |
| 6 | User posted #1 → verovio#4492. "Reground #2–3" | Posted body diffed against the paste-ready file: identical |
| 7 | Read every cited thread in full (MEI #473, #729, #619, #672, #950, #792; Verovio #4116, #1870); RNG traversal for `att.enclosingChars` members; validated 12 encodings in both schemas | 4 errors in the draft (below); proposal moved from `fb` to `harm` + `f` after the MEI maintainer's reply in #619 |
| 8 | Picker with XML previews: harm vs fb | User chose `harm` + `f`, with one sentence leaving `fb` open |
| 9 | Context guard at ~161k: same item, so handoff refreshed in place | HANDOFF.md, memory, DRAFTS.md updated |
| 10 | User posted #2 → MEI #1846; filled the number into the #4116 comment | Diffed posted body: identical; links checked via `gh api markdown` |

Session up to this log: 76 calls, 8.31M cache read, 165k cache write, 82k output; context 37k → 187k; 1.57M cost units (cache read 52.9%, cache write 21.0%, output 26.1%). No idle rebuilds.

Quota at the end: claude-5h 86%, claude-7d 98%, codex-7d 93%, cursor-auto 59% (start not measured).

## What worked

- **Re-checking a day-old draft right before posting paid off twice.** Draft #1 missed recent comments on the issue it cited (#1918). Draft #2 had 4 errors: a workaround quoted with the wrong content (`(<rend>f</rend>)` instead of the posted `(<rend>sffz</rend>)`), an untested claim about what tools read ("( f )"), a maintainer's reply to the user's own comment (#619, same day) not taken into account, and missing history (Verovio #1870, kepper's review on #729). Search results alone would not have shown any of these; reading each thread in full did.
- **`gh api markdown -f mode=gfm -f context=OWNER/REPO -F text=@body.md` as a pre-post probe.** One call showed that hard-wrapped Markdown renders with `<br>` in issues, confirmed `<img width>` survives, and listed every auto-linked reference with its target repo. Used again for each later text.
- **Diffing the posted body after each post** (`gh issue view N --json body`, word-level diff against the paste-ready file). Two posts, both identical; the status notes could then say "posted" without hedging.
- **Claims tied to source lines.** The expansion bug got a root cause (`plist` lookup scoped to the expansion's parent) instead of a symptom list, and "Verovio supports `@enclose` on dynam only" came from grepping the 6.3.0 headers, not memory.
- **Maintainers' suggested encodings validated before citing them.** pe-ro's three `harm`/`supplied`/`fb` variants from #619 were each run against MEI 5.1 and dev; all three of his statements held.
- **Picker with XML previews for the one design choice** (`harm` vs `fb`). One answer, no back-and-forth.

## Friction

1. **A `cd` in a compound command moved the shell's working directory** to `pdf-workflow-test/upstream/`. Nothing broke until the end: `delegate cost --session latest` derives the project folder from the working directory and failed with "no project dir …-upstream". Fixed by passing the transcript path.
   - **Lesson:** the rule already exists ("don't `cd` away"); it was broken for convenience (relative `-F text=@file`). Use absolute paths in `-F text=@/abs/path`.
   - **Tool suggestion:** `delegate cost --session latest` could fall back to the git root's project folder, or to the newest transcript across all project folders, when the working-directory folder does not exist.
2. **The first RNG traversal over-reached.** Following every `ref` reported 326 elements with `@enclose`, because element content models reference child elements. Caught only because the number was implausible; the fix was to follow `mei_att.*` refs only (17 elements, same in 5.1 and dev).
   - **Lesson:** sanity-check an automated schema derivation against an expected size or a known member/non-member before using it.
3. **Verovio's Python binding has no `enableLogToBuffer`**, and `toolkit.getLog()` returned nothing with logging to stderr. Warnings were captured by redirecting fd 1/2 to a temp file around `loadData` + `renderToTimemap` (the `Captured` class in `camat_v2/pdf-workflow-test/upstream/recheck-verovio-humdrum.py`).
4. **Default image size for issues.** The 900 px PNG displayed at full width; the user noticed. **Lesson:** for GitHub issue images, render at 2× and set `<img width>` to half the pixel width.
5. **Status-update prompts during research phases**, 5 times, each during multi-call reading (thread reads, schema probes). Same pattern as the 2026-10-12 report; one line before each read phase would avoid them.
6. **The context guard fired at ~161k inside the item.** The handoff was refreshed in place and the item continued to its end at ~187k, as the rule intends. The user then asked for this log before `/clear`.
