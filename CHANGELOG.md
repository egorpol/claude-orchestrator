# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/) in its 0.x form: anything may change between minor versions.

The default install symlinks the skills into `~/.claude/skills/`, so an installed copy always runs whatever the checkout holds. A tag marks a tested state, and the installed copy runs that state only while the checkout is on the tag.

## [Unreleased]

## [0.1.0] - 2026-10-13

First versioned release. The repo went public on 2026-10-13. The earlier commits are listed under [Before 0.1.0](#before-010).

### Added

- **`delegate` skill and CLI** (stdlib-only Python 3.9+):
  - `quota`: % left and pace per pool (Claude 5h/7d, Codex 5h/7d, Cursor Auto, Cursor premium), via openusage.
  - `ratings`: Artificial Analysis ratings, through the API key or a leaderboard scrape.
  - `route`: ranks delegates per task type, honouring `prefer` lists.
  - `launch`: Codex CLI or Cursor's headless agent in its own git worktree, with a brief, a log and a last-message file, plus an optional `--verify` check. Ends with an exit packet of at most 20 lines in the task notification.
  - `review`: a read-only review by the other vendor.
  - `followup`: the next round, resuming the same agent session.
  - `verify`, `status`, `wait`, `diff`, `apply` (uncommitted), `cleanup`.
  - `ask`: cheap read-only questions; `--agent both` gets second opinions from Codex and Cursor.
  - `cost`: run-log totals, or one Claude Code session's cost units and idle cache rebuilds.
  - Brief templates for implement, mechanical, review and research jobs.
- **`delegate report` and the `/field-report` skill.** One command in any project's chat writes a field report skeleton with the session's numbers filled in. It also upserts the session's row in `feedback/token_usage.jsonl` and copies the session's delegate runs into `feedback/runs.jsonl`.
- **`hooks/context-guard.py`**, a UserPromptSubmit hook:
  - from about 120k context, it asks for a handoff before a new work item starts;
  - after 60 min idle at 100k+ context, it holds the first typed prompt once, because the expired prompt cache makes that turn rewrite the whole context.
- **`rules/orchestrator.md`**, short rules for every session: one work item per session, delegate mechanical work, decisions in a picker with second opinions, git steps need approval, data checks, zsh pitfalls.
- **`install.sh`**: symlinks or copies the skills. `--check` checks the prerequisites, `--hooks` registers the hook, `--rules` imports the rules and `--bin` puts `delegate` on PATH.
- **Docs**:
  - `docs/cost-analysis.md`: the 2026-10-09 baseline and the eight cost levers;
  - `docs/field-results.md`: per-session measurements;
  - `docs/verified-behaviour.md`: dated facts about Codex, Cursor and Claude Code, tested live;
  - `docs/related-projects.md`: a survey of similar tools.
- `CHANGELOG.md` and an MIT licence.

### Changed

- `feedback/` (field reports and their JSONL logs) and `HANDOFF.md` (session-to-session state) are now gitignored. Both were tracked before 0.1.0 and stay in that history. The public evidence for the README's numbers is `docs/field-results.md`.
- `docs/optimization-plan.md` is split into `docs/cost-analysis.md` and `docs/field-results.md`, because every lever in the plan had been built.
- README: why the repo exists, the measured results so far, and a roadmap. The dated "Verified behaviour" sections moved to `docs/verified-behaviour.md`.

## Before 0.1.0

The repo was private and unversioned before 0.1.0. Its 12 commits:

- 2026-10-09 `c4b3ea6`: `delegate` skill with quota, ratings, routing and worktree launches.
- 2026-10-09 `2f7d27c`: `review`, `followup` and `verify` rounds. Cursor delegates default to `--model auto`.
- 2026-10-10 `f4681a9`: first field reports and the measured optimisation plan.
- 2026-10-10 `68a86ce`: exit packets, per-round usage, `ask`, `cost`, the context guard (from 250k) and the first 31 tests.
- 2026-10-10 `04a757d`, `b606961`: Grok and Composer routed on the Cursor Auto pool.
- 2026-10-10 `92f25d5`: fixes for CRLF patches, Codex per-round usage on resumed rounds, and run logging.
- 2026-10-10 `4178126`: handoff at item boundaries from 120k context; user-level rules.
- 2026-10-11 `0ecc1c8`: `delegate wait`; `cost --since` takes a local date.
- 2026-10-12 `4c74b67`: the idle hold in the context guard; `cost --session` counts idle rebuilds.
- 2026-10-13 `02a9f87`: decisions in pickers; `ask --agent both`.
- 2026-10-13 `958b4ab`: MIT licence and README positioning; `launch` rolls back the worktree when `--link` is rejected.

[Unreleased]: https://github.com/egorpol/claude-orchestrator/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/egorpol/claude-orchestrator/releases/tag/v0.1.0
