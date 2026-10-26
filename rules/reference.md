# Topic tips

Narrow tips from the claude-orchestrator field reports, read on demand: the session-mode hook names this file after the session rules (`hygiene.md`), which it injects at session start. Read the matching section before that kind of work.

## Handoffs and items

- **When `/clear` pays:** at an item boundary, a fresh session (~35k after reading the handoff) is cheaper when (context − 35k) × the item's expected calls exceeds ~1M cache-read tokens. A review, fix and commit item runs ~45–50 calls, so from ~55k up a fresh session costs less. Put the estimate in the picker option.
- **Item size:** five sessions (2026-10-14 to 10-17) each ended one item or one typed prompt at 241–328k; one prompt ran from 38k to 266k, and a step that wrote ~60 KB of new code reached 203k.
- **Waits:** six sessions (2026-10-13 to 10-20) paid 10–18% of their cost units for one rebuild after a 61–281 min wait, three of them on `/field-report`. Half of all rebuild cost came from runs that finished after an hour.
- **Sweeps:** one "check everything" request ran from 110k to 240k within a single prompt, with about 30 image reads.
- **Hand-paste loops:** a user posting prepared texts one by one is a new item. Make "`/clear`, then `/next`" an option in the picker that closes the first answer.
- **Handoff size:** two 29–31 KB handoffs, mostly done-item history, cost ~8k tokens per resume.
- **Gates in handoffs:** list each gate with its interpreter and env, plus a one-line scratch-venv recipe; a file regenerated with provenance names the env that produced it (two sessions lost 6 calls and a 5 min rerun between them). A full `pytest -x` once stopped at test 188 of 531 on a known failure, so give the ready `--deselect` flag. `delegate` is on PATH only after `install.sh --bin`, so write its full path.
- **Cross-repo notes** go into the target repo's `HANDOFF.md`. Before editing another repo's handoff, check its mtime, re-read the anchor and replace one line.
- **Handoff steps as hypotheses:** mark a step taken from an external audit "from audit, unverified against neighbouring rules". A handoff's "watch for X" is a hypothesis: run the generic check, not only X.
- **No handoff:** a "continue from the handoff" that finds no file costs the next session several calls of transcript digging.
- **Status lines:** all 9 harness status requests in three sessions (2026-10-13/14) came at the points the session rules name: runs of calls without text, unexpected results, calls that may run over a minute.
- **Destructive git steps** run in their own Bash call: bundled with other edits, the auto-mode classifier denied the whole call.

## Decisions

- A fill policy picked without a measurement made one file 59× larger at build time and needed a new picker.
- Ask an unstated quantity that shapes the plan (session length, audience, budget) before writing, not as an "assumption to confirm" afterwards: one outline revision took 15 edits.
- "Other" in a picker came back as a bare selection with no text, so an option that needs typed input fails.
- **Vague reports:** when the first check shows nothing wrong, show it and ask what is missing before widening the test matrix. Ten calls across browsers, widths and themes once failed to reproduce a complaint that was about taste.

## Delegation evidence

- **Spare quota:** Codex 7d peaked at 17/30/51/32/4/7/28/14% used over its eight weekly windows to 2026-10-26; Cursor Auto used 54% of the month before. Claude 7d ran at 97–99% used in both Pro weeks; on Max (since 2026-10-26) Claude may no longer be the scarcest pool. Codex 5h can be full from the user's own Codex Desktop use while 7d looks spare, so check 5h at dispatch.
- **Second opinions:** in uses through 2026-10-20 the two models mostly picked the same option, and most rounds added a risk or corrected a fact in the question. One risk note contradicted a fact measured minutes later, hence the cheap checks first. Answers took 20 s to about 5 min, and about 6 min for a review at `--effort high`. One session (2026-10-21 to 10-26) deferred a public-API second opinion "because of context size".
- **Web facts:** Cursor's ask mode has no web tools, and Codex's web tool in `ask` reads only a cache; live search needs `delegate launch codex --web-search`.

## Data checks

