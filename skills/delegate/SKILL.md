---
name: delegate
description: Delegate coding, review, research or bulk-labelling work from Claude Code to Codex CLI or Cursor's headless agent (cursor-agent, model auto), weighing live quota (openusage) and Artificial Analysis ratings, running each delegate in its own git worktree with automatic checks, cross-vendor review and follow-up rounds, and reviewing its diff before merging. Use when the user asks to offload or parallelise work to Codex, Cursor or "another agent", asks how much Claude/Codex/Cursor quota is left, or asks which model to use for a task.
---

# Delegate

One CLI: `~/.claude/skills/delegate/scripts/delegate` (stdlib Python; full path). Source of truth is `egorpol/claude-orchestrator`; `~/.claude/skills/delegate` symlinks into it. Commit edits there.

| command | does |
|---|---|
| `delegate quota [--source direct]` | % left, reset and pace for claude-5h/7d, codex-5h/7d, cursor-auto, cursor-premium |
| `delegate route <task> [--prefer IDs]` | ranks for `review`, `refactor`, `implement`, `research`, `bulk-label`; honours catalog `prefer` and `--prefer` |
| `delegate ratings [--grep RE] [--refresh]` | AA intelligence / coding / tok/s / $ for catalog (or any AA) models |
| `delegate ask QUESTION [--files F]…` | cheap read-only one-shot (default Cursor auto); cites `path:line` without filling orchestrator context |
| `delegate cost [--session latest] [--since D] [--repo R]` | summarises run-log usage (`runs.jsonl`) or the orchestrator session's size and cost |
| `delegate launch codex\|cursor --brief F [--verify CMD] ...` | worktree on `delegate/<name>`, brief + contract, background run, log, last message, checks |
| `delegate review NAME [--agent ...] [--focus TEXT]` | read-only review; by default the *other* vendor |
| `delegate followup NAME --brief F \| --from-review N [--fresh --model M] [--link PATH]` | next round in the same worktree, resuming the session; `--link` as on launch |
| `delegate verify NAME [--cmd C]` | re-run the check in the worktree |
| `delegate status [NAME] [--full]` | compact exit packet (default); `--full` for log tail and whole last message |
| `delegate diff NAME` / `apply NAME [--check]` / `cleanup NAME` | inspect, merge, tidy up. Successful `apply` appends to `runs.jsonl` in the runs root (what `cost` summarises) |

## Sessions

One work item per orchestrator session. After an item is merged or committed, write a handoff (memory files plus a short `HANDOFF.md`: state, open PRs or runs, next step), then suggest a fresh session or `/clear`. The context-guard hook (`hooks/context-guard.py`, via `./install.sh --hooks`) reminds at about 250k tokens and again every 150k. `delegate cost --session latest` shows the current session's size and cost.

## Workflow

0. **Prefetch and inspect.** Delegates have no network: fetch every input first. Inspect the real data before writing rules into a brief.
1. **Quota and pick.** `delegate route <task>` (includes quota). One line to the user: which delegate and why. openusage can lag ~1h (`DATA AGE`); use `--source direct` near a limit. Batch independent checks in one Bash call, or parallel tool calls in one message.
2. **Brief.** Pick a template from `templates/` by job type (`implement`, `mechanical`, `review`, `research`). Stable repo conventions belong in the target repo's `AGENTS.md` (verified 2026-10-10: headless Codex reads only `AGENTS.md`; `cursor-agent` reads `AGENTS.md`, `CLAUDE.md` and `.cursor/rules` with `alwaysApply`). Briefs cite it and carry only the deltas. State **intent, not mechanism** — field lesson: "derive must not add columns" made Codex skip new columns; the intent was "prove derive reproduces r012". Cover owned / off-limits files; put a real-data dry run with expected outcomes in `--verify`. Give the reason behind each constraint (Codex follows them literally). One job per run. GPT models like tagged sections (`<task>`, `<verification_loop>`, `<default_follow_through_policy>`). The launcher appends the contract (no commits, stay in worktree, flag conflicts, fixed final-message format); do not repeat it.
3. **Launch** with Bash and `run_in_background: true`. **Do not poll.** When the run ends, the exit packet (≤20 lines: state, verify, top files, Digest risks, usage, artifact paths) arrives in the task notification — that *is* the status.
   ```
   delegate launch codex  --brief B.md --name feat-x --link data/cache --verify 'pytest -q'   # gpt-6.1-sol @xhigh
   delegate launch cursor --brief B.md --name feat-y --verify 'pytest -q'                     # model auto
   delegate launch codex  --brief B.md --read-only --name research-z                          # review/research
   ```
   - `--verify CMD` runs after exit in the cleaned worktree env → `verify.log` / `verify_code`. Pass the real test command, not one piped through `tail` (masks exit code).
   - `--link PATH` symlinks a gitignored path from the main checkout and excludes it in `.git/info/exclude`.
   - Parallel runs need disjoint files. `--dry-run` prints `run.sh` and the prompt without creating anything.
   - Use `status NAME` only for runs not launched in this session; `--full` only when the packet flags something.
