# claude-orchestrator

User-level Claude Code skills for running Claude as orchestrator with Codex CLI and Cursor's headless agent as workers. Two skills: `delegate`, and `field-report`, a thin wrapper around `delegate report`. README.md covers why the repo exists, what it achieved and how to install it. `skills/*/SKILL.md` is the text other sessions load. Versions are in CHANGELOG.md (SemVer 0.x, first tag v0.1.0).

**This checkout is live.** `~/.claude/skills/delegate` is a symlink into `skills/delegate/`, so every edit here immediately changes how delegation behaves in every project on this machine. Don't leave the scripts broken between edits.

## Layout

- `skills/delegate/scripts/delegate`: argparse entry point (no `.py` extension), dispatching to:
  - `quota.py`: pools from `openusage export`
  - `ratings.py`: Artificial Analysis via API key or leaderboard scrape, cached 24h
  - `route.py`: per-task score = ratings × weights × quota
  - `runs.py`: worktree/launch/followup/review/verify/status/wait/diff/apply/cleanup, `meta.json`, environment scrubbing
  - `cost.py`, `ask.py`, `report.py`: session and run-log cost, read-only questions, field-report skeletons and JSONL rows
- `skills/delegate/scripts/catalog.json`: user-editable delegates, pools, task weights, `min_quota_pct`, subagent penalty.
- `install.sh`: links or copies skills into `~/.claude/skills`; `--check` checks prerequisites; `--hooks` registers `hooks/context-guard.py`; `--rules` imports `rules/orchestrator.md` into the user-level `CLAUDE.md`; `--timer` installs the systemd user timer for `delegate quota --snapshot --if-active` (test with a scratch `XDG_CONFIG_HOME` and a fake `systemctl`).
- `hooks/context-guard.py`: a UserPromptSubmit hook. From about 120k context tokens, it asks Claude to hand off before starting a new work item. After 60 min idle at 100k+ context, the prompt cache has expired, so it holds the first typed prompt once and suggests `/clear`. Tested in `tests/test_context_guard.py`.
- `rules/hygiene.md` (every session) and `rules/delegation.md` (when to delegate): short rules; `rules/orchestrator.md` imports both. SKILL.md only reaches sessions that invoke the skill, and the sessions that sprawl often don't.
- `skills/field-report/SKILL.md`: `/field-report [topic]` from any project; calls `delegate report`, then Claude writes the narrative.
- `docs/`:
  - `cost-analysis.md`: the frozen 2026-10-09 baseline and the eight levers;
  - `field-results.md`: append-only per-session measurements, the public evidence for the README's numbers;
  - `verified-behaviour.md`: dated facts tested live;
  - `related-projects.md`: survey of similar tools.
- `feedback/`: field reports from real use (Markdown plus `token_usage.jsonl` and `runs.jsonl`). **Gitignored since 0.1.0**, so it exists only in this checkout (Nextcloud syncs it). Read it before changing the workflow. `HANDOFF.md` is gitignored too.

## Checking a change

```bash
mkdir -p .delegate/tmp && TMPDIR=$PWD/.delegate/tmp python3 -m unittest discover -s tests   # fake agents, no quota
bash -n install.sh
```

- **What the tests cover:**
  - `tests/test_lifecycle.py` drives launch, the exit packet, followup, review, status, diff, apply, the run log and cleanup, using the fake `codex`/`cursor-agent` in `tests/fake_agents/`.
  - `test_route.py`, `test_cost.py` and `test_ask.py` cover routing preferences, cost accounting and `ask`.
- **Real-data checks** (read-only, maintainer's machine only):
  - `delegate status <run>` against the old records in `~/.cache/claude-delegate/runs/PublicDomainSheetMusicFinder/`;
  - `delegate cost --session 762dc86f` must print 389 calls and cache_read 174,522,404. The session was resumed for 3 calls after the 2026-10-09 analysis, so older docs quote 386.
- **Real agent runs spend quota.** Use a throwaway repo in the scratchpad.
- **Installer:** test it against `CLAUDE_CONFIG_DIR=<scratch>`, never the real `~/.claude`.
- **Docs:** after a behaviour change, update SKILL.md, plus `docs/verified-behaviour.md` with the date when the fact was tested live, plus a line under `[Unreleased]` in CHANGELOG.md.

## Rules that must not regress

- **Never pass `--force` to `cursor-agent`.** It silently disables `--sandbox enabled` (tested 2026-10-09). Only `--unsandboxed` adds it, and that flag needs the user's OK.
- **Cursor defaults to `--model auto`**, which is the user's preference: Auto pool, SpaceX in-house models. Named Grok and Composer bill "Cursor Models", which shares the Auto pool; other named models bill premium.
- **Delegates never commit, and nothing auto-commits.** `apply` puts the patch into the main tree uncommitted. `cleanup` refuses to drop unapplied changes without `--force`.
- **Reviews are read-only and use the other vendor by default.** Cursor `--mode ask` can't run shell, so the diff goes in as `wt/.delegate/review-N.patch`.
- **Delegates get a scrubbed environment:** no Cursor AppImage `LD_LIBRARY_PATH` or `/tmp/.mount_*` PATH entries, no `VSCODE_*`/`CURSOR_*`/`CLAUDE_CODE_*`, no foreign venvs. Claude Code here runs inside Cursor and inherits all of these.
- **Stdlib-only Python 3.9+**, so a fresh machine needs nothing beyond `install.sh`.

## Environment notes

- **Shell:** the Bash tool runs zsh, although the user's terminal is fish. In zsh, `$args` doesn't word-split and a bare `====` is expanded as `=cmd`.
- **Nextcloud:** the maintainer's checkout lives in Nextcloud, so `.git` syncs too. On each other machine, use either the synced folder or a `gh repo clone`, never both.
- **Git:** remote `egorpol/claude-orchestrator` is public since 2026-10-13, with its full history. Commit messages are imperative, e.g. "Add review, followup and verify rounds; …". Commit and push only when asked.

## Field reports, releases, open items

- **Folding in field reports:** list `feedback/*.md` newer than the last "fold in" commit. For each lesson:
  - a rule for every session goes into `rules/hygiene.md`, or into `rules/delegation.md` if it is about delegating;
  - delegate workflow goes into SKILL.md or `templates/`;
  - a CLI or hook bug becomes a fix plus a test.

  Append a dated section with the sessions' numbers to `docs/field-results.md`, with `delegate cost --session <id>` as the source, and refresh the overview table there. If a headline number in README changes, re-check it against that table. Reports name other projects' private details; carry lessons over, not quotes.
- **Releases:** move `[Unreleased]` to `## [X.Y.Z] - date` and update the compare links. The tag (`git tag -a vX.Y.Z`) and the push are outward steps, so bundle them into the end-of-item approval.
- **Open items:** README's Roadmap lists them, and `HANDOFF.md` (local) has the next step.
