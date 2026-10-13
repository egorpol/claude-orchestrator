# Verified behaviour

Facts tested live, dated, so a reader can tell what may have changed since.

## 2026-10-09

- **`cursor-agent --force` silently disables `--sandbox enabled`.** Without `--force`, headless sandboxed commands auto-run, writes outside the worktree fail with EPERM, the edit tool rejects outside paths, and there is no network. The launcher never passes `--force` unless you give it `--unsandboxed`.
- Claude Code running inside Cursor inherits Cursor's AppImage environment (`LD_LIBRARY_PATH=/tmp/.mount_*`). The launcher scrubs that, `VSCODE_*`, `CURSOR_*` and `CLAUDE_CODE_*` (including the messaging token), plus virtualenvs belonging to other repos, before starting a delegate.
- Claude Code discovers symlinked skill directories, which is what `install.sh` relies on.
- Cursor `--mode ask` cannot run shell commands (they come back `permissionDenied`); it can read files. That is why reviews get the diff as a file at `wt/.delegate/review-N.patch`.
- Session resume works headless for both agents. `cursor-agent -p --resume <session_id>` works, and so does `codex exec resume <id>`; the latter has no `-s`/`-C`, so the launcher passes `-c sandbox_mode=…` and runs from the worktree.
- Trial 2: Cursor `auto` implemented `--top N` (25s, checks passed). A Codex review found a real bug (`--top -1` silently dropped a row via negative slicing), the same one I had found independently. `followup --from-review 1` resumed the Cursor session and fixed it (20s, checks passed), and a Cursor re-review approved it.
- End-to-end trial: Codex gpt-6.1-sol @xhigh and Cursor composer-2.5 ran the same toy brief in parallel worktrees. Both handled malformed real data and neither committed. Composer's summary misquoted the brief, so review the patch rather than the prose.

## 2026-10-10

- **`AGENTS.md` loading:** headless `codex exec` reads only `AGENTS.md` (ignores `CLAUDE.md` and `.cursor/rules`). `cursor-agent -p` reads `AGENTS.md`, `CLAUDE.md` and `.cursor/rules` with `alwaysApply`.
- **Codex `--json`:** `turn.completed.usage` exposes `input_tokens`, `cached_input_tokens`, `output_tokens` and `reasoning_output_tokens`; the launcher records per-round usage in meta history.
- **`delegate ask`** answered in about 10s with `path:line` citations (Cursor auto, read-only).
- **Cursor pools:** openusage's `plan_auto_percent_used` is Auto + Grok + Composer spend over $450, so named Grok and Composer share the Auto pool (`cursor-auto`). Named Claude/GPT models bill `plan_api` (premium). Checked against per-model spend at two snapshots; a 6.5k-row grok-4.7-low labelling run moved Auto by about 2.7 points and premium by 0.
- **CRLF repos:** `apply` failed on CRLF and mixed-ending files because patches were read in text mode. Patches are now kept as bytes; a lifecycle test applies a delegate's edit to a CRLF file byte for byte.
- **Codex usage on resumed rounds:** `turn.completed.usage` is the running thread total (a resumed round reported 139k input after 114k in round 1). Each round now records its own delta.
- **Exit packet:** when a background run finishes, `run.sh` prints a compact ≤20-line packet (state, verify, top files, Digest risks, usage, artifact paths) into the task notification — that is the status; do not poll.

## 2026-10-11

- **`delegate wait`** on a finished real run printed its packet and exited 0. Tests cover the four invariants: no return while verifying, no hang on a dead job, reviews re-listed on every poll, and a one-line exit 3 on timeout.
- **`delegate cost --since 2026-10-11`** now reads a local date. On the real run log it counts 3 runs, including one applied at 01:09 local time; with `TZ=UTC` it counts 2.

## 2026-10-12

- **Idle cache rebuilds:** `delegate cost --session` now lists them. On the real transcripts it matched an independent scan exactly: 9b03e065 has 1 rebuild (91 min, 141,484 written, 15.9% of cost units), 762dc86f has 4 (19.7%) and still counts 389 calls.
- **Context guard, idle hold:** fed a real 192k transcript whose last call was a day old, the hook held the prompt, and let the same prompt through on the second run. It never holds a prompt that starts with `<` or `/`, because UserPromptSubmit also fires for task notifications (seen live: the size guard fired on one) and `/loop` turns (Claude Code hooks docs), and a held one never reaches Claude. Not yet seen in a live session.

## 2026-10-13

