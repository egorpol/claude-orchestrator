# claude-orchestrator

Claude Code as orchestrator, Codex CLI and Cursor's headless agent as workers. This repo holds user-level Claude Code skills, a hook and session rules. A symlink from `~/.claude/skills/` makes them available in every project on a machine.

## Why this exists

This is my own workflow, tuned to my setup. Cursor is my IDE, Claude Code runs inside it as the main agent, and Codex CLI and Cursor's agent run on their own subscriptions. Claude's quota runs out first. So Claude orchestrates: it keeps design, data checks and review, and hands mechanical work to whichever pool has quota to spare. The rest of the repo cuts what Claude's own sessions cost, mostly by keeping them short.

It is a snapshot, published in case parts of it are useful to someone else. It is not a general-purpose tool. It assumes Linux, [openusage](https://github.com/janekbaraniewski/openusage) for quota data, and Codex and Cursor subscriptions, and it changes whenever my workflow does. Versions are in the [CHANGELOG](CHANGELOG.md).

## What it achieved so far

These figures come from 16 Claude Code sessions across five projects, 2026-10-09 to 2026-10-13; the per-session numbers are in [field results](docs/field-results.md). Cost units weigh tokens by API price: input 1, cache read 0.1, cache write 2, output 5. Each session did different work, so the comparisons are per call, not controlled experiments.

- **Short sessions with handoffs.** The baseline was one 13-hour session: 389 calls, with context growing to 779k tokens. Each call re-reads everything before it, so cost grows with the square of session length; cache re-reads were 98% of its tokens. It averaged 67.4k cost units per call. The 15 sessions since averaged 24.8k (63% less), and those that did one item from a `HANDOFF.md` ran 17.7k–21.7k. The rules and the hook push for one work item per session.
- **Idle cache rebuilds.** After an hour idle, the prompt cache expires, and the next turn rewrites the whole context at twice the input price. That happened in 7 of the 16 sessions and cost 12% of all logged units (7.2M of 59.6M). The hook now holds the first typed prompt after an hour idle and suggests `/clear`; this has not yet been seen in a live session.
- **No polling.** In the baseline, 40 status checks cost 15.3% of the session. A finished delegate run now puts its status, at most 20 lines, into the task notification.
- **Delegation.**
  - On the baseline day, Cursor Auto used about 5× fewer tokens per run than Codex at xhigh effort, and moved its quota pool about 1 point over 9 runs.
  - Codex later built an 887-line module with 48 tests in 16 minutes, with no fix round.
  - Cursor Auto built a 15-file feature in 2 rounds with 0 review findings.
  - A bulk pass labelled 6,529 rows with 0 failures for about 2.7 points of the Cursor Auto pool.
- **Cross-vendor review.** On the module run, Cursor's review and Claude's own read each found a bug the other missed. Reviews also approved a wrong brief twice, so reviewers are now asked to challenge the brief.
- **Still short of the goal.** Claude's 7-day pool fell about 3 points per session even in the best sessions, and several design- or data-heavy sessions delegated nothing.

The analysis behind these levers is in [docs/cost-analysis.md](docs/cost-analysis.md).

## What's in it

| part | what it does |
|---|---|
| [`delegate`](skills/delegate/SKILL.md) skill and CLI | Hands work to **Codex CLI** or **Cursor's headless agent** (`cursor-agent --model auto`). Shows live quota per pool via openusage and ranks models per task type with [Artificial Analysis](https://artificialanalysis.ai/) ratings. Each delegate runs in its own git worktree with a brief, a log and an optional check command. The other vendor reviews the result read-only, and fix rounds resume the same agent session. Claude reviews the diff and applies it uncommitted. |
| [`/field-report`](skills/field-report/SKILL.md) skill | Writes a field report on the current session from any project's chat: `delegate report` fills in the numbers, then Claude writes the narrative. |
| [`hooks/context-guard.py`](hooks/context-guard.py) | Asks for a handoff before a new work item once context passes about 120k tokens. Holds the first prompt after an hour idle. |
| [`rules/orchestrator.md`](rules/orchestrator.md) | Short rules for every session, including sessions that never load a skill. |

The CLI entry point is `skills/delegate/scripts/delegate`, with the subcommands `quota`, `ratings`, `route`, `launch`, `review`, `followup`, `verify`, `status`, `wait`, `diff`, `apply`, `cleanup`, `ask`, `cost` and `report`. It is stdlib-only Python 3.9+. Tunables (models, pools, task weights, `prefer`) are in [`catalog.json`](skills/delegate/scripts/catalog.json), and brief templates are in [`templates/`](skills/delegate/templates/).

**How this differs.** The [full orchestrators in the survey](docs/related-projects.md#full-orchestrators-alternatives-heavier) are supervisors a person watches: tmux panes, boards or a desktop app over many agents. Here, Claude Code stays the orchestrator inside one session. The official Codex plugin also delegates from Claude Code, but it works in the current checkout and only with Codex.

## Install

```bash
gh repo clone egorpol/claude-orchestrator
cd claude-orchestrator
./install.sh --check        # what is missing
./install.sh                # symlink skills into ~/.claude/skills (add --bin for `delegate` on PATH)
./install.sh --hooks        # optional: register hooks/context-guard.py
./install.sh --rules        # optional: import rules/orchestrator.md into ~/.claude/CLAUDE.md (every session)
```

`install.sh` is idempotent. It never deletes an existing skill directory: with `--force`, the old one moves to `~/.claude/skill-backups/`. Use `--copy` where a symlink into the repo is not wanted. A copied install doesn't know where the checkout is, so `/field-report` then needs `CLAUDE_ORCHESTRATOR_FEEDBACK` set to a folder.

Prerequisites (`--check` prints install hints):

- `codex`, logged in with `codex login`
- `cursor-agent`: `curl https://cursor.com/install -fsS | bash`, then `cursor-agent login`. The login needs a browser approval.
- `openusage` for quota data
- optional: the [Codex plugin for Claude Code](https://github.com/openai/codex-plugin-cc) (needs Node 18.18+), which adds `/codex:review` and `/codex:adversarial-review` for the current checkout. Install it with `claude plugin marketplace add openai/codex-plugin-cc && claude plugin install codex@openai-codex`.
- optional: `ARTIFICIAL_ANALYSIS_API_KEY` (free) for AA's official coding index. Without it, ratings are parsed from the public leaderboard page.

## Field reports

Most of the rules here started as a lesson from a real session. To log one, type `/field-report [topic]` at the end of a session in any project. It writes `feedback/<date>-<project>-<topic>.md` in this checkout and adds the session's rows to `feedback/token_usage.jsonl` and `feedback/runs.jsonl`. `feedback/` is gitignored, so reports stay local. To act on new reports, open a session in this repo and ask it to fold them in. The lessons then go into the rules, `SKILL.md`, [docs/field-results.md](docs/field-results.md) and the CHANGELOG.

## Tests

```bash
mkdir -p .delegate/tmp && TMPDIR=$PWD/.delegate/tmp python3 -m unittest discover -s tests
```

The tests use fake `codex` and `cursor-agent` scripts, so they spend no quota.

## Roadmap

- A pre-push guard that blocks secrets (API tokens, private keys, home paths) in outgoing commits, which `install.sh --hooks` can offer.
- Personal tuning in an optional `catalog.local.json`, merged key by key over `catalog.json`.
- Machine-readable reviews through `codex exec --output-schema`.
- The open ideas in the [survey](docs/related-projects.md): loop cost scoring, pre-launch approval policies, `.worktreeinclude`.

## Docs

- [CHANGELOG.md](CHANGELOG.md): versions.
- [docs/cost-analysis.md](docs/cost-analysis.md): the 2026-10-09 baseline and the eight cost levers.
- [docs/field-results.md](docs/field-results.md): per-session measurements since then.
- [docs/verified-behaviour.md](docs/verified-behaviour.md): dated facts about Codex, Cursor and Claude Code, tested live; for example, `cursor-agent --force` silently disables the sandbox.
- [docs/related-projects.md](docs/related-projects.md): similar tools, and what is worth borrowing from them.

## Layout

```text
install.sh                         link/copy skills, check prerequisites; --hooks, --rules, --bin
hooks/context-guard.py             UserPromptSubmit: handoff nudge past ~120k; holds a prompt once after 1h idle
rules/orchestrator.md              user-level rules for every session (install.sh --rules)
skills/delegate/SKILL.md           instructions Claude loads
skills/delegate/templates/         brief templates (implement, mechanical, review, research)
skills/delegate/scripts/           delegate CLI + catalog.json
skills/field-report/SKILL.md       /field-report: write a field report from any project
tests/                             unittest suite with fake agents
docs/                              cost analysis, field results, verified behaviour, survey
feedback/                          field reports and usage JSONL (gitignored, local only)
```

Run records live outside the repo, in `~/.cache/claude-delegate/runs/<repo>/<name>/`. The run log is `~/.cache/claude-delegate/runs/runs.jsonl`.

## License

MIT, see [LICENSE](LICENSE).
