# Field results

Numbers come from `delegate cost --session` on each session's transcript and include the calls that wrote the report. Cost units weigh input 1, cache read 0.1, 1h cache write 2, output 5. Compare sessions per call, because the work differs. `mode` is the session's drawn mode in the control-session experiment (hook registered 2026-10-13 10:29); — means the session started before it. Field reports are kept locally in `feedback/` (gitignored since 0.1.0) and written with `delegate report` / `/field-report`; new sections are added here when reports are folded in.

| date | session | mode | calls | context at end | cost units | units per call | idle rebuild share |
|---|---|---|---:|---:|---:|---:|---:|
| 2026-10-09 | 762dc86f (marathon) | — | 389 | 779k | 26.22M | 67.4k | 19.7% |
| 2026-10-10 | 55a11d16 | — | 88 | 158k | 1.91M | 21.7k | 26.1% |
| 2026-10-10 | 9a59a1f6 | — | 104 | 334k | 3.80M | 36.5k | 7.4% |
| 2026-10-10 | fc63a0c8 | — | 67 | 162k | 1.24M | 18.5k | 0 |
| 2026-10-10 | e06990b8 | — | 103 | 251k | 2.52M | 24.4k | 0 |
| 2026-10-10 | 48800fa0 | — | 112 | 283k | 3.01M | 26.9k | 0 |
| 2026-10-10 | 96cb4576 | — | 116 | 286k | 3.36M | 28.9k | 0 |
| 2026-10-10 | 4994250f | — | 141 | 290k | 4.07M | 28.9k | 10.6% |
| 2026-10-11 | 52e13958 | — | 66 | 160k | 1.21M | 18.3k | 0 |
| 2026-10-11 | a79562ac | — | 65 | 158k | 1.15M | 17.7k | 0 |
| 2026-10-11 | 7272c8d9 | — | 71 | 183k | 1.48M | 20.9k | 0 |
| 2026-10-11 | 9b03e065 | — | 67 | 192k | 1.78M | 26.6k | 15.9% |
| 2026-10-11 | 12c68dfa | — | 129 | 223k | 3.04M | 23.5k | 12.1% |
| 2026-10-12 | 032191ec | — | 80 | 175k | 1.50M | 18.8k | 9.0% |
| 2026-10-12 | 9ce4e364 | — | 60 | 224k | 1.67M | 27.8k | 0 |
| 2026-10-13 | 6a4f5d31 | — | 79 | 193k | 1.67M | 21.1k | 0 |
| 2026-10-13 | 40ff2d3a | — | 77 | 185k | 1.60M | 20.8k | 0 |
| 2026-10-13 | 1e5ff45c | — | 56 | 172k | 1.67M | 29.9k | 17.3% |
| 2026-10-13 | ae30942b | — | 48 | 144k | 0.94M | 19.6k | 0 |
| 2026-10-13 | 7c19e13a | — | 121 | 261k | 3.30M | 27.3k | 13.7% |
| 2026-10-13 | 3240a01e | claude-only | 67 | 177k | 1.67M | 24.9k | 17.9% |

21 sessions, 2,106 calls, 68.8M cost units; idle rebuilds 8.2M (11.9%). The 20 sessions after the marathon averaged 24.8k units per call.

## 2026-10-10