- Keep the minimal example next to any tool-behaviour claim you write into a doc or handoff ("the renderer draws X"), probed on the current file.
- Look for an existing validator in the project and its libraries before writing one. Before writing a script that drives a fixture, read the fixture's own check script for paths and setup.
- Check an example meant for others in the tool they will use: the maintainers' online editor and schema version, a fresh venv with only the shipped install steps, a real JupyterLab (nbclient emulates widgets). A post checked only against the project's own schema cost ~16 calls and a round trip.
- A change to scoring, ranking or keyword weights gets a before/after run on real inputs the same day: 53 unit tests passed while 6 off-topic posts jumped to the top tier. Check a UI fix at every variant its inputs depend on (widths, themes): a CSP hash of an injected style held only at 1280 px.
- Derive UI-test fixtures and expected values from a data probe in the same turn. When porting thresholds between repos, re-derive them on both repos' data.
- Before adding a check on a manifest or config field, grep every rule that reads the field and confirm all of them can pass on one fixture. In an audit, ask "can this gate pass at all once fixed?".
- **Positive controls:** Verovio's `getLog()` is never filled through the Python binding, so "no warnings" from it proves nothing. Prove a warning probe on an input that does warn.
- **Fixtures from external tools:** run the export twice and byte-compare before using it as a fixture.
- **Headless browser checks:** disable the cache, scroll the target into view, fail on console errors, and assert one rendered property (a dark-pixel share, a screenshot) beside the DOM state: a blank scan passed every DOM check.
- **Render and export diffs:** normalise generated IDs and random tokens first, and prove the normaliser on one identical-input pair. Verovio puts a random token into every SVG, so a raw diff always "differs".
- When a note qualifies a headline value (a source's real date, say), carry it into every doc that states the value. Check public claims against what is deployed, not the checkout.
- Doc examples with invented content use fictional IDs, never a real record's.
- Never write an identifier (GND, VIAF, ISNI, Wikidata QID, DOI) from memory; look it up. Don't offer one, or a name or publisher, from memory for the user to confirm either: a from-memory publisher got a "yes", and only a later search kept it out of the file. Dates and names inherited from an import or a conversion are claims to check too.
- Tally categorical values by exact match: a substring test (`'eu' in flag`) counted 252 where the source said 65.
- An editable dependency's version: `git -C <lib> log -3`, not `importlib.metadata`.

## Shell (zsh in the Bash tool)

- Push in its own Bash call after the commit: the secret guard scans only commits that exist when the call starts.
- fish's `(cmd)` substitution fails; use `$(cmd)`. An unquoted `$var` does not split into words; loop over a file list with `while read`.
- `$var:r…` is a zsh modifier (`$c:rules` printed `c61e134ules`); write `${var}:path`. `=word` at the start of a word expands to a command path, so `echo ===` fails; use `echo ---`. Give shell helpers multi-letter names: a helper named `c()` collided with an alias (parse error).
- A glob that matches nothing aborts the whole command. Quote globs and URLs meant for the called program (`--include='*.py'`, `'https://…?q=1'`), and search with `grep -r --include=` or `find` rather than a shell glob.
- Write scratch scripts to files rather than nesting heredocs. Python's `tempfile` writes to `/tmp` (TMPDIR is unset), not the session scratchpad: pass `dir=`.
- Don't `cd` away from the project root: the new directory sticks, and later relative paths, `git add` and tools' config lookups run there. `(cd sub && make)` leaves the shell where it was.
- `pkill -f PATTERN` and `pgrep -f PATTERN | kill` can match their own shell's command line and kill it; stop a server with `fuser -k PORT/tcp` or a saved PID. With `run_in_background`, run the server in that command's foreground (no trailing `&`), so the task ends when the server does.
- Run each long command as its own background task. Killing the child of an `a && b` chain lets the chain go on to `b`. Join independent probes with `;`: in an `&&` chain, a grep with no match silently skips the rest. Gate a long consumer on its generator (`test -f OUT || exit 1`), never with `;`: a missing file hung a CLI for 300 s.
- **Background waits:** run one iteration of a wait loop in the foreground first, and make every exit branch (success, failure, timeout) print what the waiter matches: one broken loop cost ~10 min. For CI, use `gh run watch <id> --exit-status`.
- **Output size:** one log grep printed over 64 MB. Print one PASS/FAIL line per case plus the first error and grep for those rather than `tail -N`, which cut results and kept whole tracebacks (~3k tokens per failed run). Send a third-party tool's stderr to a file and print its line count when the check is about that output (6 KB of warnings once). Keep stderr (no `2>/dev/null`) when a check is about warnings or logs: hidden renderer warnings took 4 calls to reproduce.
- A probe of a library that may crash runs one subprocess per case and prints its return code; `$?` after a pipe is the last command's.
- **Python packaging:** `python -m build` in a repo with a `build/` folder imports that folder; use `uv build --out-dir DIR`. A scratch venv with old dependencies in one step: `uv pip install -r pyproject.toml --all-extras -c pin.txt`.

## Edits

- For several changes in one sentence, replace the whole sentence: overlapping pairs each match once and still eat text.
- A file seen only through Bash (`cat`, `sed -n`) can't take the Edit tool, which needs a Read first; use the Python exact-match replace. Never send a verify in the same message as the Edits it checks.
- A script that rewrites a structured file (JSON, a notebook, hand-formatted config) edits at text level or matches its `indent`: a re-serialised notebook turned a 199-line diff into 1,477.
- For before/after checks, use `git show HEAD:path > tmp`, not `git stash`, which unstaged `git mv` renames.

## Reads

- Trim (`magick -trim`) and downscale renders and crops to ≤1000 px wide before reading them.
- Dump catalogues with only the fields you need; read vendor PDFs by grepping for the section. To compare long headers with a peer file, tally with `grep -c` first.
- A file over ~1.5k lines can also go to a subagent that returns findings: one 2.8k-line read carried ~40k tokens through 70 later calls, ~14% of that session's cache reads.
