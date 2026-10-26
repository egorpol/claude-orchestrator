# Field results

Numbers come from `delegate cost --session` on each session's transcript and include the calls that wrote the report. Cost units weigh input 1, cache read 0.1, 1h cache write 2, output 5. Compare sessions per call, because the work differs. `mode` is the session's drawn mode in the control-session experiment (hook registered 2026-10-13 10:29); — means the session started before it, and none that no draw was logged. Field reports are kept locally in `feedback/` (gitignored since 0.1.0) and written with `delegate report` / `/field-report`; new sections are added here when reports are folded in.

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
| 2026-10-13 | c3057ac0 | none | 68 | 183k | 1.47M | 21.6k | 0 |
| 2026-10-13 | f002ba21 | orchestrator | 106 | 171k | 1.75M | 16.5k | 0 |
| 2026-10-14 | 67cbd1d1 | orchestrator | 61 | 171k | 1.30M | 21.3k | 0 |
| 2026-10-14 | a14926ff | orchestrator | 48 | 163k | 1.16M | 24.2k | 0 |
| 2026-10-14 | 2991ecc1 | claude-only | 74 | 250k | 2.34M | 31.6k | 0 |
| 2026-10-14 | 8e2592ed | orchestrator | 66 | 255k | 2.04M | 30.9k | 0 |
| 2026-10-14 | b4f23cf6 | orchestrator | 88 | 151k | 1.63M | 18.6k | 14.2% |
| 2026-10-14 | 0efc0a5f | orchestrator | 101 | 243k | 2.57M | 25.5k | 0 |
| 2026-10-17 | 2ebc2f69 | claude-only | 90 | 328k | 3.34M | 37.1k | 0 |
| 2026-10-17 | b25535bb | vanilla | 78 | 241k | 2.15M | 27.6k | 0 |
| 2026-10-17 | 471f185b | vanilla | 90 | 202k | 1.82M | 20.2k | 0 |
| 2026-10-17 | 0b474ff6 | orchestrator | 79 | 170k | 1.66M | 21.0k | 9.8% |
| 2026-10-19 | c3b22c1e | orchestrator | 68 | 169k | 1.65M | 24.3k | 16.1% |
| 2026-10-19 | 4cd72052 | claude-only | 52 | 118k | 0.81M | 15.6k | 0 |
| 2026-10-20 | f4ddbc11 | orchestrator | 55 | 181k | 1.58M | 28.6k | 17.3% |
| 2026-10-20 | 63a01e88 | orchestrator | 88 | 221k | 2.07M | 23.5k | 0 |
| 2026-10-20 | f542404f | orchestrator | 51 | 155k | 1.03M | 20.1k | 0 |
| 2026-10-21 | aa93d7cd | orchestrator | 54 | 126k | 0.95M | 17.6k | 0 |
| 2026-10-21 | 2a0eae26 | orchestrator | 59 | 210k | 1.78M | 30.1k | 0 |
| 2026-10-21 | 7716b5cb | orchestrator | 55 | 127k | 0.95M | 17.3k | 0 |
| 2026-10-21 | 404ba9fe | orchestrator | 39 | 123k | 0.77M | 19.8k | 0 |
| 2026-10-21 | 46869c36 | claude-only | 48 | 154k | 1.07M | 22.3k | 0 |
| 2026-10-22 | ea48f2bb | claude-only | 86 | 163k | 1.54M | 18.0k | 0 |
| 2026-10-23 | d4b6e9de | orchestrator | 71 | 161k | 1.33M | 18.7k | 0 |
| 2026-10-23 | 48c1baa2 | orchestrator | 89 | 275k | 2.83M | 31.8k | 0 |
| 2026-10-26 | e464ce15 | claude-only | 80 | 198k | 1.82M | 22.7k | 0 |

47 sessions, 3,950 calls, 112.2M cost units; idle rebuilds 9.1M (8.1%). The 46 sessions after the marathon averaged 24.2k units per call.

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

## 2026-10-13 to 10-20: fifteen sessions, six projects

