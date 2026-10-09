# claude-orchestrator

Claude Code as orchestrator, other agents as workers. This repo holds the user-level skills and scripts. A symlink from `~/.claude/skills/` makes them available in every project on a machine.

## Skills

| skill | what it does |
|---|---|
| [`delegate`](skills/delegate/SKILL.md) | Hands work to **Codex CLI** or **Cursor's headless agent** (`cursor-agent --model auto`). Shows live quota per pool via [openusage](https://github.com/janekbaraniewski/openusage) and ranks models per task type with [Artificial Analysis](https://artificialanalysis.ai/) ratings. Each delegate runs in its own git worktree with a brief, a log, a last-message file and an optional check command (`--verify`). The other vendor reviews the result read-only (`review`), and fix rounds resume the same agent session (`followup`). Claude reviews the diff and applies it. |

CLI entry point: `skills/delegate/scripts/delegate` (`quota`, `ratings`, `route`, `launch`, `review`, `followup`, `verify`, `status`, `diff`, `apply`, `cleanup`). It is stdlib-only Python 3.9+. Tunables (models, pools, task weights) are in [`skills/delegate/scripts/catalog.json`](skills/delegate/scripts/catalog.json).

## New machine

```bash
gh repo clone egorpol/claude-orchestrator   # private; or use the Nextcloud-synced copy
cd claude-orchestrator
./install.sh --check        # what is missing
./install.sh                # symlink skills into ~/.claude/skills (add --bin for `delegate` on PATH)
```

`install.sh` is idempotent. It never deletes an existing skill directory: with `--force` the old one moves to `~/.claude/skill-backups/`. Use `--copy` where a symlink into the repo is not wanted.

Prerequisites (`--check` prints install hints):

- `codex`, logged in with `codex login`
- `cursor-agent`: `curl https://cursor.com/install -fsS | bash`, then `cursor-agent login`. The login needs a browser approval.
- `openusage` for quota data
- optional: the [Codex plugin for Claude Code](https://github.com/openai/codex-plugin-cc) (needs Node 18.18+). Install it with `claude plugin marketplace add openai/codex-plugin-cc && claude plugin install codex@openai-codex`; it adds `/codex:review` and `/codex:adversarial-review` for the current checkout
- optional `ARTIFICIAL_ANALYSIS_API_KEY` (free) for AA's official coding index; without it, ratings are parsed from the public leaderboard page

## Verified behaviour (2026-10-09)

- **`cursor-agent --force` silently disables `--sandbox enabled`.** Without `--force`, headless sandboxed commands auto-run, writes outside the worktree fail with EPERM, the edit tool rejects outside paths, and there is no network. The launcher never passes `--force` unless you give it `--unsandboxed`.
- Claude Code running inside Cursor inherits Cursor's AppImage environment (`LD_LIBRARY_PATH=/tmp/.mount_*`). The launcher scrubs that, `VSCODE_*`, `CURSOR_*` and `CLAUDE_CODE_*` (including the messaging token), plus virtualenvs belonging to other repos, before starting a delegate.
- Claude Code discovers symlinked skill directories, which is what `install.sh` relies on.
- Cursor `--mode ask` cannot run shell commands (they come back `permissionDenied`); it can read files. That is why reviews get the diff as a file at `wt/.delegate/review-N.patch`.
- Session resume works headless for both agents. `cursor-agent -p --resume <session_id>` works, and so does `codex exec resume <id>`; the latter has no `-s`/`-C`, so the launcher passes `-c sandbox_mode=…` and runs from the worktree.
- Trial 2: Cursor `auto` implemented `--top N` (25s, checks passed). A Codex review found a real bug (`--top -1` silently dropped a row via negative slicing), the same one I had found independently. `followup --from-review 1` resumed the Cursor session and fixed it (20s, checks passed), and a Cursor re-review approved it.
- End-to-end trial: Codex gpt-6.1-sol @xhigh and Cursor composer-2.5 ran the same toy brief in parallel worktrees. Both handled malformed real data and neither committed. Composer's summary misquoted the brief, so review the patch rather than the prose.

## Related projects

See [docs/related-projects.md](docs/related-projects.md) for what else exists (OpenAI's Codex plugin for Claude Code, obra/superpowers, multi-agent orchestrators) and what is worth borrowing.

## Layout

```text
install.sh                      link/copy skills, check prerequisites
skills/delegate/SKILL.md        instructions Claude loads
skills/delegate/scripts/        delegate CLI + catalog.json
docs/related-projects.md        survey of similar tools
```

Run records live outside the repo, in `~/.cache/claude-delegate/runs/<repo>/<name>/`.
