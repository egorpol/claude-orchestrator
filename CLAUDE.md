# claude-orchestrator

User-level Claude Code skills for running Claude as orchestrator with Codex CLI and Cursor's headless agent as workers. The only skill so far is `delegate`; README.md covers what it does and how to install it, and `skills/delegate/SKILL.md` is the text other sessions load.

**This checkout is live.** `~/.claude/skills/delegate` is a symlink into `skills/delegate/`, so every edit here immediately changes how delegation behaves in every project on this machine. Don't leave the scripts broken between edits.

## Layout

- `skills/delegate/scripts/delegate`: argparse entry point (no `.py` extension), dispatching to:
  - `quota.py`: pools from `openusage export`
  - `ratings.py`: Artificial Analysis via API key or leaderboard scrape, cached 24h
  - `route.py`: per-task score = ratings × weights × quota
  - `runs.py`: worktree/launch/followup/review/verify/status/wait/diff/apply/cleanup, `meta.json`, environment scrubbing
- `skills/delegate/scripts/catalog.json`: user-editable delegates, pools, task weights, `min_quota_pct`, subagent penalty.
- `install.sh`: links or copies skills into `~/.claude/skills`; `--check` checks prerequisites; `--hooks` registers `hooks/context-guard.py`; `--rules` imports `rules/orchestrator.md` into the user-level `CLAUDE.md`.
- `hooks/context-guard.py`: a UserPromptSubmit hook. From about 120k context tokens, it asks Claude to hand off before starting a new work item. After 60 min idle at 100k+ context, the prompt cache has expired, so it holds the first typed prompt once and suggests `/clear`. Tested in `tests/test_context_guard.py`.
- `rules/orchestrator.md`: short rules for every session. SKILL.md only reaches sessions that invoke the skill, and the sessions that sprawl often don't.
- `docs/related-projects.md`: survey of similar tools and ideas worth borrowing.
- `feedback/`: field reports from real use (Markdown plus `runs.jsonl`). Read them before changing the workflow.

## Checking a change

```bash
mkdir -p .delegate/tmp && TMPDIR=$PWD/.delegate/tmp python3 -m unittest discover -s tests   # fake agents, no quota
bash -n install.sh
```

- **What the tests cover:**
  - `tests/test_lifecycle.py` drives launch, the exit packet, followup, review, status, diff, apply, the run log and cleanup, using the fake `codex`/`cursor-agent` in `tests/fake_agents/`.
  - `test_route.py`, `test_cost.py` and `test_ask.py` cover routing preferences, cost accounting and `ask`.
- **Real-data checks** (read-only):
  - `delegate status <run>` against the old records in `~/.cache/claude-delegate/runs/PublicDomainSheetMusicFinder/`;
  - `delegate cost --session 762dc86f` must print 389 calls and cache_read 174,522,404. The session was resumed for 3 calls after the 2026-10-09 analysis, so older docs quote 386.
- **Real agent runs spend quota.** Use a throwaway repo in the scratchpad.
- **Installer:** test it against `CLAUDE_CONFIG_DIR=<scratch>`, never the real `~/.claude`.
- **Docs:** after a behaviour change, update SKILL.md, plus README's "Verified behaviour" with the date when the fact was tested live.

## Rules that must not regress

- **Never pass `--force` to `cursor-agent`.** It silently disables `--sandbox enabled` (tested 2026-10-09). Only `--unsandboxed` adds it, and that flag needs the user's OK.
- **Cursor defaults to `--model auto`**, which is the user's preference: Auto pool, SpaceX in-house models. Named Grok and Composer bill "Cursor Models", which shares the Auto pool; other named models bill premium.
- **Delegates never commit, and nothing auto-commits.** `apply` puts the patch into the main tree uncommitted. `cleanup` refuses to drop unapplied changes without `--force`.
- **Reviews are read-only and use the other vendor by default.** Cursor `--mode ask` can't run shell, so the diff goes in as `wt/.delegate/review-N.patch`.
- **Delegates get a scrubbed environment:** no Cursor AppImage `LD_LIBRARY_PATH` or `/tmp/.mount_*` PATH entries, no `VSCODE_*`/`CURSOR_*`/`CLAUDE_CODE_*`, no foreign venvs. Claude Code here runs inside Cursor and inherits all of these.
- **Stdlib-only Python 3.9+**, so a fresh machine needs nothing beyond `install.sh`.

## Environment notes

- **Shell:** the Bash tool runs zsh, although the user's terminal is fish. In zsh, `$args` doesn't word-split and a bare `====` is expanded as `=cmd`.
- **Nextcloud:** the repo lives in Nextcloud, so `.git` syncs too. On each other machine, use either the synced folder or a `gh repo clone`, never both.
- **Git:** remote `egorpol/claude-orchestrator` is private. Commit messages are imperative, e.g. "Add review, followup and verify rounds; …". Commit and push only when asked.

## Open items

[docs/optimization-plan.md](docs/optimization-plan.md) is the current plan. It ranks levers by measured cost impact from the 2026-10-09 transcript analysis and splits them into jobs A–D with disjoint file ownership. The biggest lever, about 60%, is one work item per orchestrator session. It is workflow guidance, not CLI code. Mark items done there as they land.
