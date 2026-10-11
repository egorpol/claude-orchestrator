# Handoff (2026-10-12)

**State:** the 2026-10-11/12 batch of field reports (Webern edition headers, Webern breaks + proofreading Q&A, homepage table grid + site audit + note) has been processed. See "Field results, 2026-10-11/12" and lever 8 in [docs/optimization-plan.md](docs/optimization-plan.md).
- **Finding:** idle cache rebuilds are 12.7% of all logged orchestrator cost (7 of 14 sessions; half after a user's break, half after a long run's notification). The user chose "block once" for the context guard.
- **Code (applied, uncommitted):** run `idle-guard` (Cursor Auto; Codex review found 2 issues, fixed inline: bad env values crashed the hook, an untimed call did not restart the gap clock). The context guard holds the first typed prompt after ≥60 min idle at ≥100k context once and lets the resend through; prompts starting with `<` or `/` (task notifications, `/loop`) always pass. `delegate cost --session` lists idle rebuilds.
- **Docs:** rules (Q&A `/clear` as a picker option, <10-line changes inline, identifiers never from memory, deployed-not-checkout, exact-value tallies, tool-behaviour claims, vague reports, `pkill -f`), SKILL.md, research template, README, CLAUDE.md, plan.

**Open:**
- Commit and push of the batch wait for the user's approval. After the commit, `delegate cleanup idle-guard` and `delegate cleanup wait-since` (the auto-mode classifier blocked `cleanup wait-since` once; its worktree is already gone).
- The size guard fired on a task notification in this session, which confirms that notifications pass through UserPromptSubmit. The idle hold itself has not been seen in a live session yet. When it first fires, check that the block message shows and that a resend goes through, then note it in README's "Verified behaviour".

**Next:** the next batch of field logs in `feedback/`. Run `delegate cost --session <id>` per session; it now prints idle rebuilds too.
