# Orchestrator rules

From the claude-orchestrator field reports (`feedback/`). They load in every session, including sessions that never invoke the `delegate` skill.

- **One work item per session.** At an item boundary above ~120k context, write or refresh the handoff (memory files plus `HANDOFF.md`: state, open PRs or runs, decided cases, next step). Then offer `/clear` and "continue from the handoff" in one line before starting the next item, even after "go on". Below 120k, hand off too when the next item is large, such as docs that read or write tens of KB. Always write `HANDOFF.md`, even if it only points at a plan: a "continue from the handoff" that finds no file costs the next session several calls of transcript digging. These count as new items:
  - a finished build that turns into Q&A support;
  - the user turning to a different topic.

  Also hand off before a wait of over an hour, since the prompt cache expires.
- **Claude's quota is the scarcest pool.** Before doing mechanical work inline, use the `delegate` skill if the work has a clear spec: tests from a written spec, the same update across several docs, or a near-copy of an existing script. Cursor Auto usually has spare quota. Keep design, data checks and review here.
- **A commit, merge, push or tag listed in a handoff or plan is not approval.** Neither is a small step: a local commit on a feature branch also waits. Ask in one line at the end of the item, bundling all git steps, and keep working on independent parts meanwhile.
- **Data changes:** before a PR, check every changed headline value against a source; passing gates are not enough. Test a hypothesis with one cheap live sample before writing it into a plan.
  - Name the check that passed ("passes RNG"), not "validates", until the project's full gate has run. Look for an existing validator in the project and its libraries before writing one.
  - When a note qualifies a headline value (a source's real date, say), carry it into every doc that states the value.
  - Doc examples with invented content use fictional IDs, never a real record's.
- **Shell:** the Bash tool runs zsh, not the user's fish.
  - `(cmd)` substitution fails.
  - An unquoted `$var` does not split into words; loop over a file list with `while read`.
  - Write scratch scripts to files rather than nesting heredocs.
  - Don't `cd` away from the project root; use absolute paths or pass the path as an argument. The new directory can stick, and the next relative path then runs in the wrong place.
  - `=word` at the start of a word expands to a command path, so `echo ===` fails; use `echo ---`.
  - Quote globs meant for the called program (`--include='*.py'`); a glob that matches nothing aborts the command.
  - Run each long command as its own background task. Killing the child of an `a && b` chain lets the chain go on to `b`.
- **Edits:** edit prose with a Python replace that asserts exactly one match; `sed` on wrapped Markdown silently matches nothing. For before/after checks, use `git show HEAD:path > tmp`, not `git stash`, which unstaged `git mv` renames.
- **Clean up only what this session created.**
