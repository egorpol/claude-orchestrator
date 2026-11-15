# Delegate agents and run artifacts

Read before selecting per-agent flags, handling environment gotchas or inspecting run artifacts; paths such as `templates/` and `scripts/catalog.json` are relative to the skill folder.

## Agents

- **Codex** (`codex exec --json`): `-s workspace-write` or `read-only`, `-C <worktree>`, `-o last-message.md`, prompt on stdin. Per-round usage from `turn.completed.usage` lands in meta history. Follow-ups: `codex exec resume <session-id>` (id from log; sandbox via `-c sandbox_mode=…`). Models: `gpt-6.1-sol` (proven), `gpt-6-astra` (expect faster Codex pool drain than Sol), `gpt-6.1-terra` at `high` (faster, cheaper mechanical option; pool drain unmeasured). Effort `low`…`xhigh`, then `max`.
- **Cursor** (`cursor-agent -p`, `~/.local/bin`): **`--model auto`** stays the default (Auto pool, SpaceX in-house). Named Grok (`cursor-grok`, `grok-4.7-*`) and Composer (`composer-2.5*`) bill Cursor's "Cursor Models" quota, which is the same pool as Auto (`cursor-auto`), not premium. Use named models only if the user asks, or through the frontier fallback for `ask --agent both` and default reviews when Codex quota is low.
  - Runs with `--sandbox enabled --trust --workspace <worktree> --output-format stream-json`. Sandboxed shell auto-runs; writes outside fail with EPERM; no network.
  - **Never add `--force`**: it silently bypasses the sandbox (tested 2026-10-09). `--unsandboxed` (network, installs) needs the user's OK; with `install.sh --hooks`, the secret-guard hook asks before such a launch and before a `followup` of that run.
  - `--mode ask` (`--read-only` and Cursor reviews) reads files and runs simple commands such as `ls`, but rejects `git show` and compound commands (2026-11-04): evidence at a commit goes into the question, the tree or a patch file.
  - Follow-ups: `--resume <session_id>` from the stream init event. If `cursor-agent status` says "Not logged in", the user runs `cursor-agent login`.
- **Claude subagents** (Agent tool, `sonnet|haiku`) appear in `route` for comparison; 0.8 penalty (orchestrator's Claude pool).
- **Codex plugin** (`openai/codex-plugin-cc`): `/codex:review` on the *current checkout*. Use `delegate` for anything that writes.

## Gotchas

- The launcher strips Cursor AppImage variables (`LD_LIBRARY_PATH`, `/tmp/.mount_*` PATH entries), `VSCODE_*`, `CURSOR_*`, `CLAUDE_CODE_*` (including the messaging token), and virtualenvs that belong to another repo.
- Python editable installs: inside the worktree, `import pkg` can resolve to the main checkout. Check with `python -c "import pkg; print(pkg.__file__)"` or use `PYTHONPATH=.`.
- A gitignore rule `dir/` does not match a symlink; `--link` writes exact paths (and `/.delegate`) to `.git/info/exclude`. Paths not independently gitignored (including tool `/path` excludes) warn at launch; newly appended lines are owned in `meta.excluded` and removed or transferred on cleanup. Only real link symlinks are left out of `changes.patch` and the cleanup dirty check.
- **Codex's sandbox blocks Jupyter kernel sockets** (`nbclient` fails with `PermissionError`). Run extracted cells with the venv's `ipython`, or run notebooks in `--verify`, which runs outside the sandbox.
- **`--link`ed paths are read-only inside Codex's sandbox:** the symlink resolves outside the worktree (`Read-only file system`, tested 2026-10-30). Have the delegate write output under the worktree, and leave writes to linked folders to `--verify`.
- **Cursor's sandbox has no pty:** `pty.openpty()` raises `OSError: out of pty devices` (run `cursor-reader`, 2026-11-07). Pty tests skip there (`unittest.skipUnless`) and run in `--verify`, outside the sandbox; adjudicate a pty finding with a local probe.
- **Clean up only what the session created.** Use scratchpad `--outdir` for builds, leaving the repo's `build/` intact.
- **If the Bash tool runs zsh,** an unquoted `$var` does not split into words. Loop over a file list with `while read`.
- Untracked files count as changes. `run.sh` exports `PYTHONDONTWRITEBYTECODE=1`, so test runs leave no `.pyc`; other build output (`node_modules`, `dist`) needs a `.gitignore` in the target repo, or it lands in the patch.

## Run directory

`~/.cache/claude-delegate/runs/<repo>/<name>/` (`--root` or `$CLAUDE_DELEGATE_ROOT`):
- current round: `brief.md` · `prompt.md` · `run.sh` · `log` · `last-message.md` · `exit_code` · `verify.log` / `verify_code`
- `round-<k>/` (includes `changes.patch`: the diff against base at followup time, with edits made between rounds, so one round's delta is the interdiff of two archives), `review-<n>/` (includes `review.patch`, the diff that reviewer saw; plus `pre_digest` / `post_digest`)
- `meta.json` (`main_dirty`: a HEAD launch's dirty main-tree paths), `wt/`, `changes.patch` (rewritten at the end of each round and by `followup`, `diff`, `review`, `apply`), `applied` (after `apply`)
