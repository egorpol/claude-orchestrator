---
name: delegate
description: Delegate coding, review, research or bulk-labelling work from Claude Code to Codex CLI or Cursor's headless agent (Auto, Composer, Grok), weighing live quota (openusage) and Artificial Analysis ratings, running each delegate in its own git worktree and reviewing its diff before merging. Use when the user asks to offload or parallelise work to Codex, Cursor, Composer, Grok or "another agent", asks how much Claude/Codex/Cursor quota is left, or asks which model to use for a task.
---

# Delegate

One CLI: `~/.claude/skills/delegate/scripts/delegate` (stdlib Python; call it by full path).

| command | does |
|---|---|
| `delegate quota [--source direct]` | % left, reset time and pace for claude-5h/7d, codex-5h/7d, cursor-auto (Auto+Composer), cursor-premium |
| `delegate route <task>` | ranks delegates for `review`, `refactor`, `implement`, `research`, `bulk-label` and prints the launch command for the pick |
| `delegate ratings [--grep RE] [--refresh]` | Artificial Analysis intelligence / coding / tok/s / $ for catalog models (or any AA model) |
| `delegate launch codex\|cursor --brief F ...` | worktree on branch `delegate/<name>`, brief + contract, background run, log, last message |
| `delegate status [NAME]` | all runs, or one run's state, changed paths, log tail and last message |
| `delegate diff NAME` / `apply NAME [--check]` / `cleanup NAME` | review, merge, tidy up |

## Workflow

1. **Quota and pick.** Run `delegate route <task>`, which includes quota. Tell the user in one line which delegate and why. openusage daemon data can be about an hour old (see the DATA AGE column of `delegate quota`); use `--source direct` when a pool is close to its limit.
2. **Brief.** Write it to a file in the scratchpad. Cover the goal; which files the delegate owns and which it must not touch; the exact verify commands, including a dry run on real data; and what the output should look like. Give the reason behind each constraint. Codex follows constraints literally: once it dropped a Wikipedia-list year fallback because the brief said to use only one source. The launcher appends a contract (no commits, stay in the worktree, flag conflicts with real data, fixed final-message format), so the brief does not need to repeat it.
3. **Launch** with the Bash tool and `run_in_background: true`; you are notified when it exits:
   ```
   delegate launch codex  --brief B.md --name feat-x --link data/cache            # gpt-6.1-sol @xhigh by default
   delegate launch cursor --brief B.md --name feat-y --model composer-2.5           # Auto+Composer pool
   delegate launch codex  --brief B.md --read-only --name review-z                # review/research
   ```
   `--link PATH` symlinks a gitignored path (e.g. a data cache) from the main checkout into the worktree and excludes it in `.git/info/exclude`. Parallel runs are fine if they own disjoint files. `--dry-run` prints run.sh and the full prompt without creating anything.
4. **Keep working** while it runs; `delegate status NAME` shows progress.
5. **Review. Do not trust the summary.** Read `last-message.md`, then `delegate diff NAME` and read the whole `changes.patch`. Re-run the delegate's tests and dry run yourself in the worktree and compare the output with the real data. `diff` warns if the delegate committed, which breaches the contract.
6. **Merge.** Run `delegate apply NAME --check`, then `delegate apply NAME`. This applies the patch, uncommitted, to the main working tree. Fix up by hand if needed, and commit only when the user asks. Use `--3way` if the main tree has moved on.
7. **Clean up.** `delegate cleanup NAME` removes the worktree and branch and keeps the run record. It refuses while the worktree holds changes that were not applied, or that changed after `apply`. `--purge` deletes the record too; `--force` discards unapplied changes.

Delegates' summaries can be wrong even when their code is right. In the first trial, Composer's summary misquoted the brief. Judge the patch and your own re-run, not the prose.

## Run directory

`~/.cache/claude-delegate/runs/<repo>/<name>/` (override with `--root` or `$CLAUDE_DELEGATE_ROOT`):
`wt/` worktree · `brief.md` · `prompt.md` (brief + contract, exactly what the agent got) · `run.sh` (exact command, re-runnable) · `meta.json` · `log` (Cursor writes stream-json; `status` renders it) · `last-message.md` · `exit_code` · `changes.patch` (after `diff`) · `applied` (after `apply`).

## Agents

- **Codex** (`codex exec`): `-s workspace-write` (or `read-only`), `-C <worktree>`, `-o last-message.md`, prompt on stdin. Models: `gpt-6.1-sol` (proven in practice), `gpt-6-astra` (scores higher on AA but costs about 5x per token, so it drains the Codex pool faster), `gpt-5.6-terra` (fast and mechanical). Effort ranges `low` to `xhigh`, then `max`.
- **Cursor** (`cursor-agent -p`, installed under `~/.local/share/cursor-agent`, symlinked to `~/.local/bin/agent` and `cursor-agent`; update with `cursor-agent update`): runs with `--sandbox enabled --trust --workspace <worktree> --output-format stream-json`, or `--mode ask` with `--read-only`. In headless mode, sandboxed shell commands auto-run with no approval, and the agent can write only inside the worktree: outside writes fail with EPERM, the edit tool rejects them, and there is no network. **Never add `--force`.** It silently bypasses the sandbox (tested 2026-10-09). `--unsandboxed` exists for tasks that need network (installs); ask the user first. `last-message.md` holds the assistant text after the last tool call. Model ids (`cursor-agent models`): `auto`, `composer-2.5[-fast]`, `grok-4.7-{low,medium,high,xhigh}[-fast]`, `cursor-grok-4.5/4.6-*`, plus GPT, Claude and Gemini variants. `auto` and `composer-*` draw on the Auto+Composer pool. Other named models are assumed to bill to the premium pool; this is not verified per model, and the `cursor-grok-*` variants in particular may differ. If `cursor-agent status` says "Not logged in", the user must run `cursor-agent login` and approve in the browser.
- **Claude subagents** (Agent tool, `model: sonnet|haiku`) appear in `route` for comparison. They spend the orchestrator's own Claude pool, so they carry a 0.8 penalty.

## Gotchas

- The launcher strips Cursor AppImage variables (`LD_LIBRARY_PATH`, `/tmp/.mount_*` PATH entries), `VSCODE_*`, `CURSOR_*`, `CLAUDE_CODE_*` (including the messaging token), and virtualenvs that belong to another repo from the delegate's environment.
- Python projects with an editable install: inside the worktree, `import pkg` can resolve to the main checkout. Check with `python -c "import pkg; print(pkg.__file__)"` or run with `PYTHONPATH=.`.
- A gitignore rule `dir/` does not match a symlink; that is why `--link` writes the exact path to `.git/info/exclude`.

## Tuning

`scripts/catalog.json` holds the delegates (agent, model, effort, AA slug, pools, notes), hand-set `prior` numbers for models AA does not rate (Cursor Auto and Composer; they are guesses), task weights, `min_quota_pct` (default 10, below which a pool is excluded) and the subagent penalty. Find AA slugs with `delegate ratings --grep 'gpt-6|grok'`. Without `ARTIFICIAL_ANALYSIS_API_KEY` the ratings come from the public leaderboard page, where "coding" is a proxy (mean of Terminal-Bench 4.0 and SciCode). A free API key gives AA's official Coding Index. The cache lives at `~/.cache/claude-delegate/aa-models.json` for 24h.
