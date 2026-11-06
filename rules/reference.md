# Topic tips

Narrow tips from the claude-orchestrator field reports, read on demand: the session-mode hook names this file after the session rules (`hygiene.md`), which it injects at session start. Read the matching section before that kind of work.

## Handoffs and items

- **When `/clear` pays:** at an item boundary, a fresh session (~35k after reading the handoff) is cheaper when (context − 35k) × the item's expected calls exceeds ~1M cache-read tokens. A review, fix and commit item runs ~45–50 calls, so from ~55k up a fresh session costs less. Put the estimate in the picker option.
- **Item size:** five sessions (2026-10-14 to 10-17) each ended one item or one typed prompt at 241–328k; one prompt ran from 38k to 266k, and a step that wrote ~60 KB of new code reached 203k.
- **Context figures:** two pickers (2026-10-28) guessed the context at 95k and ~110k; it was 143k and 205k. 11 of 20 field reports (2026-10-26 to 10-30) passed 145k, most within one item that started near 40k, mostly from whole reads of core files, diffs, notebook dumps and probe output.
- **`/clear` as a closing sentence** was passed over four times while one session ran on to 223k; as a picker option it is taken.
- **Boundaries:** a handoff that ends the item at "gate green" fell short twice: the live check and the review each added a fix round. A step with a large dump read plus a large write ends at "review done, before rewrite", with the findings in the handoff. A running delegate round is a natural handoff point: the next session runs `delegate wait`.
- **Skill loads:** above ~100k, check that the step a large skill (~10k) is loaded for will run in this session; once the launch it was loaded for moved to the next session.
- **Ask-only steps** need no skill load: the rules give the `ask` command. Load it before `launch`, `review` or `followup`.
- **Inline instead of delegated:** a step planned for a delegate that runs inline (control session, low quota) puts its reads, code and tests into the main context. One such build reached 161k before its first `cost` reading, and its review moved to the next session. Move the handoff boundary one deliverable earlier and read `cost --session latest` after each deliverable over ~20 KB.
- **Verification-heavy items:** past ~120k, run probes and fetches in a subagent that returns counts and pass/fail. One source-checking step added 73k to the main context (148k → 221k), and 10 of that session's 26 calls ran above 200k.
- **Reads before a decision:** above ~90k, with a decision picker as the next boundary, don't read code that only the next step's brief needs; put its file:line in the handoff.
- **Memory files** hold the resume pointer and binding facts, not history: three resumes (2026-11-01 to 11-03) read 9–10 KB of memory, one file a single running paragraph. Cut a file to that when it passes ~4 KB; the handoff carries the state.
- **Session id** for the closing picker's `--session`: the first line of `cost --session latest`.
- **Closing picker above ~100k:** list "`/clear`, then `/field-report --session <id>`" before plain `/field-report`, with the re-read cost in its description. Listed third without one, it was passed over and the report ran at ~125k.
- **Git picker above ~100k:** also offer "commit at the start of the next session, after `/clear`", recommended when the handoff already carries the commit step. One commit picker answered after 331 min paid for a whole rebuild; the handoff had been refreshed, so the next session could have committed for less.
- **Waits:** six sessions (2026-10-13 to 10-20) paid 10–18% of their cost units for one rebuild after a 61–281 min wait, three of them on `/field-report`. Half of all rebuild cost came from runs that finished after an hour. Over 14 logged sessions such rebuilds were 12.7% of orchestrator cost.
- **Sweeps:** one "check everything" request ran from 110k to 240k within a single prompt, with about 30 image reads.
- **Hand-paste loops:** a user posting prepared texts one by one is a new item. Make "`/clear`, then `/next`" an option in the picker that closes the first answer.
- **Handoff size:** two 29–31 KB handoffs, mostly done-item history, cost ~8k tokens per resume.
- **Gates in handoffs:** list each gate with its interpreter and env, plus a one-line scratch-venv recipe; a file regenerated with provenance names the env that produced it (two sessions lost 6 calls and a 5 min rerun between them). A full `pytest -x` once stopped at test 188 of 531 on a known failure, so give the ready `--deselect` flag. `delegate` is on PATH only after `install.sh --bin`, so write its full path. Run each gate command once before writing it in: a `--dump` gate broke when its data moved.
- **Gates outside the project venv:** a gate that needs a package the venv lacks (Playwright, a browser) names the interpreter's path, or the next session spends calls finding it. Past ones are in the `verify` field of `~/.cache/claude-delegate/runs/<repo>/*/meta.json`.
- **Steps that need another vendor:** a handoff that names a review or followup gives the inline fallback beside it (which modules to read whole, which outputs to check against each other), so a session without delegation doesn't improvise.
- **Review pages:** a page built during a delegate wait and checked in a browser took one session from 45k to 159k, well above the ~10–15k per step the rules assume. The handoff's next step stops at "page built and handed to the user" and lists applying the picks separately.
- **Cross-repo notes** go into the target repo's `HANDOFF.md`. Before editing another repo's handoff, check its mtime, re-read the anchor and replace one line.
- **Handoff steps as hypotheses:** mark a step taken from an external audit "from audit, unverified against neighbouring rules". A handoff's "watch for X" is a hypothesis: run the generic check, not only X.
- **Facts in handoffs:** each later item names its source (file:line, or date and who asked); a number names the command that measured it, with any non-default parameter (difflib `autojunk=False`) (an unmeasured "158 buys, EUR 45.2k" cost 4 calls); a user-side prerequisite (bot, account, connector) says whether it exists yet, not only the plan to use it.
- **Later-list requests:** quote the user's words in one line, with the session id. "The author's request" alone cost ~9 calls of transcript mining and found nothing.
- **Changelog sections in handoffs** are guesses: check whether the feature has been released and follow the latest precedent. A handoff's "Fixed" line was wrong for a feature that was itself unreleased.
- **No handoff:** a "continue from the handoff" that finds no file costs the next session several calls of transcript digging.
- **Status lines:** all 9 harness status requests in three sessions (2026-10-13/14) came at the points the session rules name: runs of calls without text, unexpected results, calls that may run over a minute.
- **Destructive git steps** run in their own Bash call: bundled with other edits, the auto-mode classifier denied the whole call.

