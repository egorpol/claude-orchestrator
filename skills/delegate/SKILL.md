---
name: delegate
description: Delegate coding, review, research or bulk-labelling work from Claude Code to Codex CLI or Cursor's headless agent (cursor-agent, model auto), weighing live quota (openusage) and Artificial Analysis ratings, running each delegate in its own git worktree with automatic checks, cross-vendor review and follow-up rounds, and reviewing its diff before merging. Use when the user asks to offload or parallelise work to Codex, Cursor or "another agent", asks how much Claude/Codex/Cursor quota is left, or asks which model to use for a task.
---

# Delegate

One CLI: `~/.claude/skills/delegate/scripts/delegate` (stdlib Python; call it by full path). Source of truth is the private repo `egorpol/claude-orchestrator`; `~/.claude/skills/delegate` is a symlink into it, so edits made here change the repo. Commit them there.

| command | does |
|---|---|
| `delegate quota [--source direct]` | % left, reset time and pace for claude-5h/7d, codex-5h/7d, cursor-auto, cursor-premium |
| `delegate route <task>` | ranks delegates for `review`, `refactor`, `implement`, `research`, `bulk-label` and prints the launch command for the pick |
| `delegate ratings [--grep RE] [--refresh]` | Artificial Analysis intelligence / coding / tok/s / $ for catalog models (or any AA model) |
| `delegate launch codex\|cursor --brief F [--verify CMD] ...` | worktree on branch `delegate/<name>`, brief + contract, background run, log, last message, checks |
| `delegate review NAME [--agent ...] [--focus TEXT]` | read-only review of the run's changes; by default the *other* vendor reviews |
| `delegate followup NAME --brief F \| --from-review N [--fresh --model M]` | next round in the same worktree, resuming the agent's session |
| `delegate verify NAME [--cmd C]` | re-run the check command in the worktree |
| `delegate status [NAME]` | all runs, or one run's state, rounds, checks, reviews, log tail and last message |
| `delegate diff NAME` / `apply NAME [--check]` / `cleanup NAME` | inspect, merge, tidy up |

## Workflow

