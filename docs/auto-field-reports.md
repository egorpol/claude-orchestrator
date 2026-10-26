# Automatic field reports: design

Decided 2026-10-26. Not built yet. Second opinions came from Cursor Auto; Codex is to review this document next and reopen a point only if it finds a risk.

## Goal

About 11% of sessions get a hand-written `/field-report` (12 of the 105 logged sessions from 2026-10-20 to 10-26). The numbers for every session are already automatic: the SessionEnd summary row and the `delegate report` skeleton. What is missing is the lessons from the other 89%. Claude's quota is the scarcest pool, so a spare pool writes them: Cursor Auto, 94% left and ahead of pace on 2026-10-26.

`/field-report` stays for sessions where the user knows what they want recorded.

## Signals, measured on the 105 sessions

| Signal | Sessions with it | Use |
|---|---|---|
| Context max >= 120k | 89 | none: it doesn't tell sessions apart |
| Context-guard nudge | 69 | none on its own |
| Tool result with `is_error` | 56 (max 6) | weak |
| Denied, rejected or hook-blocked call | 16 (max 2) | score |
| Idle cache rebuild | 7 (max 1; up to 22.7% of a session's cost units) | score |
| Cost units in the top quartile | 21 of the 93 without a hand report | score |
| Correction-like typed prompt | 1 | none: users type a median of 1 prompt and steer through pickers |

Transcripts are 1.7 MB at the median, 7.1 MB at most.

## Decisions

1. **Sampling: friction score plus floors.** Flag a session for a denied or blocked call, an idle rebuild, or top-quartile cost units. That flags 37 of the 93 unreported sessions in the measured week, so rank them: rebuild share of cost, then denied calls, then cost units. Take the top 15, add 5 at random from the unflagged rest, and keep at least one per experiment mode. Skip sessions that have a hand report. The random share stays because a score built on signals the hooks already catch keeps rediscovering known problems.
2. **Digest: excerpts in the prompt, compressed transcript as a file.** The prompt is capped at 60,000 characters, as in `delegate rate`, but not blinded. It holds the skeleton numbers, the typed prompts, the picker questions and answers, HANDOFF.md at start and end, the commits in the session window, and ±3 transcript rows around each friction event, with tool results cut to 500 characters. The compressed main thread goes in the agent's working directory as a file, passed with `ask --files`: Claude's visible text plus one line per tool call (tool, short arguments, ok or error, result size). The writer reads it where the excerpts are thin.
3. **Writer: Cursor Auto in two stages, run by hand at fold-in time.** Stage 1 makes one call per session and returns lesson rows: lesson, category (rule, SKILL.md, CLI or hook, project scope), the session's 8-character prefix, and an evidence line quoted from the digest. Rows without a prefix and evidence are dropped. Stage 2 makes one call over all the rows and writes a deduplicated list to `feedback/auto/<date>-lessons.md` (gitignored, like `feedback/`), keeping the citations. It runs from `delegate report --batch --since <last fold-in>`, not from a hook, so it never runs beside a live session and spends quota only on batches that get folded in. One batch call can't take the digests directly, because 5–10 digests at 60k don't fit one prompt.
4. **Validation: a Codex judge on 10 of the 29 sessions that have a hand report**, picked across modes (there are 18 orchestrator, 7 claude-only, 2 vanilla and 2 unlabelled) and transcript sizes (1.2–9.6 MB). For each session the judge lists the hand report's lessons and marks each one found (1), partly found (0.5) or missed in the auto output. It also marks each auto lesson as supported or unsupported by the digest. Pass: mean recall >= 50% and at most 1 unsupported lesson per report. The hand reports were written by Claude with full context, so they are a reference, not ground truth.

## Leak guards (validation)

- All 29 transcripts contain the `/field-report` run and the Write of the report. Cut the digest and the transcript file before the first `/field-report` prompt, then assert that neither contains `/field-report` or a Write to `feedback/`. A session that fails the check is dropped from the trial.
- Cursor `--mode ask` reads files outside its `--workspace` by absolute path (see `verified-behaviour.md`, 2026-10-26), so an empty working directory doesn't keep it away from `feedback/` or the transcripts. Before the trial, find out whether `--output-format stream-json` lists the paths it reads, using a positive control: a question that makes it read a known outside file. If it does, a run that reads anything outside its workspace fails. If it doesn't, the build session decides on another guard before running the trial.

## Build order (the session after this one)

1. Codex review of this document: `~/.claude/skills/delegate/scripts/delegate ask - --agent codex --effort high`, with this file named in the question.
2. The stream-json probe from the leak guards.
3. Digest builder and session selection, with tests on fake transcripts (delegated).
4. Stage 1 and stage 2 prompts, then the validation trial on 10 sessions. Hand off after the trial's numbers are in.
5. The `--batch` command, plus a fold-in note in `CLAUDE.md`, only if the trial passes.
