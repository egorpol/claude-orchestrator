# Automatic field reports: design

Decided 2026-10-26, with second opinions from Cursor Auto. A Codex review on 2026-10-27 changed the sampling (1), the pass bar (4) and the leak guards. Build steps 1–4 are done and step 5 has its stage-2 list; the Codex judge and the report pipeline itself are not built yet.

## Goal

About 11% of sessions get a hand-written `/field-report` (12 of the 105 logged sessions from 2026-10-20 to 10-26). The numbers for every session are already automatic: the SessionEnd summary row and the `delegate report` skeleton. What is missing is the lessons from the other 89%. Claude's quota is the scarcest pool, so a spare pool writes them: Cursor Auto, 94% left and ahead of pace on 2026-10-26.

`/field-report` stays for sessions where the user knows what they want recorded.

## Signals, measured on the 105 sessions

| Signal | Sessions with it | Use |
|---|---|---|
| Context max >= 120k | 89 | none: it doesn't tell sessions apart |
| Context-guard nudge | 69 | none on its own |
| Tool result with `is_error` | 56 (max 6) | weak |
| Denied, rejected or hook-blocked call | 6 (max 2) | score |
| Idle cache rebuild | 7 (max 1; up to 22.7% of a session's cost units) | score |
| Cost units in the top quartile | 21 of the 93 without a hand report | score |
| Correction-like typed prompt | 1 | none: users type a median of 1 prompt and steer through pickers |

Transcripts are 1.7 MB at the median, 7.1 MB at most.

A denied or blocked call is an `is_error` tool result that starts with a hook error (`PreToolUse:<tool> hook error`), a permission denial or a user rejection. The first count matched a looser set of phrases anywhere in a result line: 20 of its 22 matches (in 17 sessions) were ordinary results quoting the words, such as a CHANGELOG or a model's answer, and it missed all 5 hook errors (corrected 2026-10-27).

## Decisions

1. **Sampling: friction score plus floors.** Flag a session for a denied or blocked call, an idle rebuild, or top-quartile cost units. That flags 28 of the 93 unreported sessions in the measured week (6 denied, 7 idle rebuilds, 21 top quartile; 37 before the denial count was corrected), so rank them: rebuild share of cost, then denied calls, then cost units. Take the top 15, then add 5 at random from the unflagged sessions and 2 at random from the flagged rest (ranks 16–28 in the measured week): 22 calls in all. Keep at least one session per experiment mode within that budget: the missing mode's top session replaces the lowest-ranked flagged pick. Log why each session was picked (rank, random unflagged, random flagged or mode floor). Skip sessions that have a hand report. The random unflagged share stays because a score built on signals the hooks already catch keeps rediscovering known problems. The flagged draw means sessions ranked below 15 still get read sometimes.
2. **Digest: excerpts in the prompt, compressed transcript as a file.** The prompt is capped at 60,000 characters, as in `delegate rate`, but not blinded. It holds the skeleton numbers, the typed prompts, the picker questions and answers, HANDOFF.md at start and end, the commits in the session window, and ±3 transcript rows around each friction event, with tool results cut to 500 characters. The compressed main thread goes in the agent's working directory as a file, passed with `ask --files`: Claude's visible text plus one line per tool call (tool, short arguments, ok or error, result size). The writer reads it where the excerpts are thin.
3. **Writer: Cursor Auto in two stages, run by hand at fold-in time.** Stage 1 makes one call per session and returns lesson rows: lesson, category (rule, SKILL.md, CLI or hook, project scope), the session's 8-character prefix, and an evidence line quoted from the digest. Rows without a prefix and evidence are dropped. Stage 2 makes one call over all the rows and writes a deduplicated list to `feedback/auto/<date>-lessons.md` (gitignored, like `feedback/`), keeping the citations. It runs from `delegate report --batch --since <last fold-in>`, not from a hook, so it never runs beside a live session and spends quota only on batches that get folded in. One batch call can't take the digests directly, because 5–10 digests at 60k don't fit one prompt.
4. **Validation: a Codex judge on 10 of the 29 sessions that have a hand report**, picked across modes (there are 18 orchestrator, 7 claude-only, 2 vanilla and 2 unlabelled) and transcript sizes (1.2–9.6 MB). The judge scores the stage-2 list, the deliverable, through its citations. For each session it lists the hand report's lessons and marks each one found (1), partly found (0.5) or missed. It also marks each auto lesson as supported or unsupported by the digest. Pass needs all three: pooled recall >= 50% over all the hand reports' lessons, at most 2 of the 10 sessions below 25% recall, and at most 10% unsupported. Lessons are the friction and suggestion bullets: a median of 10 per report (range 7–13), so about 100 for 10 sessions. Unsupported is counted per lesson–session citation, because a merged lesson can carry a false citation. The trial also prints a per-session table; on a long list, 10% can still be several fabrications, so read the table before building `--batch`. The hand reports were written by Claude with full context, so they are a reference, not ground truth. The judge gets each session's transcript file beside its digest when it checks support, because stage 1 may quote either (both second opinions on the stage prompts, 2026-10-27).

## Leak guards (validation)

- All 29 transcripts contain the `/field-report` run and the Write of the report. Cut the digest and the transcript file before the first `/field-report` prompt, then assert that the rows kept hold no `/field-report` command, no field-report Skill call and no Write or Edit under `feedback/`. A plain text search for `/field-report` fails 26 of the 30 sessions that have the prompt, because their closing pickers offer it (2026-10-27). Cut the commits at the same prompt, and check that HANDOFF.md was not written after it, because the end-state copy is taken later. On 2026-10-27, 2 of the 30 sessions with the prompt wrote HANDOFF.md after it, both through Bash (a Python replace, and a helper writing a handoff by path), none through Write or Edit; 1 committed after it. Every one of the 30 mentions HANDOFF.md in a tool call after the prompt, mostly the report quoting it, so the check looks for Write or Edit to a HANDOFF.md path and for Bash commands that name it as a quoted path or a redirect, `tee`, `sed -i`, `cp` or `mv` target: that finds exactly the 2. A session that fails a check is dropped from the trial.
- Cursor `--mode ask` reads files outside its `--workspace` by absolute path (see `verified-behaviour.md`, 2026-10-26), so an empty working directory doesn't keep it away from `feedback/` or the transcripts. `--output-format stream-json` lists each read as a `tool_call` event with its path (`readToolCall` `args.path`, `globToolCall` `args.targetDirectory`; positive control on 2026-10-27). So the trial runs stream-json and fails any run with a tool call whose path lies outside its workspace, or whose tool kind the guard doesn't know. Ask mode also runs shell commands (`ls`) and starts subagents whose reads don't reach the stream (2026-10-27), so shell, subagent and unknown tool kinds fail too, and the whole process group is killed. Built 2026-10-27 as `ask.run_cursor(..., confine=True)`. The guard can't see context Cursor injects without a tool call: its user rules, a skills list and, in 64 of 89 past rating chats, the full field-report SKILL.md. That text names no session, but the stage prompts shouldn't rely on its absence.

## Build order

1. Codex review of this document: done 2026-10-27.
2. The stream-json probe from the leak guards: done 2026-10-27; the stream lists the reads.
3. The outside-read guard: run Cursor with stream-json and fail on tool-call paths outside the workspace. `delegate rate` needs the same guard. Done 2026-10-27; `rate` refuses Codex, whose reads show only as shell strings.
4. Digest builder and session selection, with tests on fake transcripts (delegated). Done 2026-10-27 as `delegate report --select` and `--digest`. Of the 34 sessions with a hand report in the frozen log, 28 build and 6 are dropped: 2 wrote HANDOFF.md after the prompt, and 4 ran the field-report skill without a typed `/field-report`, so they have no cut point.
5. Stage 1 and stage 2 prompts, then the validation trial on 10 sessions. Hand off after the trial's numbers are in. Prompts and runs done 2026-10-27:
   - **Picks**, by rule from the 28 that build (18 orchestrator, 7 claude-only, 2 vanilla, 1 unlabelled): one per mode and the rest in proportion (6/2/1/1), evenly spaced by transcript size within a mode, and a session with an idle rebuild or a denied call swapped in for the nearest-size pick in a mode that had one but no such pick.
   - **Stage 1** returns JSON Lines. A row is kept only if its evidence is found verbatim (whitespace-normalised) in the digest or the transcript file. A run whose stream shows no complete read of the transcript file is retried once with the file inlined.
   - **Stage 2** cites row ids, every id exactly once, and code attaches each row's session and evidence, so no quote is copied twice.
   - **Result on Cursor Auto:** all 10 runs read the transcript file in full; 69 rows kept and 1 dropped (a near-miss quote), 4–9 per session against the hand reports' median of 10 lessons. Stage 2 merged them into 63 lessons on its first attempt. 12 calls in all, 18 minutes, 275k input and 69k output tokens.
6. The `--batch` command, plus a fold-in note in `CLAUDE.md`, only if the trial passes.