[Lever 1](cost-analysis.md#1-one-work-item-per-orchestrator-session-about-60) held up. Measured with `delegate cost --session`; the work differs between sessions, so compare per call, not in total.

| session | work items | calls | context at end | cost units | units per call | Claude 7d (shared pool) |
|---|---|---:|---:|---:|---:|---|
| 762dc86f, eu-pd-composers marathon | 7 | 389 | 779k | 26.2M | 67k | 84 → 67 |
| 55a11d16, LLM style pass | 1 (from `HANDOFF.md`) | 88 | 158k | 1.9M | 22k | 60 → 58 |
| 9a59a1f6, audiospylt | 3 (assessment, plan, Phase 0) | 104 | 334k | 3.8M | 37k | 57 → 54 |
| fc63a0c8, eu-pd-composers v3.5.0 | 1 (from `HANDOFF.md`) | 67 | 162k | 1.2M | 19k | 58 → 54 |
| e06990b8, cache fix, cleanup, IMSLP rights | 4 | 103 | 251k | 2.5M | 24k | 54 → 51 |
| 48800fa0, rights evidence r016, gold set, merge | 3 | 112 | 283k | 3.0M | 27k | 51 → 48 |
| 96cb4576, gold-set form, annotation support, plan | 10 | 116 | 286k | 3.4M | 29k | 48 → 45 |
| 4994250f, identity guard, r017 | 1 large + 2 short | 141 | 290k | 4.1M | 29k | 45 → 41 |

One-item sessions cost 19–22k units per call, 68–72% below the marathon; the model predicted about 60%. The three-item audiospylt session sits in between at 37k: its last ~20 calls each re-read ~300k. Its report notes that a handoff after the plan would have kept Phase 0 near 40k context.

New findings from 55a11d16:
- **Idle cache rebuilds.** Two wake-ups after 4.4h and 6.1h waits each re-wrote the whole context, about 120k tokens at 2×. Together they were 26% of the session's cost. SKILL.md now says to hand off before a multi-hour wait.
- **Cursor pools resolved.** openusage `plan_auto_percent_used` = (Auto + Grok + Composer spend) / $450, exact at two snapshots. So "Cursor Models" shares the Auto pool, and `cursor-grok` now routes on `cursor-auto` with real quota. Named Claude/GPT models bill `plan_api` ($22.50).
- **Bulk labelling** (6.5k rows, outside `delegate`): grok-4.7-low used about 2.7 points of Cursor Auto; gpt-6.1-sol@low used about 15 points of Codex 7d. Both had zero failures.
- **Lessons now in SKILL.md:** read-only is not blind (isolate the answer key), per-chunk quota gates, PID files for detached drivers.
- **Thinking share was unchanged** at about 60% of output under xhigh, which is consistent with the effort analysis in [lever 4](cost-analysis.md#4-keep-heavy-reading-and-scripting-out-of-the-main-context-about-4-measured-more-possible).

New findings from 9a59a1f6 and fc63a0c8, and what changed:
- **CRLF patches never applied: our bug, now fixed.** `make_patch` read `git diff` with `text=True`, and universal newlines turned every `\r\n` into `\n`. Patches and review diffs are now handled as bytes; a lifecycle test covers a CRLF base. The proposed file-copy fallback and launch warning are not needed.
- **`runs.jsonl` saw 1 of 5 runs.** Read-only, hand-merged and discarded runs are now logged once at `cleanup`, with their reviews.
- **Codex usage of resumed rounds was cumulative.** `turn.completed.usage` is the thread total. A round now records its own delta as `usage` and the raw total as `usage_thread`, and the exit packet shows the delta.
- **Reviews approved wrong briefs twice** (a physically wrong Σamp² criterion; a `dump_date` convention break). The review prompt now asks the reviewer to challenge the brief's criteria against the domain and repo conventions. The implement template names convention-stamped columns, and SKILL.md tells the orchestrator to check domain criteria before launch.
- **SKILL.md and templates:** hand off after an assessment or plan; measured numbers per option in decision questions; one bundled approval for outward git steps in auto mode; Jupyter needs `ipython` or `--verify` under Codex; clean up only paths the session created; zsh word splitting; research scratch under `.delegate/`.

## 2026-10-10 (evening)

All four started from a handoff, and all four then sprawled. The user added items mid-session ("ok go on", "sure go on"), and the orchestrator started them in place.

| session | prompts that started a new item, at context |
|---|---|
| e06990b8 | 87k, 180k |
| 48800fa0 | 205k, 256k |
| 96cb4576 | 145k, 201k, then 7 support questions from 215k |
| 4994250f | 219k (mid-item), 275k |

- **The guard fired too late, but it works.** At 250k it fired only in three sessions, and each time after the boundaries had passed. Each time it fired, the session wrote a handoff. Clearing at every prompt above 120k (simulated on the transcripts, re-reads only, an upper bound) would have saved 20%, 18%, 33% and 21%. The hook now fires from 120k, then every 60k. Its text asks Claude to decide whether the prompt starts a new item, and if so to hand off and offer `/clear` before starting, even after "go on". Tested in `tests/test_context_guard.py`.
- **SKILL.md never reached these sessions.** None of the four invoked `delegate`, so its session and approval guidance went unread, and the zsh slip recurred. Two fixes:
  - `rules/orchestrator.md`, imported into the user-level `CLAUDE.md` by `./install.sh --rules`, carries the rules that apply everywhere;
  - the skill description now asks for unprompted use on mechanical work with a clear spec.
- **No delegation at all.** Claude 7d fell from 54% to 41% over the four sessions while Cursor Auto stayed at 71% (2.9× pace). Doc refreshes across five files, tests from a written spec and a near-copy fetcher were all delegable.
- **Smaller items:**
  - `status` flags an applied run whose worktree is over a day old;
  - `apply` ends by suggesting `cleanup`;
  - SKILL.md says a merge listed in a handoff is not approval, to start long network jobs first, to falsify a hypothesis with one cheap sample, and to check every changed headline value against a source.
- **Not in this repo's scope:**
  - Artifact lessons: take a headless-Firefox screenshot before the first publish, and read the `.d.ts` before writing capability code.
  - The project-side cache-lag bug in eu-pd-composers.

## 2026-10-11

All three started after `/clear` from a `HANDOFF.md` and ended at the next decision point. Numbers from `delegate cost --session`, which includes the calls that wrote each report, so they run a few calls above the reports' own figures.

| session | work | delegates | calls | context at end | cost units | units per call | Claude 7d |
|---|---|---|---:|---:|---:|---:|---|
| 52e13958, eu-pd open-licence scanner + live scans | 1 item | Cursor round 3, Codex reviews 2–3 | 66 | 160k | 1.21M | 18.3k | → 32 |
| a79562ac, eu-pd open-licence axis for r020 | 1 item (merge, pilot, feature) | Cursor 2 rounds, 1 Codex review | 65 | 158k | 1.15M | 17.7k | 32 → 29 |
| 7272c8d9, Webern Op. 27 MEI 5.1 + docs | 3 small items | none | 71 | 183k | 1.48M | 20.9k | → 26 |

- **[Lever 1](cost-analysis.md#1-one-work-item-per-orchestrator-session-about-60) held.** 17.7–20.9k units per call is the lowest yet: the one-item sessions of 2026-10-10 ran 19–22k, the sprawling evening ones 24–29k. The rules file reached these sessions; two reports cite it.
- **The 120k guard fired mid-item once** (a79562ac, ~121k). The prompt continued the item, so the session carried on and handed off at 144k. That is the intended reading of the guard text.
- **Webern ran three items to 183k.** The docs item started at ~70k, under the threshold, and read and wrote about 55 KB. The rules now say to hand off below 120k when the next item is large.
- **Delegation improved, but Claude 7d still fell about 3 points a session** (32 → 26 over the three). Cursor Auto built a 15-file feature in 2 rounds with 0 review findings and no visible pool change (70 → 70; 68 at the end of the batch).
- **What worked, now in SKILL.md and the templates:**
  - one batched question for all of a handoff's open decisions;
  - a stop rule agreed before a pilot (0 of 49 ended a 4.9M-token search);
  - expected dry-run numbers computed by the orchestrator on a separate path; Cursor matched them exactly;
  - reading the core while the cross-review runs;
  - fixing the last review's 1–10 line findings inline instead of a fourth round.
- **Friction, and what changed:**
  - Two prescribed fixes handled the reported example and failed the adjacent case. Briefs that prescribe a mechanism now state the invariant and one adversarial case; the review template checks invariants against adjacent cases.
  - Injected-response tests never touched the real `requests` adapter, where a header-case bug sat. The implement template asks network code for one fake-session test of the adapter.
  - A hand-written wait loop exited at `verifying`. New `delegate wait RUN`, run in the background, blocks until the round and its reviews have finished, including verify.
  - `cost --since` took a UTC date, so a run applied at 01:09 local time fell on the previous day. It now takes a local date.
  - Rules: a local commit is not approved either; name the check that passed rather than "validates"; carry qualifying notes into every doc; use fictional IDs in doc examples; zsh `=word`, unmatched globs, `cd` in a call, and killing chained commands.
  - This session itself: "continue from the handoff" found no `HANDOFF.md` here, because the previous session had pointed at the optimisation plan (now this file and `cost-analysis.md`) instead. Six calls went into reading transcripts. The rules now say to always write the file.
- **Not in this repo's scope:** lxml cannot compile MEI's XPath 2 Schematron (CAMAT uses `saxonche`); the eu-pd scanner's test fake answers unmapped robots URLs with 404.

## 2026-10-11/12

All three started after `/clear` from a `HANDOFF.md`. Numbers from `delegate cost --session`, including the calls that wrote each report.

| session | work | delegates | calls | context at end | cost units | units per call | idle rebuild | Claude 7d |
|---|---|---|---:|---:|---:|---:|---|---|
| 9b03e065, Webern MEI edition headers | 1 item, short Q&A | none | 67 | 192k | 1.78M | 26.6k | 91 min, 141k written: 15.9% | 26 → 24 |
| 12c68dfa, Webern breaks, then proofreading Q&A | 1 item, ~6 questions | none | 129 | 223k | 3.04M | 23.5k | 464 min, 184k: 12.1% | 24 → 20 |
| 032191ec, table grid, site audit, eu-pd note | 3 items | Codex ×3, Cursor ×1 | 80 | 175k | 1.50M | 18.8k | 102 min, 68k: 9.0% | 11 → 10 |

- **Idle cache rebuilds are the second-largest lever: 12.7% of all logged orchestrator cost.** Each of the three sessions had one: the user came back after a break, and the first prompt rewrote the whole context at 2×. Without it, units per call were 22.4k, 20.7k and 17.1k, in line with the one-item sessions. A scan of all 14 sessions in `token_usage.jsonl` found rebuilds in 7 of them, 7–26% of each, 7.2M of 56.3M units in total. See [lever 8](cost-analysis.md#8-idle-cache-rebuilds-127-of-logged-cost-found-2026-10-12).
- **Q&A after a finished item is still where sessions sprawl.** 12c68dfa answered six proofreading questions after its build was pushed and ran on to 223k. The guard fired at ~125k and ~204k, and `/clear` was offered four times as a closing sentence. The rules and SKILL.md now say to make it an option in the picker that closes the first answer.
- **A six-line delegation cost more than doing it inline:** about 8 orchestrator calls against about 3. The rules and SKILL.md now keep changes under ~10 lines inline unless the user asks to delegate.
- **`delegate wait` worked as built** (0ecc1c8): two detached read-only audits ran while the note was researched and written, with one wait and no polling.
- **Nothing was delegated in the two Webern sessions.** Both were design and data items, and the break placement needed judgement. Claude 7d fell from 26% to 20% over them while Cursor Auto kept 2.8× spare pace; the mechanical follow-ups are queued in that project's handoff. Claude 7d then fell from 20% to 11% before 032191ec began, in sessions with no report.
- **Lessons now in the rules:** never write an authority identifier from memory (a remembered GND for Webern was wrong); dates inherited from a Kern-to-MEI conversion are claims to check; a sibling project's renderer claim did not hold, so probe such claims and keep the minimal example next to any you write; check public claims against what is deployed, not the checkout; tally categories by exact value (a substring test counted 252 instead of 65); show the first check and ask before widening a test matrix; `pkill -f` can kill its own shell.
- **SKILL.md and templates:** visual checks use orchestrator-made screenshots handed to every reviewer, because Codex's headless Firefox crashed in its sandbox; research briefs for public writing ask for a privacy-flags section and a second count per headline number, capped at about 15 KB.
- **Not in this repo's scope:** a dark-mode `user.js` pref for headless Firefox, Brave's `--no-sandbox`, Verovio SVG position parsing, the Wikidata API as a fallback when DNB and lobid show a bot wall, revision histories from timestamps.

## 2026-10-12/13

Both started after `/clear` from the project's `HANDOFF.md`. Numbers from `delegate cost --session`, including the calls that wrote each report.

| session | work | delegates | calls | context at end | cost units | units per call | idle rebuild | Claude 7d |
|---|---|---|---:|---:|---:|---:|---|---|
| 9ce4e364, PDF facsimile module from a written guide | 1 item | Codex build, Cursor review | 60 | 224k | 1.67M | 27.8k | none | 7 → 6 |
| 6a4f5d31, upstream issues: re-check, post, follow up | 1 item | none | 79 | 193k | 1.67M | 21.1k | none | 98 at end (weekly refill) |

- **Round 1 passed on a data module.** Codex @xhigh built `camat.pdf_facsimile` (887 lines, 48 tests) in 16 min. Expected values came from an acceptance fixture that the orchestrator built from the edition's own files, independently of the code being ported; all checks passed with no fix round. Cursor's review and the orchestrator's full read each found one bug the other missed.
- **27.8k units per call** in 9ce4e364 came from staying past the ~150k picker for the review, up to 224k. It was the same item, so that is the intended use, and the late calls' re-reads are its price.
- **Verify must match CI.** A bare `pytest -q` collected a release-only test folder, so the run reported verify FAIL, and both medium review findings were about the command. SKILL.md now says to copy `--verify` from the CI workflow, and to edit the spec before launch when a brief deliberately narrows it.
- **Status-update prompts:** the harness asked for a status update 9 times across the two sessions, each time during a run of several reads or probes. The rules now ask for one line before such a run.
- **A stray `cd` broke `cost --session latest`** at the end of 6a4f5d31. It now walks up to the nearest parent folder that has a project dir.
- **Decisions in pickers (user request, 2026-10-13).** Decisions had often been asked in prose. The rules now say discrete choices go into `AskUserQuestion` with the recommended option first. Lasting decisions also get Codex's and Cursor's picks through `delegate ask --agent both`. First use, on how to open-source this repo: both picked the same option, their risks did not overlap, and Cursor corrected a fact in the question.
- **Not in this repo's scope:**
  - Re-read every cited thread in full before posting a day-old draft: it found 4 errors.
  - `gh api markdown -f mode=gfm` as a pre-post render probe, and diffing the posted body after posting.
  - Issue images at 2× with `<img width>` set to half the pixel width.
  - Verovio's Python log capture through fd redirection.
  - Sanity-check an automated schema traversal against a known member: it first reported 326 elements instead of 17.

## 2026-10-13: MEI 5.1 target

Started after `/clear` from the project's `HANDOFF.md`; the first report written with `/field-report`. Numbers from `delegate cost --session 40ff2d3a`.

| session | work | delegates | calls | context at end | cost units | units per call | idle rebuild | Claude 7d |
|---|---|---|---:|---:|---:|---:|---|---|
| 40ff2d3a, `camat-convert --mei-version 5.1` from a decided spec | 1 item | Codex build (2 rounds), Cursor review | 77 | 185k | 1.60M | 20.8k | none | 93 at end |

- **Independent expectations held again.** The orchestrator built a fixture of 40 real Humdrum files and computed the expected rewrites with lxml, a different code path from the delegate's text edits; one probe call found a real file for the incomplete branch. Codex matched every expected total in round 1. Told "do not change the expectation; report the case and its cause", it flagged three checker conflicts and left the fixture alone.
- **The checker, not the code, lost round 1's verify signal.** The acceptance script called `Path(__file__).resolve()`, which followed the `--link` symlink into the main checkout, so it failed before checking anything. It also compared two separate conversions whose IDs are random per run. A smoke test against the old code showed neither: the new flag failed at argparse, so only the failure branch ran. SKILL.md step 0 now covers acceptance scripts in linked folders, running each branch once before launch, and pinning seeds. `delegate launch --preflight` is on the Roadmap.
- **A byte invariant taken literally cost a round** (8 min, 1.05M cached Codex tokens). "Byte-identical outside the edited tags" made Codex keep six whitespace-only wrapper lines and flag them instead of fixing them. The implement template now asks for the expected look of the output next to a byte rule.
- **The cross-review added nothing new.** The known issues were listed in `--focus`; the reviewer echoed two of them, proposed a fix that would have hidden other errors, and one finding was wrong. SKILL.md step 4 now says to mark known issues as handled, and that a small follow-up covered by your own read and a real-data check needs no second review.
- **`delegate report` worked on its first live use:** skeleton, one `token_usage.jsonl` row, one copied run row, mode 644. The copied row kept the run log's folder name (`camat_v2`) while the session row had `egorpol/camat_v2`; copied rows now take the session row's `repo`.
- **Context reached 185k within one item.** The guard fired at ~145k mid-item, which is its intended use. The two waits (18 and 8 min) went into handoff and memory updates, so the item stayed resumable. One stray `cd` broke the rule again, without damage.
- **Not in this repo's scope:** a converter that resolves manifest entries against the cwd instead of the manifest's folder; Verovio's default `xmlIdSeed=0`, which makes conversions differ byte for byte between runs; the bundled MEI 5.1 schema rejecting Verovio's `meiversion="6.0-dev"`.

## 2026-10-13: camat_v2 and Webern Op. 27

Four sessions, each closed with `/field-report`. Numbers from `delegate cost --session <id>`, so they include the calls after each report. 3240a01e is the first session with a drawn mode (`claude-only`: rules and hook on, delegation blocked); the other three started before the session-mode hook was registered. Claude 7d is the share left when the report was written.

| session | work | delegates | calls | context at end | cost units | units per call | idle rebuild | Claude 7d |
|---|---|---|---:|---:|---:|---:|---|---|
| 1e5ff45c, camat_v2: namespace bug fixed test-first, an earlier run's fix committed, upstream comment checked | 3 items | none | 56 | 172k | 1.67M | 29.9k | 281 min (17.3%) | 84 |
| ae30942b, camat_v2: tutorial example source chosen, build spec written | 1 decision | `ask --agent both` | 48 | 144k | 0.94M | 19.6k | none | 82 |
| 7c19e13a, Webern Op. 27 movement I: placement fixes, then a consistency pass, over three days | 3 items | none | 121 | 261k | 3.30M | 27.3k | 643, 1457, 466 min (13.7%) | 76 |
| 3240a01e, Webern Op. 27: four proofreading scripts promoted from scratch, tempo marks | 1 item | none (claude-only) | 67 | 177k | 1.67M | 24.9k | 77 min (17.9%) | 73 |

- **A late `/field-report` pays an idle rebuild.** In 1e5ff45c and 3240a01e the report was typed after 281 and 77 min idle at 164–168k context, so it rewrote the whole cache: 17–18% of each session's cost units. The guard lets slash commands through. The rules now say to offer `/field-report` in the picker that closes an item, while the cache is warm.
- **The idle hold fired live for the first time** (7c19e13a, 464 min idle at 110k). The user resent the prompt, which started a consistency pass that reached 240k before the next prompt, with about 30 image reads. Its two earlier idle resumes, at 77k and 99k, were below the 100k threshold. A sweep request above ~100k now counts as a new item, and renders get downscaled before reading.
- **Handoffs had grown to 29–31 KB** in the camat_v2 sessions (~8k tokens at every resume, mostly done-item history); one overflowed `cat` and was read twice. The rule now asks for under ~10 KB, with committed items cut to one line, and for commands that run as written: `delegate` was not on PATH in two sessions (2 calls in one, a 6k-token skill load in the other), and a full `pytest -x` stopped at test 188 of 531 on a known failure.
- **Status requests kept coming:** 7 in two sessions, each after 2–4 silent calls during probe chains, test/fix loops or reads. The rule had been applied to read phases only; it now covers any run of 2+ calls without text.
- **Second opinions, second use** (ae30942b): both models picked the recommended option again, Codex added a risk the question had missed, and Cursor corrected a fixture count (51 s and 59 s). Both said they could not check web facts; probes afterwards showed that Cursor's ask mode has no web tools and Codex's reads only a cache ([verified behaviour](verified-behaviour.md)).
- **What worked:** one probe over six real files settled a fix's scope before any design; tests written first surfaced a second symptom; one side-by-side render of 3–4 encodings settled each placement question; promoted scripts were checked against the previous tool's output on the same input; a doc claim ("beamSpan is not drawn") was probed (41 of 41 drawn) and removed instead of copied forward. A before/after render diff needs generated IDs normalised first (now in the rules).
- **The first control session** (3240a01e, claude-only) did a near-copy job, promoting scratch scripts, which the delegation rule would otherwise hand off. One session says nothing about the comparison; `delegate cost --by-mode` will, once ratings accumulate.
- **Not adopted:** a hook that lints `echo =` in Bash commands (the zsh rule exists; a check on every Bash call isn't worth it for a one-call error), and letting the user pick a delegate-enabled session for mechanical items, which would break the random assignment.

## Still open

- Re-measure on the next long session with `delegate cost --session latest`.
- Orchestrator effort: measured above ([lever 4](cost-analysis.md#4-keep-heavy-reading-and-scripting-out-of-the-main-context-about-4-measured-more-possible)), keep xhigh. For proof, run one routine work item at `high` and compare its thinking tokens and rounds against an xhigh item.