- **`delegate ask --agent both`** returned one block per agent; both cited the right line (Codex 10 s, Cursor 26 s). On a real decision with a 1.5 KB question, Codex took 20 s and Cursor 279 s.
- **`delegate cost --session latest`** run from a subfolder finds the session through the nearest parent folder that has a Claude project dir.
- **A rejected `--link`** (here `.delegate/x`) no longer leaves an orphan worktree and branch behind: `launch` removes both and the run dir, so the name is free again. Seen live before the fix, then probed live after it.
- **`CLAUDE_CODE_SESSION_ID`** is set in Claude Code's Bash tool environment (2.1.296, in Cursor) and names the current transcript, so `delegate report` defaults to it instead of `--session latest`, which picks by mtime.
- **`delegate report --dry-run --session 762dc86f`** printed 389 calls and cache_read 174,522,404, the same as `delegate cost --session 762dc86f`. A real write kept the JSONL files at mode 644.
- **`delegate report` from another project** (camat_v2, session `40ff2d3a`, through `/field-report`) wrote the skeleton, one `token_usage.jsonl` row and one copied `runs.jsonl` row, and kept mode 644. The copied run row carried the run log's folder name as `repo` while the session row had `owner/name`; copies now take the session row's `repo` (unit-tested, not yet seen live).
- **Codex web search in `codex exec`** (codex-cli 0.159.1): `-c web_search="live"` works under `-s read-only`. One call made 6 web searches and returned ccusage's latest release (v20.0.26, 2026-09-27). `--search` is a flag of the interactive TUI only. Example: `codex exec -m gpt-5.6-terra -c 'web_search="live"' -s read-only --skip-git-repo-check "<question>"`. `delegate launch --web-search` passes the same option.
- **Hook events, Claude Code 2.1.296 CLI.** Probe: one logging hook in a scratch project's `.claude/settings.json`, registered for SessionStart, SessionEnd and PreToolUse (matcher `Skill|Agent|Bash`), appending its stdin to a JSONL file.
  - Headless `claude -p`: SessionStart `startup`, then SessionEnd `other`. `claude -p --resume <id>`: SessionStart `resume`, same session id.
  - Interactive: SessionStart `startup`. `/clear` gives SessionEnd `clear` (old id), then SessionStart `clear` (new id), in the same second. `/compact` gives SessionStart `compact` with the same id. `/exit` gives SessionEnd `prompt_input_exit`.
  - SessionStart `additionalContext` of 5.7 KB arrives inline: the transcript holds a `hook_additional_context` attachment and no `tool-results` file. The `systemMessage` shows in the TUI.
  - PreToolUse `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "<reason>"}}` blocked `Bash`, `Agent` and `Skill` calls, and the model quoted `PreToolUse:Skill hook error: <reason>` verbatim. A plain Bash call in the same session ran.
  - Stdin fields: SessionStart has `source`, `model`, `context_tokens`, `seconds_since_last_response`; SessionEnd has `reason`; all have `session_id`, `cwd`, `transcript_path`.
  - The project trust dialog defaults to "No, exit"; project hooks run only after the folder is trusted.
- **Hooks reload mid-session in the VS Code extension** (in Cursor, 2.1.296): a PreToolUse hook added to `.claude/settings.local.json` fired on the next Bash call of the running session.
- **SessionStart and SessionEnd in the VS Code extension** (2.1.296). Probe (logging hook in `.claude/settings.local.json` for SessionStart, SessionEnd, PreToolUse):
  - A new conversation tab gives SessionStart `startup`, and its `additionalContext` reaches the model. In a fresh tab, `/clear` gives SessionEnd `clear` (old id), then SessionStart `clear` (new id), in the same second, as in the CLI. Closing the tab gives SessionEnd `other`.
  - Exception, seen once: `/clear` in a tab whose process had run for 2.5 h, with the hooks added mid-session, started a new process, logged SessionStart `startup` and no SessionEnd. The old process kept running. So a session's end can go unlogged, and the session log needs a back-fill.
- **Headless cursor-agent runs Claude Code project hooks:** a `delegate ask` Cursor run (`--model auto`) in this repo fired the probe from `.claude/settings.local.json` as lowercase `sessionStart` and `sessionEnd` (reason `completed`), with Cursor's own fields (`conversation_id`, `cursor_version`, `composer_mode`, `is_background_agent`, `workspace_roots`). Hooks must ignore event names they don't know; `hooks/session-mode.py` does. Whether cursor-agent also reads the user-level `~/.claude/settings.json` hooks has not been tested.
- **Nested relative `@imports`:** `CLAUDE.md` importing `@sub/a.md`, which imports `@b.md`, loads `sub/b.md` (headless, 2.1.296). `rules/orchestrator.md` relies on this to import `hygiene.md` and `delegation.md`.
- **ccusage 20.0.30** (`npx ccusage@20.0.30 claude session --id <uuid> --json --mode calculate --offline`) matched a hand calculation from list prices exactly: 762dc86f $69.9778368 (389 Opus 5.5 calls, 1-hour writes at 2×, reads at 0.05×), e35ea49e $2.3064334 (32 Opus calls plus 2 Haiku 5.5 calls from `subagents/`, 5-minute writes at 1.25×). It attributes subagent transcripts to the parent session and dedupes them by message id (34 entries against `cost.py`'s 32). Its JSON gives per-entry tokens but no per-entry cost, no TTL split and no main/subagent split. Token totals match `cost.py` (762dc86f: 178,280,368).
- **Cost units overweight Opus 5.5 cache reads:** units weight reads 0.1×, while Opus 5.5 bills them at 0.05× ($0.20 against $4 input). For 762dc86f, reads are 66.6% of cost units and 49.9% of list-price USD.
- **`delegate cost --session` list price** reproduces both ccusage values to 1e-6 (762dc86f $69.9778368; e35ea49e $2.3064334, of which $0.0020938 from 2 Haiku subagent calls). Transcript usage splits writes in `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`. Of 3,752 deduplicated calls across 59 transcripts and their subagent files, 3,750 had `speed: standard` and 2 had no `speed`; none had a `fallback_credit`; all 9,084 usage rows had `inference_geo: global`. LiteLLM's local table (`~/.cache/openusage/pricing/litellm.json`) agrees with the catalog prices: Opus 5.5 flat, Haiku 5.5 above 100k prompt tokens at $0.50 in, $2.50 out, $0.05 read, $0.625 / $1.00 writes; regional endpoints (`us.`, `eu.`) cost 1.1×.
- **openusage 0.25.0 keeps no history of pool percentages.** Its `telemetry.db` (90-day retention) holds only cumulative token counters per client in `balance_observations`. `openusage export --source daemon` answered in 57 ms, but its Claude snapshot was 13 min old and showed 5h `used: 0` while the session had spent 3%; `--source direct` took 6.7 s and was current, with reset times for both windows. The daemon's default poll interval is 30 s, so it refreshes Claude's figures less often for a reason not found.
