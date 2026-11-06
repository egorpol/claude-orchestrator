---
name: delegate
description: Delegate coding, review, research or bulk-labelling work from Claude Code to Codex CLI or Cursor's headless agent (cursor-agent, model auto), weighing live quota (openusage) and Artificial Analysis ratings, running each delegate in its own git worktree with automatic checks, cross-vendor review and follow-up rounds, and reviewing its diff before merging. Use when the user asks to offload or parallelise work to Codex, Cursor or "another agent", asks how much Claude/Codex/Cursor quota is left, or asks which model to use for a task. Also use it unprompted before doing mechanical work inline that has a clear spec (tests from a written spec, the same update across several docs, a near-copy of an existing script), because spare Codex and Cursor quota expires unused.
---

# Delegate

One CLI: `~/.claude/skills/delegate/scripts/delegate` (stdlib Python; full path, or `delegate` after `install.sh --bin` links it into `~/.local/bin`). Source of truth is `egorpol/claude-orchestrator`; `~/.claude/skills/delegate` symlinks into it. Commit edits there.

Before doing each workflow step, read the file named there: the field lessons live in those topic files.

## Rules for every delegating session

- Never pass `--force` to cursor-agent: it silently disables the sandbox.
- Cursor defaults to `--model auto`.
- Delegates never commit; nothing auto-commits, and you commit only when the user asks.
- Reviews are read-only; send them to the other vendor by default.
- Launch with `run_in_background: true` and do not poll: the exit packet arrives in the task notification.

## Quick commands

| command | purpose |
|---|---|
| `delegate quota` | Check remaining pools and resets. |
| `delegate route TASK` | Rank delegates; TASK is review, refactor, implement, research or bulk-label. |
| `delegate ask Q` / `delegate ask - --agent both < q.md` | Get read-only answers. |
| `delegate launch AGENT --brief F [--preflight \| --dry-run]` | Prepare or start a run; one flag, not both. |
| `delegate wait NAME` | Receive completion from a background wait. |
| `delegate status NAME` | Inspect the current packet. |
| `delegate review NAME` / `--working-tree --brief F` | Request a review. |
| `delegate followup NAME --brief F` | Send a fix round. |
| `delegate verify NAME` | Run the saved checker again. |
| `delegate diff NAME` | Inspect the patch. |
| `delegate apply NAME` | Bring reviewed changes into the main checkout. |
| `delegate cleanup NAME` | Remove a finished worktree. |
| `delegate cost --session latest` | Measure this session. |
| `delegate report` | Record a field report. |

Read `~/.claude/skills/delegate/reference/commands.md` for the full command table, or run `delegate CMD --help` for flags.

## Workflow

0. Read `~/.claude/skills/delegate/reference/briefs.md` first; fetch inputs, inspect real data and prepare acceptance checks before briefing. Before putting a decision to the user, also read `~/.claude/skills/delegate/reference/sessions.md`.
1. Read `~/.claude/skills/delegate/reference/quota.md` first; check quota, pick a delegate and tell the user why.
2. Read `~/.claude/skills/delegate/reference/briefs.md` first; write a brief for one job per run. State intent, not mechanism; explain the reason behind each constraint and name owned and off-limits files.
   Scale the checker to the job as described under Acceptance scripts in `briefs.md`.
   Put a real-data dry run with expected outcomes in `--verify`, computing expectations by a different code path; for new modules, parsers, guards and data checks, prove the checker fails on a planted failure, and run `--preflight` when the checker is new or reads `--link`ed inputs.
3. Read `~/.claude/skills/delegate/reference/launch.md` first; launch in the background and build your independent checker while the run works.
4. Read `~/.claude/skills/delegate/reference/review.md` first; arrange cross-vendor review for non-trivial changes after your first pass.
5. Read `~/.claude/skills/delegate/reference/review.md` first; review the patch yourself and rerun tests and real-data checks.
6. Read `~/.claude/skills/delegate/reference/review.md` first; adjudicate findings and send accepted fixes through followup rounds.
7. Read `~/.claude/skills/delegate/reference/review.md` first; check and apply the reviewed patch, then verify integrated behaviour.
8. Read `~/.claude/skills/delegate/reference/review.md` first; clean up the run after its changes are accounted for.
9. Read `~/.claude/skills/delegate/reference/review.md` first; verify quantitative claims before outward-facing actions.

## Other topics to read when needed

- `~/.claude/skills/delegate/reference/sessions.md`: session boundaries, when to delegate unprompted, and decision questions.
- `~/.claude/skills/delegate/reference/quota.md`: bulk labelling, long runs and tuning.
- `~/.claude/skills/delegate/reference/agents.md`: per-agent flags, gotchas and the run directory.