## Decisions

- A fill policy picked without a measurement made one file 59× larger at build time and needed a new picker.
- Ask an unstated quantity that shapes the plan (session length, audience, budget) before writing, not as an "assumption to confirm" afterwards: one outline revision took 15 edits.
- "Other" in a picker came back as a bare selection with no text, so an option that needs typed input fails.
- When the user names the consumer of the output (a bot, a person, a tool), offer an option where that consumer does the work. A picker that asks for a user-side test result offers "not set up yet: help set it up" and says what in pasted output would decide it.
- **Reading first:** when the next step is "the user reads X", ask whether the reading is done before content picks or git approval; a push picker came back "still reading".
- **Vague reports:** when the first check shows nothing wrong, show it and ask what is missing before widening the test matrix. Ten calls across browsers, widths and themes once failed to reproduce a complaint that was about taste.
- **Pickers answered with a question:** answer it as Q&A; after the second such answer in one item, refresh the handoff before asking again. Two of three pickers in one session (2026-11-02) came back as new questions, and context grew ~95k → ~135k inside the item.
- **All lasting decisions at once:** before sending asks, list every lasting decision of the step, including which data files get committed. One ask sent after the first three returned cost 4 calls and another wait.
- **Verdict pickers:** for a listening or visual check, open the page (`xdg-open URL`) before the picker that asks for the verdict; otherwise the answer is "open it" and the picker repeats.
- **Parts that add up to a target** (word cuts, a budget): offer single-select combinations that each meet the target, plus "raise the target". A multiselect left the sum to the user: the pick covered 20 of 63 words and needed another picker.