4. **Cross-review** (non-trivial changes): `delegate review NAME` in the background. Other vendor by default (`--agent` / `--focus` override). Reviewer gets the brief plus the diff at `wt/.delegate/review-N.patch`. Returns Verdict, findings, spec compliance. `status` warns if a reviewer changed the worktree.
5. **Your own review. Never trust a summary.** Risk-based:
   - **Data / semantic modules** (parsers, validation, matching, accounting, safety guards): read the whole patch.
   - **UI, docs, plumbing:** review the packet's file list and Digest risks, then read only the flagged spots.
   - Send large documents to `delegate ask` instead of reading them here.
   - Re-run tests and the dry run yourself against real data. Adjudicate each reviewer finding; some are wrong.
6. **Fix rounds.** Brief with accepted findings (or `--from-review N`), then `delegate followup NAME --brief fixes.md`. Archives to `round-<k>/`, resumes the same session and `--verify`. After ~3 rounds without convergence: `--fresh --model <stronger>`.
7. **Merge.** `delegate apply NAME --check`, then `delegate apply NAME` (uncommitted on the main tree). Commit only when the user asks. `--3way` if the main tree moved. After merging several delegates' work: one real integrated end-to-end pass with the same cache state as production (cold if production is cold), plus a preflight of offline inputs before long network phases.
8. **Clean up.** `delegate cleanup NAME` removes worktree and branch, keeps the run record; refuses while unapplied. `--purge` deletes the record; `--force` discards changes.
9. **Before outward-facing actions** (emails, scheduled crawls, publishing): check every quantitative claim in docs and briefs against the code that executes. Field incident: docs said ≤1 request/s while code paced 0.1 s apart.

Delegates' summaries can be wrong when their code is right (Composer misquoted a brief). Judge the patch and your own re-run.

## Run directory

`~/.cache/claude-delegate/runs/<repo>/<name>/` (`--root` or `$CLAUDE_DELEGATE_ROOT`):
- current round: `brief.md` · `prompt.md` · `run.sh` · `log` · `last-message.md` · `exit_code` · `verify.log` / `verify_code`
- `round-<k>/`, `review-<n>/` (plus `pre_digest` / `post_digest`)
- `meta.json`, `wt/`, `changes.patch` (after `diff`), `applied` (after `apply`)

## Agents

- **Codex** (`codex exec --json`): `-s workspace-write` or `read-only`, `-C <worktree>`, `-o last-message.md`, prompt on stdin. Per-round usage from `turn.completed.usage` lands in meta history. Follow-ups: `codex exec resume <session-id>` (id from log; sandbox via `-c sandbox_mode=…`). Models: `gpt-6.1-sol` (proven), `gpt-6-astra` (~5× $/token), `gpt-5.6-terra` (fast, mechanical). Effort `low`…`xhigh`, then `max`.
- **Cursor** (`cursor-agent -p`, `~/.local/bin`): **`--model auto`** stays the default (Auto pool, SpaceX in-house). Named Grok (`cursor-grok`, `grok-4.7-*`) and Composer (`composer-2.5*`) draw on Cursor's "Cursor Models" quota, not premium (Cursor spending tab, 2026-10-10); openusage has no metric for it, so `route` shows QUOTA `?`. Use named models only if the user asks.
  - Runs with `--sandbox enabled --trust --workspace <worktree> --output-format stream-json`. Sandboxed shell auto-runs; writes outside fail with EPERM; no network.
  - **Never add `--force`**: it silently bypasses the sandbox (tested 2026-10-09). `--unsandboxed` (network, installs) needs the user's OK.
  - `--mode ask` (`--read-only` and Cursor reviews) **cannot run shell**; reads only.
  - Follow-ups: `--resume <session_id>` from the stream init event. If `cursor-agent status` says "Not logged in", the user runs `cursor-agent login`.
- **Claude subagents** (Agent tool, `sonnet|haiku`) appear in `route` for comparison; 0.8 penalty (orchestrator's Claude pool).
- **Codex plugin** (`openai/codex-plugin-cc`): `/codex:review` on the *current checkout*. Use `delegate` for anything that writes.

## Gotchas

- The launcher strips Cursor AppImage variables (`LD_LIBRARY_PATH`, `/tmp/.mount_*` PATH entries), `VSCODE_*`, `CURSOR_*`, `CLAUDE_CODE_*` (including the messaging token), and virtualenvs that belong to another repo.
- Python editable installs: inside the worktree, `import pkg` can resolve to the main checkout. Check with `python -c "import pkg; print(pkg.__file__)"` or use `PYTHONPATH=.`.
- A gitignore rule `dir/` does not match a symlink; `--link` writes exact paths (and `/.delegate`) to `.git/info/exclude`.
- Untracked files count as changes. `run.sh` exports `PYTHONDONTWRITEBYTECODE=1`, so test runs leave no `.pyc`; other build output (`node_modules`, `dist`) needs a `.gitignore` in the target repo, or it lands in the patch.

## Tuning

`scripts/catalog.json`: delegates (agent, model, effort, AA slug, `aa_proxy`, pools, notes), task weights, `prefer`, `min_quota_pct` (default 10), subagent penalty. Find AA slugs with `delegate ratings --grep 'gpt-6|grok'`. Without `ARTIFICIAL_ANALYSIS_API_KEY`, ratings come from the public leaderboard ("coding" = Terminal-Bench 4.0 + SciCode mean). Cache: `~/.cache/claude-delegate/aa-models.json` (24h).