Each closed with `/field-report`. Numbers from `delegate cost --session <id>`, so they include the calls after each report, and idle shares come out a little lower than in the reports. Modes: 9 orchestrator, 3 claude-only, 2 vanilla, and c3057ac0 without a draw (its session log has no start row). Claude 7d is the share left when the report was written; the pool reset before f4ddbc11.

| session | work | delegates | calls | context at end | cost units | units per call | idle rebuild | Claude 7d |
|---|---|---|---:|---:|---:|---:|---|---|
| c3057ac0, egorpol.github.io: two delegate audits verified into one list, note feedback | 2 items | none (cleaned up two audit runs) | 68 | 183k | 1.47M | 21.6k | none | 59 |
| f002ba21, egorpol.github.io: guided audit fixes 1–8, one step per user OK | 1 item | `ask --agent both` | 106 | 171k | 1.75M | 16.5k | none | 57 |
| 67cbd1d1, camat_v2: Nextcloud integrity check (a corrupt git object repaired), refactor spec | 2 items | `ask --agent both` | 61 | 171k | 1.30M | 21.3k | none | 48 |
| a14926ff, camat_lectures: seminar outline with tested format examples, then a revision | 2 items | `ask --agent both` | 48 | 163k | 1.16M | 24.2k | none | 47 |
| 2991ecc1, camat_lectures: live page with four in-browser demos (claude-only) | 1 item | none | 74 | 250k | 2.34M | 31.6k | none | 43 |
| 8e2592ed, camat_v2: a 757-line notebook helper cell moved into the package | 1 item | Codex build (1 round), Cursor review | 66 | 255k | 2.04M | 30.9k | none | 41 |
| b4f23cf6, camat_lectures: clean-env notebook runs, site published, fetch script | 3 items | `ask --agent both` | 88 | 151k | 1.63M | 18.6k | 87 min (14.2%) | 37 |
| 0efc0a5f, camat_lectures: demo and notebook polish from one typed list, review, deploy | 1 prompt, ~8 tasks | `ask --agent both` | 101 | 243k | 2.57M | 25.5k | none | 35 |
| 2ebc2f69, job-webcrawler: review, new sources, keyword alignment, PR (claude-only) | 1 item | none | 90 | 328k | 3.34M | 37.1k | none | 22 |
| b25535bb, webern_op27: notes rewrite, static Pages viewer built and browser-tested (vanilla) | 1 item | none | 78 | 241k | 2.15M | 27.6k | none | 16 |
| 471f185b, webern_op27: release wording, Pages, facsimile repo, paste guide (vanilla) | 4 items | none | 90 | 202k | 1.82M | 20.2k | none | 13 |
| 0b474ff6, webern_op27: paste-and-verify loop for seven upstream posts | 1 item | none | 79 | 170k | 1.66M | 21.0k | 237 min (9.8%) | 12 |
| c3b22c1e, audiospylt: notebook fixtures, paths, legacy archive | 1 item | none | 68 | 169k | 1.65M | 24.3k | 61 min (16.1%) | 10 |
| 4cd72052, audiospylt: headless notebook runner and CI job, sample provenance (claude-only) | 3 items | none | 52 | 118k | 0.81M | 15.6k | none | 8 |
| f4ddbc11, audiospylt: review of an uncommitted step, a design decision after probes | 2 items | 2 × `ask --agent both` | 55 | 181k | 1.58M | 28.6k | 204 min (17.3%) | 100 |

