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
