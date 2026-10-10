# Orchestrator rules

From the claude-orchestrator field reports (`feedback/`). They load in every session, including sessions that never invoke the `delegate` skill.

- **One work item per session.** At an item boundary above ~120k context, write or refresh the handoff (memory files plus `HANDOFF.md`: state, open PRs or runs, decided cases, next step). Then offer `/clear` and "continue from the handoff" in one line before starting the next item, even after "go on". These count as new items:
  - a finished build that turns into Q&A support;
  - the user turning to a different topic.

  Also hand off before a wait of over an hour, since the prompt cache expires.
- **Claude's quota is the scarcest pool.** Before doing mechanical work inline, use the `delegate` skill if the work has a clear spec: tests from a written spec, the same update across several docs, or a near-copy of an existing script. Cursor Auto usually has spare quota. Keep design, data checks and review here.
- **A merge, push or tag listed in a handoff or plan is not approval.** Ask in one line, bundling all outward git steps, and keep working on independent parts meanwhile.
- **Data changes:** before a PR, check every changed headline value against a source; passing gates are not enough. Test a hypothesis with one cheap live sample before writing it into a plan.
- **Shell:** the Bash tool runs zsh, not the user's fish.
  - `(cmd)` substitution fails.
  - An unquoted `$var` does not split into words; loop over a file list with `while read`.
  - Write scratch scripts to files rather than nesting heredocs.
- **Edits:** edit prose with a Python replace that asserts exactly one match; `sed` on wrapped Markdown silently matches nothing. For before/after checks, use `git show HEAD:path > tmp`, not `git stash`, which unstaged `git mv` renames.
- **Clean up only what this session created.**
