# Session rules

From the claude-orchestrator field reports (`feedback/`): work items and handoffs, decisions, data checks, shell and edits. They hold in every session, with or without delegation.

- **One work item per session.** At an item boundary above ~120k context, write or refresh the handoff (memory files plus `HANDOFF.md`: state, open PRs or runs, decided cases, next step). Then offer `/clear` and "continue from the handoff" in one line before starting the next item, even after "go on". Below 120k, hand off too when the next item is large, such as docs that read or write tens of KB. Always write `HANDOFF.md`, even if it only points at a plan: a "continue from the handoff" that finds no file costs the next session several calls of transcript digging. These count as new items:
  - a finished build that turns into Q&A support. Make "/clear and continue from the handoff" an option in the picker that closes the first answer: offered four times as a closing sentence, it was passed over while one session ran on to 223k;
  - the user turning to a different topic.

  Also hand off before a wait of over an hour. The prompt cache expires, and the first turn after the wait rewrites the whole context at 2×: 12.7% of all logged orchestrator cost, half of it from runs that finished after an hour.
- **A commit, merge, push or tag listed in a handoff or plan is not approval.** Neither is a small step: a local commit on a feature branch also waits. Ask once at the end of the item, as a picker option, bundling all git steps, and keep working on independent parts meanwhile.
- **Decisions go in a picker.** When a reply asks the user to choose between 2–4 discrete options, ask with `AskUserQuestion`, not in prose. Put the recommended option first, labelled "(Recommended)", with measured numbers in each description. Use prose only for open answers, such as a link or a name.
- **Status lines:** before several reads or probes in a row, write one line saying what they are for. Without it, the harness asked for a status update 9 times over two sessions (2026-10-12/13).
- **Data changes:** before a PR, check every changed headline value against a source; passing gates are not enough. Test a hypothesis with one cheap live sample before writing it into a plan. The same goes for a tool-behaviour claim in a doc or handoff ("the renderer draws X"): probe it on the current file, and keep the minimal example next to any such claim you write.
  - Name the check that passed ("passes RNG"), not "validates", until the project's full gate has run. Look for an existing validator in the project and its libraries before writing one.
  - When a note qualifies a headline value (a source's real date, say), carry it into every doc that states the value.
  - Doc examples with invented content use fictional IDs, never a real record's.
  - Never write an identifier (GND, VIAF, ISNI, Wikidata QID, DOI) from memory; look it up. Dates and names inherited from an import or a conversion are claims to check too.
  - Check public claims against what is deployed, not the checkout. Tally categorical values by exact match: a substring test (`'eu' in flag`) counted 252 where the source said 65.
- **Vague reports:** when the first check shows nothing wrong, show it and ask what is missing before widening the test matrix. Ten calls across browsers, widths and themes once failed to reproduce a complaint that was about taste.
- **Shell:** the Bash tool runs zsh, not the user's fish.
  - `(cmd)` substitution fails.
  - An unquoted `$var` does not split into words; loop over a file list with `while read`.
  - Write scratch scripts to files rather than nesting heredocs.
  - Don't `cd` away from the project root; use absolute paths or pass the path as an argument. The new directory can stick, and the next relative path then runs in the wrong place.
  - `=word` at the start of a word expands to a command path, so `echo ===` fails; use `echo ---`.
  - Quote globs meant for the called program (`--include='*.py'`); a glob that matches nothing aborts the command.
  - `pkill -f PATTERN` can match its own shell's command line and kill it; stop a server with `fuser -k PORT/tcp` or a saved PID.
  - Run each long command as its own background task. Killing the child of an `a && b` chain lets the chain go on to `b`.
- **Edits:** edit prose with a Python replace that asserts exactly one match; `sed` on wrapped Markdown silently matches nothing. For before/after checks, use `git show HEAD:path > tmp`, not `git stash`, which unstaged `git mv` renames.
- **Clean up only what this session created.**