## Delegation evidence

- **Spare quota:** Launches fell from 16 (2026-10-13 to 10-20) to 2 in ~30 orchestrator sessions (10-21 to 10-26). A six-line CSS fix took ~8 calls through `delegate` against ~3 inline; one session sat idle for 12.5 min of review. Codex 7d peaked at 17/30/51/32/4/7/28/14% used over its eight weekly windows to 2026-10-26; Cursor Auto used 54% of the month before. Claude 7d ran at 97–99% used in both Pro weeks; on Max (since 2026-10-26) the first full week peaked at 65%, so Claude is no longer clearly the scarcest pool. Codex 5h can be full from the user's own Codex Desktop use while 7d looks spare, so check 5h at dispatch.
- **Second opinions:** in uses through 2026-10-20 the two models mostly picked the same option, and most rounds added a risk or corrected a fact in the question. One risk note contradicted a fact measured minutes later, hence the cheap checks first. Answers took 20 s to about 5 min, and about 6 min for a review at `--effort high`. One session (2026-10-21 to 10-26) deferred a public-API second opinion "because of context size". Context size is no reason to skip one: the question is a file and the answer is short. Read numbers from the config before writing them into a question: one question said 6 followed links where the config had 4.
- **Web facts:** Cursor's ask mode has no web tools, and Codex's web tool in `ask` reads only a cache; live search needs `delegate launch codex --web-search`.
- **Evidence at a commit:** Cursor's ask mode ran `ls -1` but rejected `git show` and compound commands (2026-11-04), so it read the working tree instead; that only matched because the tree was at the commit. Paste what the question needs, or have the tree at that commit.
- **Asks as confirmation:** when your own probes already list every doubt, the ask mostly confirms and words the fixes (one fact-check, 2026-11-02: both vendors, 0 doubts beyond the list, under 3 min). Say so in the record, and still ask for doubts beyond the list: that is its only blind part.
- **Visual builds** tuned by eye with live network input stay inline until the first good render (one ~100-line figure script took 3 calls); delegate later regeneration and variants.
- **Mixed builders:** when a builder's content is judgement (per-item prose and analysis) beside near-copy code, split it: delegate the code from a spec stub and write the content inline. Written whole inline, one 20 KB builder was carried through ~25 later calls.

## Data checks