1. **Quota and pick.** Run `delegate route <task>`, which includes quota. Tell the user in one line which delegate and why. openusage daemon data can be about an hour old (see the DATA AGE column of `delegate quota`); use `--source direct` when a pool is close to its limit.
2. **Brief.** Write it to a file in the scratchpad. Cover the goal; which files the delegate owns and which it must not touch; the exact verify commands, including a dry run on real data; and what the output should look like. Give the reason behind each constraint: Codex follows constraints literally, and once dropped a Wikipedia-list year fallback because a brief said to use only one source. Keep one job per run; review, fix and docs are separate runs. GPT models respond well to tagged sections such as `<task>`, `<structured_output_contract>`, `<default_follow_through_policy>` ("keep going until…") and `<verification_loop>` (from OpenAI's Codex prompting guide). The launcher appends a contract (no commits, stay in the worktree, flag conflicts with real data, fixed final-message format), so the brief does not need to repeat it.
3. **Launch** with the Bash tool and `run_in_background: true`; you are notified when it exits:
   ```
   delegate launch codex  --brief B.md --name feat-x --link data/cache --verify 'pytest -q'   # gpt-6.1-sol @xhigh
   delegate launch cursor --brief B.md --name feat-y --verify 'pytest -q'                     # model auto
   delegate launch codex  --brief B.md --read-only --name research-z                          # review/research
   ```
   - `--verify CMD` runs after the agent exits, in the worktree with the cleaned environment. The result goes to `verify.log` / `verify_code`, and `status` shows "verify pass/FAIL". Pass the real test command, not something piped through `tail`, which masks the exit code.
   - `--link PATH` symlinks a gitignored path (e.g. a data cache) from the main checkout into the worktree and excludes it in `.git/info/exclude`.
   - Parallel runs are fine if they own disjoint files.
   - `--dry-run` prints run.sh and the full prompt without creating anything.
4. **Keep working** while it runs; `delegate status NAME` shows progress.
5. **Cross-review** (for non-trivial changes): `delegate review NAME` in the background. Codex reviews Cursor's work and Cursor reviews Codex's (`--agent` overrides; `--focus` steers). The reviewer gets the original brief plus the diff (written to `wt/.delegate/review-N.patch`), and returns a Verdict (APPROVE / CHANGES REQUESTED), findings with severity, and per-requirement spec compliance. `status` warns if a reviewer changed the worktree.
6. **Your own review. Do not trust any summary**, the writer's or the reviewer's. Read `last-message.md`, then `delegate diff NAME` and the whole `changes.patch`. Re-run the tests and the dry run yourself and compare against the real data. Adjudicate each reviewer finding: some are wrong.
7. **Fix rounds.** Write a brief with the findings you accept (or use `--from-review N` after checking them), then run `delegate followup NAME --brief fixes.md`. This archives the round to `round-<k>/` and resumes the same agent session in the same worktree, with the same `--verify`. After about 3 rounds without convergence, use `--fresh --model <stronger>`: a new session that builds on the current worktree.
8. **Merge.** Run `delegate apply NAME --check`, then `delegate apply NAME`. This applies the patch, uncommitted, to the main working tree. Commit only when the user asks. Use `--3way` if the main tree has moved on.
9. **Clean up.** `delegate cleanup NAME` removes the worktree and branch and keeps the run record. It refuses while there are unapplied changes. `--purge` deletes the record too; `--force` discards changes.

Delegates' summaries can be wrong even when their code is right; in the first trial, Composer misquoted the brief. Judge the patch and your own re-run.

## Run directory

`~/.cache/claude-delegate/runs/<repo>/<name>/` (override with `--root` or `$CLAUDE_DELEGATE_ROOT`):
- current round: `brief.md` · `prompt.md` (exactly what the agent got) · `run.sh` (re-runnable) · `log` (Cursor writes stream-json; `status` renders it) · `last-message.md` · `exit_code` · `verify.log` / `verify_code`
- `round-<k>/` holds earlier rounds, and `review-<n>/` holds reviews (same files plus `pre_digest` / `post_digest`).
- `meta.json` (agent, model, base, rounds, history), `wt/` worktree, `changes.patch` (after `diff`), `applied` (after `apply`).

## Agents

- **Codex** (`codex exec`): `-s workspace-write` or `read-only`, `-C <worktree>`, `-o last-message.md`, prompt on stdin. Follow-ups use `codex exec resume <session-id>`; the session id is read from the log, and the sandbox is passed via `-c sandbox_mode=…`. Models: `gpt-6.1-sol` (proven), `gpt-6-astra` (scores higher on AA but costs about 5x per token, so it drains the pool faster), `gpt-5.6-terra` (fast, mechanical). Effort ranges `low` to `xhigh`, then `max`.
- **Cursor** (`cursor-agent -p`, in `~/.local/bin`; `cursor-agent update` updates it): **use `--model auto`**, the default. Auto routes to SpaceX in-house models (Cursor is SpaceX-owned), draws on the Auto pool, and is the most cost-efficient choice. Named models (`composer-2.5`, `grok-4.7-*`, GPT/Claude/Gemini) bill to the premium pool; use them only if the user asks.
  - Runs with `--sandbox enabled --trust --workspace <worktree> --output-format stream-json`. Sandboxed shell commands auto-run headless; writes outside the worktree fail with EPERM, the edit tool rejects outside paths, and there is no network.
  - **Never add `--force`**: it silently bypasses the sandbox (tested 2026-10-09). `--unsandboxed` (network, installs) needs the user's OK.
  - `--mode ask` (used by `--read-only` and Cursor reviews) **cannot run shell commands**; it reads files only.
  - Follow-ups use `--resume <session_id>` from the stream's init event.
  - If `cursor-agent status` says "Not logged in", the user must run `cursor-agent login` and approve in the browser.
- **Claude subagents** (Agent tool, `model: sonnet|haiku`) appear in `route` for comparison. They spend the orchestrator's own Claude pool, so they carry a 0.8 penalty.
- **Codex plugin** (`openai/codex-plugin-cc`, if installed): `/codex:review` and `/codex:adversarial-review` review the *current checkout* in place. It is a good fit for reviewing your own uncommitted work. Use `delegate` for anything that writes, since the plugin's `/codex:rescue` has no worktree isolation.

## Gotchas

- The launcher strips Cursor AppImage variables (`LD_LIBRARY_PATH`, `/tmp/.mount_*` PATH entries), `VSCODE_*`, `CURSOR_*`, `CLAUDE_CODE_*` (including the messaging token), and virtualenvs that belong to another repo.
- Python projects with an editable install: inside the worktree, `import pkg` can resolve to the main checkout. Check with `python -c "import pkg; print(pkg.__file__)"` or use `PYTHONPATH=.`.
- A gitignore rule `dir/` does not match a symlink, which is why `--link` writes exact paths (and `/.delegate`) to `.git/info/exclude`.

## Tuning

`scripts/catalog.json` holds:
- the delegates: agent, model, effort, AA slug, `aa_proxy` when the slug is a stand-in model, pools and notes;
- task weights;
- `min_quota_pct` (default 10), below which a pool is excluded;
- the subagent penalty.

Find AA slugs with `delegate ratings --grep 'gpt-6|grok'`. Without `ARTIFICIAL_ANALYSIS_API_KEY` the ratings come from the public leaderboard page, where "coding" is a proxy (mean of Terminal-Bench 4.0 and SciCode). The cache lives at `~/.cache/claude-delegate/aa-models.json` for 24h.