- **One item grew past 240k in five sessions** (241–328k): a handoff that bundled a site, four demos and three notebooks; a 757-line refactor; a crawler review; a viewer build; an eight-task typed list. The guard fired mid-item in each, where its handoff advice can't apply. These five ran 25.5k–37.1k units per call, the other ten 15.6k–28.6k. The rules now let an item stop at a step boundary above ~120k and ask a handoff to name one deliverable per session; the guard's in-item text points at step boundaries, counted output and ranged reads. In 2ebc2f69 one full read of a 2.8k-line file carried ~40k tokens through 70 calls, ~14% of the session's cache reads.
- **Four more idle rebuilds, 9.8–17.3% each:** a `/field-report` typed 204 min after an item closed with a prose line instead of a picker (f4ddbc11), a picker answered after 61 min at 133k (c3b22c1e), a hand-paste loop paused for 237 min (0b474ff6), and an 87-min gap at 116k (b4f23cf6). The guard's idle hold watches typed prompts only, so the picker answer went through. The rules now close every item with a picker that offers "`/clear`, then `/next`" and `/field-report`, and refresh the handoff before a picker or hand-paste step above ~100k.
- **The handoff sat outside the project root three times** (`private/` in one project because of Jekyll, `revision/` in another), at 1–2 calls each. `/next` now looks one folder down and in the project memory; the rule asks for the root plus `.git/info/exclude`.
- **`cd` stuck in 5 of the 15 sessions despite the rule**; in one, a strict build failed ~50 calls later on a relative snippet path. A subshell `(cd sub && …)` leaves the Bash tool's directory alone ([verified behaviour](verified-behaviour.md)), and the rule now says so. A hook that flags the bare form is on the Roadmap.
- **9 status requests in three sessions**, all after an unexpected result, before a long call (venv builds, a 10-minute test run) or in a run of Writes. In a fourth session the user lost track of the plan at 133k. The status rule now covers those points and asks for the plan position in plain words on a resume.
- **Checks in the audience's own tool.** Examples prepared for upstream maintainers passed the project's schema and failed in the maintainers' online editor (~16 calls and a round trip). nbclient stalled on a widget notebook, and a real JupyterLab showed the Bokeh failure; a fresh venv found two doc errors that the project's execute check missed because it skips the install cell. A CSP hash held only at the harness's 1280 px, and 53 unit tests passed while a keyword change moved 6 off-topic posts into the top tier. Each is now a data-check rule.
- **Delegation was rare.** The nine orchestrator sessions launched one delegate run between them (8e2592ed): the byte-identity baseline was proven both ways before launch, Codex passed it in round 1, and four small fixes after the Cursor review went in inline. Second opinions ran in six sessions, in the background, without waiting. In f4ddbc11 `ask --agent both --effort high` reviewed uncommitted main-tree work and found three real defects, which `delegate review` can't do (Roadmap: `--working-tree`). SKILL.md now covers acceptance scripts written for the main tree and a tutorial checker that always exits 0.
- **A control session overrode a typed request:** in 2ebc2f69 the user asked for Cursor, the claude-only draw blocked it, and all 90 calls ran on Claude. The control line now says to tell the user in one line.
- **`delegate report` fixes.** Two audit runs launched in one session and cleaned up in the next (c3057ac0) were copied into the second report; copied runs now keep their launch session, and the skeleton names it. A session's `delegate ask` calls were missing from the skeleton; it now counts them from the session log.
- **This repo's own tests caused 912 Nextcloud sync errors** (found in 67cbd1d1, all under `claude-orchestrator/.delegate/tmp/`): the documented test command put TMPDIR inside the synced checkout, and one test set file mtimes to 1970. TMPDIR now stays outside, test mtimes are plausible, and a secret-guard test that passed only inside a git checkout now runs in a real repo.
- **Not adopted:** a shared `cdp.mjs` helper for headless browser checks (the traps went into one rule line instead), a guard that names a clean, pushed tree before Q&A, and project-specific shell notes (`gh run list --commit` needs a full SHA, ugrep's limits on long patterns, `uv run --with` for one-off parsing). Worktree metadata under `.git/worktrees` drew transient "could not get modification time" warnings in four synced repos, which comes with keeping git worktrees in a synced folder.

## 2026-10-13 to 10-20: control-session arms, first numbers

From 2026-10-13 10:29 the session-mode hook drew each session's mode, at weights 50/25/25: 57 drawn sessions, 32 orchestrator, 11 claude-only and 14 vanilla. Another 17 sessions were excluded from the draw and 1 was unassigned; those are left out. The numbers come from `delegate cost --by-mode`, intent-to-treat: each session stays in the arm it was drawn into. Items come from `delegate rate` with rubric v2: Cursor Auto, blinded to the mode, splits each session into items from its typed prompts, its HANDOFF.md at start and end, and its commits ([verified behaviour](verified-behaviour.md)). USD is list price, including subagents; field-report items are left out of the item counts, but their USD stays in.

| arm | sessions (with cost) | calls | USD | rated sessions | items (finished) | USD per finished item | unclear counted as finished | Claude 7d points, estimated | delegate runs, `ask` calls |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| orchestrator | 32 (28) | 1,643 | 111.38 | 28 | 36 (21) | 5.30 | 3.98 | 40.2 | 8, 14 |
| claude-only | 11 (10) | 620 | 51.80 | 10 | 13 (9) | 5.76 | 4.71 | 18.7 | 0, 0 |
| vanilla | 14 (8) | 530 | 35.65 | 8 | 11 (7) | 5.09 | 3.96 | 12.9 | 0, 0 |

- **Too few items to compare the arms.** USD per finished item is 5.09–5.76 under v2. Under v1, on 39 of these sessions, it was 5.18–6.54. One more finished item would lower vanilla's figure by about 0.6.
- **Unclear items set the range.** Counted as finished, they bring the arms to 3.96–4.71. 10 of the 11 unclear items are in sessions that left no end-state HANDOFF.md copy. From 2026-10-20 SessionEnd saves one, so new sessions should settle more items. Under v1, 27 of 64 items were unclear, 14 of them field reports (11, 4 and 2 per arm, now counted apart).
- **Claude pool points are estimated, not read.** The estimate is list-price USD times the saved fit (claude-7d 0.361 points per USD), about 1.4, 1.9 and 1.6 claude-7d points per session. The SessionStart-to-SessionEnd reading deltas sum to 57.0, 28.0 and 110.0, but they also count other sessions' use and the hours a session stays open: three vanilla sessions left open for 21–92 h read 103 points between them.
- **Delegation stayed in the orchestrator arm.** It had all 8 delegate runs (all applied, with 7 review rounds and 2 follow-ups) and all 14 `ask` calls, with Codex at 0.68M input, 12.2M cached and 0.23M output tokens, and Cursor at 1.05M, 9.8M and 0.20M. Codex and Cursor reading deltas also rise in claude-only and vanilla sessions, from other sessions' use, so those pools have no per-session estimate yet. Claude-only sessions made 138 Claude subagent calls, counted in their USD. The hook blocked one delegate call in a vanilla session.
- **10 drawn sessions are startup ghosts** that ended before any call, with no transcript (6 vanilla, 3 orchestrator, 1 claude-only): 9 within 2 s of the start, one cleared after 5 s. They have no cost, so vanilla's USD covers 8 of its 14 sessions. `--by-mode` now lists them in a `GHOST` column and leaves them out of the per-session means, which they had lowered: vanilla's calls per session are 66.3 without them, 37.9 with them.

## 2026-10-20 to 10-26: eleven sessions, four projects

Each closed with `/field-report`; folded in on 2026-10-26. Numbers from `delegate cost --session <id>`. Modes: 8 orchestrator, 3 claude-only (weights 50/25/25 until 2026-10-26). Claude 7d is the share left when the report was written, on Pro.

| session | delegates | calls | context at end | cost units | units per call | idle rebuild | Claude 7d |
|---|---|---:|---:|---:|---:|---|---|
| 63a01e88, audiospylt: pitch spelling, MEI notation and Verovio render | Codex build, Cursor review | 88 | 221k | 2.07M | 23.5k | none | 95 |
| f542404f, audiospylt: transform and morph modules, own independent checker | Codex build, Cursor review | 51 | 155k | 1.03M | 20.1k | none | 88 |
| aa93d7cd, camat_v2: lazy package init, tests, wheel probe, PR | none | 54 | 126k | 0.95M | 17.6k | none | 60 |
| 2a0eae26, camat_v2: web-edition template, builder and tests, ~60 KB written inline | none | 59 | 210k | 1.78M | 30.1k | none | 57 |
| 7716b5cb, ddt_1_vol_11: commit cleanup, rebase onto collaborators' commits, report refresh, push | none | 55 | 127k | 0.95M | 17.3k | none | 76 |
| 404ba9fe, ddt_1_vol_11: release mode compares bytes with the release tag | `ask --agent both` | 39 | 123k | 0.77M | 19.8k | none | 63 |
| 46869c36, webern_op27: viewer display settings ported from another repo (claude-only) | none | 48 | 154k | 1.07M | 22.3k | none | 61 |
| ea48f2bb, audiospylt: dependency gate fix, review by Claude's own subagent, commit, CI (claude-only) | none | 86 | 163k | 1.54M | 18.0k | none | 38 |
| d4b6e9de, audiospylt: test exports from an external analysis tool, CLI probes | `ask --agent both` | 71 | 161k | 1.33M | 18.7k | none | 22 |
| 48c1baa2, audiospylt: SDIF reader and writer, fixtures, docs, commit | none (believed delegation blocked) | 89 | 275k | 2.83M | 31.8k | none | 13 |
| e464ce15, ddt_1_vol_11: a newly combined work checked against the corpus format, added to the web edition (claude-only) | none | 80 | 198k | 1.82M | 22.7k | none | 3 |

- **Delegation stopped after 10-20.** Both 10-20 sessions ran a Codex build with a Cursor review. The six orchestrator sessions from 10-21 launched nothing and asked twice. In 48c1baa2 a control session's leftover line in the handoff ("work inline, delegation is blocked") was read as fact, and 27 tests and the docs were written inline; 2a0eae26 wrote ~60 KB of new code inline. On 2026-10-26, control sessions stopped writing their mode into handoffs, orchestrator sessions delete such leftovers, SessionStart names the pools ahead of pace, the rules say "delegate by default", one mid-session nudge fires, and the draw moved to 70/15/15.
- **The injected rules were persisted, not shown.** From `c61e134` the SessionStart text (12.6 KB) went past the harness's inline limit: sessions got a ~2 KB preview, and 65 of 68 opened the file with one extra call. Hygiene and delegation rules are now cut to 6.8 KB, 7,404 bytes with the mode line and the `reference.md` pointer, below the 7,925 bytes measured to stay inline. Narrow tips moved to `rules/reference.md`, which the hook names by absolute path.
- **Writing, not reading, grew the long sessions.** 48c1baa2 ended at 275k after one prompt ran from 38k to 266k, and 2a0eae26 reached 210k writing ~60 KB. The item rule now sizes a step by what it writes and asks the handoff to name the step's internal boundary.
- **No idle rebuilds in eleven sessions**, against four (9.8–17.3% each) in the previous fifteen.
- **A decided policy hid its cost.** A fill policy picked without a measurement made one file 59× larger at build time and needed a new picker. Picker options now carry numbers measured on the worst real input.
- **Rendered output needs eyes.** In 63a01e88, 1,084 passing tests and a review APPROVE missed reversed labels in a render. SKILL.md step 5 now asks for one look at a real render and gives the reviewer the PNG.
- **CLI fixes.** `--link` hid a path meant to be committed in the main tree via the shared `.git/info/exclude`, and cleanup then refused: launch now warns, cleanup removes the lines the run added, and links never enter a patch. A 1.4 MB schema would have filled the review patch: per-file diffs over 200 KB now go in as a header and a note. Cursor's `**Pick:**` after a preamble escaped `grep ^Pick`: `ask --agent both` now ends with one `Pick (agent):` line each.
- **Commands.** A closing picker offered `/field-report`, which the model could not invoke (46869c36); it is model-invocable now. `/next` opens with "handoff predates N commits / M dirty files" when the handoff is older than the latest commit (e464ce15).
- **Not adopted:** project-specific tips (SVG hover on painted children, assertions on markup form, post-merge regeneration paths, a `GIT_INDEX_FILE` export) and the triage's "inline is cheaper for ≤5-call rewrites", which the delegate-by-default decision supersedes apart from the <~10-line exception.

## Still open

- Control sessions: USD per finished item needs many more items per arm before the arms can be compared. Re-read `delegate cost --by-mode` as sessions accumulate.

- Re-measure on the next long session with `delegate cost --session latest`.
- Orchestrator effort: measured above ([lever 4](cost-analysis.md#4-keep-heavy-reading-and-scripting-out-of-the-main-context-about-4-measured-more-possible)), keep xhigh. For proof, run one routine work item at `high` and compare its thinking tokens and rounds against an xhigh item.