- Keep the minimal example next to any tool-behaviour claim you write into a doc or handoff ("the renderer draws X"), probed on the current file.
- Look for an existing validator in the project and its libraries before writing one. Before writing a script that drives a fixture, read the fixture's own check script for paths and setup.
- Check an example meant for others in the tool they will use: the maintainers' online editor and schema version, a fresh venv with only the shipped install steps, a real JupyterLab (nbclient emulates widgets). A post checked only against the project's own schema cost ~16 calls and a round trip.
- A change to scoring, ranking or keyword weights gets a before/after run on real inputs the same day: 53 unit tests passed while 6 off-topic posts jumped to the top tier. Check a UI fix at every variant its inputs depend on (widths, themes): a CSP hash of an injected style held only at 1280 px.
- Derive UI-test fixtures and expected values from a data probe in the same turn. When porting thresholds between repos, re-derive them on both repos' data.
- Before adding a check on a manifest or config field, grep every rule that reads the field and confirm all of them can pass on one fixture. In an audit, ask "can this gate pass at all once fixed?".
- **Positive controls:** Verovio's `getLog()` is never filled through the Python binding, so "no warnings" from it proves nothing. Prove a warning probe on an input that does warn.
- **Absence and probe output:** print full key lists or grep for the key; a `sorted(keys)[:8]` cut `usage` and nearly proved it missing. Print probes as counts, kinds and pass/fail per case, and keep raw streams in files.
- **Pattern checkers** get near misses that must not fire (`'/'` for an absolute-path check), not only hits.
- **Causes and labels:** a causal line in public text ("X costs Y because Z") gets one probe that isolates Z; one README cause was refuted after only its numbers had been checked. An error type (`NXDOMAIN` from an errno) is not a cause in a comment or PR body. Re-derive a label ("duplicates", "stale") from the data before it goes into records.
- **Reference series:** check a benchmark over the whole window (first date, session count, first print) before computing excess returns; a new listing that starts mid-window with a placeholder price inverted one conclusion. Normalise hosts (`www.`, trailing `/`) before same-host comparisons.
- **Fixtures from external tools:** run the export twice and byte-compare before using it as a fixture.
- **Headless browser checks:** disable the cache, scroll the target into view, fail on console errors, and assert one rendered property (a dark-pixel share, a screenshot) beside the DOM state: a blank scan passed every DOM check.
- **Render and export diffs:** normalise generated IDs and random tokens first, and prove the normaliser on one identical-input pair. Verovio puts a random token into every SVG, so a raw diff always "differs".
- When a note qualifies a headline value (a source's real date, say), carry it into every doc that states the value. Check public claims against what is deployed, not the checkout.
- Doc examples with invented content use fictional IDs, never a real record's.
- Never write an identifier (GND, VIAF, ISNI, Wikidata QID, DOI) from memory; look it up. Don't offer one, or a name or publisher, from memory for the user to confirm either: a from-memory publisher got a "yes", and only a later search kept it out of the file. Dates and names inherited from an import or a conversion are claims to check too.
- **Source vs fact:** a substring match proves the source says X, not that X holds; go through a fact sheet's caveats before review. A claim that joins two sources gets its own check, and claims added in a fix round get the claim check again.
- **Layout fit:** probe the changed block's own line count, with a variant known to change it as the positive control.
- Tally categorical values by exact match: a substring test (`'eu' in flag`) counted 252 where the source said 65.
- An editable dependency's version: `git -C <lib> log -3`, not `importlib.metadata`.
- **Thresholds:** a pass/fail bar taken from a facts file is a number entering a claim: re-derive its setting (grid, N, options) or measure a floor in the same run. A 0.7 dB bar measured on another grid failed a correct build.
- **Checker self-tests:** each control feeds bad input to the same check and must FAIL; never wrap a control in `not`. The self-test passes only when exactly the controls fail; two of four controls written the other way round reported 2/4.
- Print `inspect.signature(f)` before a library function's first call in a probe; a guessed keyword (`window=` for `windows`) cost one.

## Shell (zsh in the Bash tool)

- Push in its own Bash call after the commit: the secret guard scans only commits that exist when the call starts.
- fish's `(cmd)` substitution fails; use `$(cmd)`. An unquoted `$var` does not split into words; loop over a file list with `while read`.
- `$var:r…` is a zsh modifier (`$c:rules` printed `c61e134ules`); write `${var}:path`. `=word` at the start of a word expands to a command path, so `echo ===` fails; use `echo ---`. Give shell helpers multi-letter names: a helper named `c()` collided with an alias (parse error).
- A glob that matches nothing aborts the whole command. Quote globs and URLs meant for the called program (`--include='*.py'`, `'https://…?q=1'`), and search with `grep -r --include=` or `find` rather than a shell glob.
- Write scratch scripts to files rather than nesting heredocs. Python's `tempfile` writes to `/tmp` (TMPDIR is unset), not the session scratchpad: pass `dir=`.
- Don't `cd` away from the project root: the new directory sticks, and later relative paths, `git add` and tools' config lookups run there. `(cd sub && make)` leaves the shell where it was.
- `pkill -f PATTERN` and `pgrep -f PATTERN | kill` can match their own shell's command line and kill it; stop a server with `fuser -k PORT/tcp` or a saved PID. With `run_in_background`, run the server in that command's foreground (no trailing `&`), so the task ends when the server does.
- External fetches get a timeout (`curl --max-time 30`); probe a host that project notes call flaky once before a loop. One fetch loop hung ~3 min and took 4 calls to stop.
- Run each long command as its own background task. Killing the child of an `a && b` chain lets the chain go on to `b`. Join independent probes with `;`: in an `&&` chain, a grep with no match silently skips the rest. Gate a long consumer on its generator (`test -f OUT || exit 1`), never with `;`: a missing file hung a CLI for 300 s.
- **Background waits:** run one iteration of a wait loop in the foreground first, and make every exit branch (success, failure, timeout) print what the waiter matches: one broken loop cost ~10 min. For CI, use `gh run watch <id> --exit-status`.
- **Output size:** one log grep printed over 64 MB. Print one PASS/FAIL line per case plus the first error and grep for those rather than `tail -N`, which cut results and kept whole tracebacks (~3k tokens per failed run). Send a third-party tool's stderr to a file and print its line count when the check is about that output (6 KB of warnings once). Keep stderr (no `2>/dev/null`) when a check is about warnings or logs: hidden renderer warnings took 4 calls to reproduce.
- A probe of a library that may crash runs one subprocess per case and prints its return code; `$?` after a pipe is the last command's.
- `find` here may be bfs: `-newermt '-6 hours'` fails; use `-mmin -360`. Never chain two heredoc commands with `||`: the empty first script ran and nothing was written. Read a config's top-level keys before indexing it in a one-off script.
- Never print any part of a secret file (a 6-char prefix preceded a denied batch); check it with `test -s` or its length.
- `gh release view --jq .body` adds a newline: `diff <(gh release view TAG --json body --jq .body) <(cat FILE; echo)`.
- **Python packaging:** `python -m build` in a repo with a `build/` folder imports that folder; use `uv build --out-dir DIR`. A scratch venv with old dependencies in one step: `uv pip install -r pyproject.toml --all-extras -c pin.txt`.

## Edits

- For several changes in one sentence, replace the whole sentence: overlapping pairs each match once and still eat text.
- A file seen only through Bash (`cat`, `sed -n`) can't take the Edit tool, which needs a Read first; use the Python exact-match replace. Never send a verify in the same message as the Edits it checks.
- A script that rewrites a structured file (JSON, a notebook, hand-formatted config) edits at text level or matches its `indent`: a re-serialised notebook turned a 199-line diff into 1,477.
- For before/after checks, use `git show HEAD:path > tmp`, not `git stash`, which unstaged `git mv` renames.
- After editing Python that contains backslashes (docstrings, regexes), parse it with `python3 -W error -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' FILE` before the gate.

## Reads

- Trim (`magick -trim`) and downscale renders and crops to ≤1000 px wide before reading them.
- **Rendered pages:** screenshot the element or region that answers the question (one render row, one table), not the whole card.
- Generated reports with wide tables: grep the `##` outline and read one section through `cut -c1-300`; a 240-line read of a 121 KB report cost ~25k. Send whole reads to `delegate ask`. For a diff that moves a large block, read `git diff --stat`, then the new functions by line range.
- **Truncated lines:** `cut -c` is for outlines. One `cut -c1-700` dropped the last ~100 characters of an ~800-character line, and those held the qualifier a finding turned on. Print the line's length or read it whole before a claim rests on it.
- Dump catalogues with only the fields you need; read vendor PDFs by grepping for the section. To compare long headers with a peer file, tally with `grep -c` first.
- **Transcripts:** picker answers and rejection reasons live in `tool_result` blocks, not typed prompts. `grep -c` counts lines, so on a one-line page count with `grep -o … | wc -l`.
- Notebooks are JSON: `grep -F 'window="boxcar"'` on an `.ipynb` finds nothing, because quotes there are `\"`. Grep with escaped quotes or read cells through `nbformat`.
- A file over ~1.5k lines can also go to a subagent that returns findings: one 2.8k-line read carried ~40k tokens through 70 later calls, ~14% of that session's cache reads.
