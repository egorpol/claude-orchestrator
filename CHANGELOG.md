# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/) in its 0.x form: anything may change between minor versions.

The default install symlinks the skills into `~/.claude/skills/`, so an installed copy always runs whatever the checkout holds. A tag marks a tested state, and the installed copy runs that state only while the checkout is on the tag.

## [Unreleased]

### Added

- Secret guard at commit time: pre-commit hook and PreToolUse commit scans, a local private-name deny list with output masking, and added working-file checks. Per-repo opt-outs: `secretGuard.privateNames` and `secretGuard.workingFiles`; `install.sh --git-hook` installs both git hooks and `--check` reports the deny-list path/count and warns when it sits inside a git work tree.
- `hooks/secret-guard.py` checks what a push would send for GitHub, AWS, Anthropic, OpenAI, Slack, Google and Stripe tokens, private-key headers, home paths of any user except placeholders such as `/home/user/`, and files such as `.env`, `id_rsa` or `*.pem`. As a PreToolUse hook on Bash (`install.sh --hooks`, timeout 30) it finds `git push`, `gh repo create --push` and `gh repo edit --visibility public` in compound commands, follows `cd`, `git -C`, `env -C` and `bash -c` to the repo, and scans every commit the push could send: refspec sources, `HEAD`, branches and tags not on the target's tracking refs (`--all` for a wildcard refspec), or the full history when the target is a URL, has no tracking refs or has a `pushurl`; merges through `--remerge-diff`. Findings, a scan error, a directory that isn't a repo or the 20 s budget give `ask`, with tokens masked and at most 10 lines; it never answers `allow`, so permission rules still apply. A commit and a push in one Bash call is denied, because the hook runs before the commit exists. The same script is a git pre-push hook (`install.sh --git-hook=DIR`, exit 1 on findings) and `secret-guard.py --check [DIR]` (full history plus uncommitted changes, no time limit: 9.6 s for a 118-commit repo with 285 M characters of added lines). `git config secretGuard.homePaths false` turns off only the home-path rule in one repo, for a private repo full of its owner's paths; `-c` on the push command can't. `SECRET_GUARD_DISABLE=1` turns the guard off, `SECRET_GUARD_BUDGET_S` sets the budget.
- `delegate launch --web-search` (Codex only) turns on Codex's live `web_search` tool, for research runs that need the web. The tool runs server-side, so the shell stays offline and `--read-only` still applies. Follow-up rounds keep it.
- `delegate launch --preflight` (needs `--verify`) creates the worktree and `--link`s, runs the check once at the base in the same scrubbed environment as the run's own verify step, prints the last 30 lines, the exit code and the time, then removes the worktree, branch and run folder and exits with the check's code. No agent starts, and it takes no quota reading and writes no `delegate` row. A broken checker then shows up in seconds instead of after a full run: an acceptance script once failed on a `--link`ed path only after an 18-minute run.
- `hooks/session-mode.py` for randomised control sessions. On SessionStart it draws `orchestrator`, `claude-only` or `vanilla` (default weights 50/25/25), injects that mode's rules, shows the mode to the user and logs the draw to `~/.cache/claude-delegate/sessions.jsonl`. `resume` and `compact` keep the drawn mode. In the two control modes, PreToolUse denies delegation (`Skill(delegate)`, `codex:*` skills and subagents, Bash calls that run `codex`, `cursor-agent` or `delegate launch|followup|review|ask`) and logs each attempt. SessionEnd logs the call count (0 while no transcript exists: Claude Code writes none before the first message, so a `/clear` before the first prompt is a re-roll) and a re-roll flag. Repos in `exclude` (default: this one) always get `orchestrator`.
- `delegate quota --snapshot --reason R` appends one reading of every pool (`used_pct`, `resets_at`, the provider's `observed_at`) to `~/.cache/claude-delegate/quota.jsonl`, polled with `--source direct` under a file lock. A failed poll appends an error row. `--if-active` polls only after Claude Code transcripts changed since the last good reading, when a reset has passed, or once 10 minutes after activity stops. Session start and end (from `hooks/session-mode.py`) and `launch`, `followup` and `review` take a reading in the background; `CLAUDE_DELEGATE_QUOTA_SNAPSHOT=0` turns the run readings off. `install.sh --timer` installs a systemd user timer that runs `--if-active` every 10 minutes; the unit gets openusage's folder on its PATH, plus `CLAUDE_CONFIG_DIR`, `XDG_CACHE_HOME` and `CLAUDE_DELEGATE_QUOTA_LOG` when they are set at install time. openusage keeps no history of pool percentages, and these readings are the input for calibrating cost against pool points.
- `delegate report --auto` appends one `summary` row per session to `sessions.jsonl`: calls, tokens by class, cost units and list-price USD including subagents, per-model groups, mode, end reason, re-roll, blocked attempts, delegate commands, and each pool's `used_pct` at session start and end, with a delta only when both readings are fresh and in the same reset window. `hooks/session-mode.py` starts it detached at SessionEnd, where it takes the end reading itself, and starts `--backfill` at SessionStart for logged sessions whose transcript or subagent files have writes no summary has seen, once they have been quiet for an hour (a VS Code `/clear` once ended a session without SessionEnd). Each summary records the newest write it saw (`activity`), so a session that goes on after a back-fill is summarised again. Rows are append-only and the last summary per session wins. `CLAUDE_SESSION_SUMMARY=0` or `"summary": false` in the config turns this off.
- `install.sh --experiment` (opt-in) registers `hooks/session-mode.py` (SessionStart, PreToolUse for `Skill|Agent|Bash`, SessionEnd) and removes the `--rules` import from the user-level `CLAUDE.md`, because the hook injects the rules per mode and an import would reach control sessions too. `--hooks` registers the context guard and the secret guard. `--rules` skips the import while session-mode is registered; `--check` reports the hook, the `/next` command and an import left next to the hook.
- `/next` (`commands/next.md`, installed with the skills): continues from `HANDOFF.md`. Type `/clear` first: a command's body reaches the model as text and can't run `/clear`.
- `launch`, `followup`, `review` and `ask` append a `delegate` row to `sessions.jsonl` under the calling session (`CLAUDE_CODE_SESSION_ID`), so the summary counts real delegate use.
- `delegate cost --by-mode [--sessions] [--session-log P]` compares the control-session arms, intent-to-treat: only drawn sessions (excluded, forced and unassigned ones are counted apart), assignment from the first start row, last summary and end row win, re-rolls stay in their arm, sessions without a summary show as pending. Per arm: sessions, calls, cost units, USD, delegate commands, joined runs (applied, discarded, tokens per agent) and pool points used where both readings share a reset window. It warns when sessions were drawn under different weights.
- `delegate rate`: a blinded rater splits each finished, drawn session into items by user-prompt index. Cursor Auto (or `--agent codex`) runs in an empty temp dir and sees only the typed prompts (`/next` marked, with a note when no start-time copy was saved; command expansions, tool results and assistant text left out), the start-time HANDOFF.md copy and the commits in the session window, with words that reveal the mode redacted. It returns type, size (by requested scope), status (finished, partial, unclear) and continuation per item, validated against rubric `v1`, and appends a `rating` row. `--by-mode` then reports items, finished share and USD per finished item (all USD of rated sessions over finished items), by size and type, with per-item USD split by call timestamps.
- `hooks/session-mode.py` copies `HANDOFF.md` (git root, else cwd, else one folder down as `/next` looks: `private/HANDOFF.md` first, else a single `*/HANDOFF.md`; first 64 KiB) to `~/.cache/claude-delegate/handoffs/<session>.md` at startup and `/clear`, and logs the path (`handoff`) and the draw `weights` in each start row. Run records (`meta.json`, `runs.jsonl`) carry the launching `session`.
- `delegate report` adds the context at each typed prompt to the skeleton (`Context at typed prompts: 37k, 79k, 111k (idle hold), 242k (guard), 260k.`) and as `prompts` to the `token_usage.jsonl` row, so a report can tell growth inside an item from growth at item boundaries. Prompts are the blinded rater's typed prompts; a context-guard nudge tags the prompt it ran for (none when it ran for a task notification), an idle hold the resent prompt after it.

### Fixed

- `delegate launch` removes the half-created worktree, branch and run folder when setting up the `--link`s fails for any reason, such as a `PermissionError`; before, only a rejected `--link` was rolled back, and other errors left a worktree with no `meta.json` for `cleanup` to find.
- `install.sh --hooks` backs up and rewrites `settings.json` only when it changes, replaces an older registration of its own command (another matcher or timeout) instead of keeping both, tolerates `"hooks": null` groups, stops with a message instead of a traceback on invalid JSON, and never overwrites an earlier backup made in the same second.
- The context guard ignores events other than `UserPromptSubmit`: cursor-agent runs the hooks in `~/.claude/settings.json` as `beforeSubmitPrompt`.
- `delegate report` gives copied run rows the session row's `repo` (`owner/name`), so the two rows of one report agree; the folder path stays in `repo_path`.
- `delegate rate` also reads prompts typed while Claude was working. The transcript records them only as `queued_command` attachments, so the rater missed them (7 in the 68 local transcripts); task notifications and messages from other sessions stay out.
- `delegate report` keeps the launching session on a run that a later session cleaned up, and the skeleton names it; the skeleton also counts the session's `delegate ask` calls from the session log.
- Tests: the documented command keeps TMPDIR outside the checkout (in `.delegate/tmp/` of a Nextcloud-synced checkout, test files drew 912 sync errors), test mtimes are plausible dates, and a secret-guard test that passed only inside a git checkout runs in a real repo.

### Changed

- `delegate cost --session` adds dated list-price USD by model and source, including deduplicated subagents and separate 5m/1h cache writes; `delegate report` logs these totals while preserving historical main-only measurements.
- SKILL.md: acceptance scripts in linked folders, each checker branch run once before launch, and seeds pinned for two-run comparisons; known issues marked as handled in `--focus`, and no second review for a small, covered follow-up.
- The context guard, `hygiene.md` and SKILL.md suggest "`/clear`, then `/next`" instead of "continue from the handoff".
- Implement template: a byte invariant also says how the output should look.
- Rules split into `rules/hygiene.md` (every session) and `rules/delegation.md` (when to delegate, second opinions). `rules/orchestrator.md` imports both, so a `--rules` install loads the same text as before; the second-opinion rule is now a bullet of its own.
- Field results: the 2026-10-13 MEI 5.1 session; README totals now cover 17 sessions.
- Rules from four 2026-10-13 field reports: `HANDOFF.md` under ~10 KB with state first, committed items cut to a line, and commands that run as written; a sweep request above ~100k counts as a new item, and renders are downscaled before reading; `/field-report` offered while the cache is warm; status lines before any run of 2+ silent calls; ask before accepting terms on the user's behalf; generated IDs normalised before a render diff; stderr kept for warning checks. The second-opinion rule gives the CLI's full path and says that neither `ask` agent can check fresh web facts.
- SKILL.md: `install.sh --bin` for a bare `delegate`; web access of the `ask` agents.
- Field results: four more 2026-10-13 sessions and a `mode` column; README totals now cover 21 sessions, and the idle hold has been seen live.
- `docs/related-projects.md` folds in the 2026-10-13 wide survey (Codex with live web search): new orchestrators and routers, a quota-aware routing section, and a measurement section on the ccusage family, Claude Code's OpenTelemetry export and published controlled comparisons. It also records which of the survey's recommendations are done and adds the rest to the open list and the README roadmap.
- Rules from 15 field reports (2026-10-13 to 10-20): an item may stop at a step boundary above ~120k; every item closes with a picker; above ~100k the handoff is refreshed before a picker or hand-paste step; `HANDOFF.md` stays at the root (`.git/info/exclude`); status lines after unexpected results, before long calls and with the plan position on a resume; checks in the audience's own tool, same-day before/after runs for scoring changes, headless browser checks; ranged reads of large files; subshell `cd`, `pgrep -f`, `;` between probes, counted output, PASS/FAIL lines, whole-sentence replaces and kept file formatting.
- `/next` looks one folder down (such as `private/HANDOFF.md`) and in the project memory when the root has no `HANDOFF.md`.
- The claude-only control line says the mode holds when the user asks to delegate, and to tell them in one line. The context guard's in-item advice adds counted output, ranged reads and a handoff at the next step boundary.
- SKILL.md: acceptance scripts with checkout-relative paths, `sys.executable` and one planted failure; `ask --agent both --effort high` as the review of uncommitted main-tree work; no polling a background `ask`; brief inputs outside the worktree; audit findings with reproducible checks.

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
