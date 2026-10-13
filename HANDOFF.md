# Handoff (2026-10-13, second session)

**State:** the repo is public with its full history: this session's commit sits on top of `02a9f87`, a normal push, then a visibility change. This session added:
- an MIT `LICENSE` ("Copyright (c) 2026 Egor Polyakov", as in the other public repos);
- a README "How this differs" section, an Install heading and a License section;
- `docs/related-projects.md` refreshed to 2026-10-13 by Cursor Auto (run `related-refresh`: stats for 23 repos, 10 new entries; claims spot-checked);
- a fix: a rejected `--link` left an orphan worktree and branch, and `launch` now rolls them back (new test; 58 pass; probed live).

**Decided (2026-10-13), as decision — why — cost if wrong:**
- Publish this repo as it is, with full history and `feedback/` tracked — the user's call: nothing in it is sensitive, and the projects it names are meant to go public. A scan of all 11 commits (139 file versions) found no tokens, keys, private keys or home paths; the only email is `test@example.invalid` — anything in the history is public for good (forks, event archives).
  - This replaced the earlier plan of a fresh one-commit history in a new repo, with the old repo kept as a private archive. That plan only made sense if the history held something to hide. The fresh commit `162e892` is in the reflog.
- MIT — matches the 4 other public repos and superpowers (Codex A, Cursor A) — no explicit patent grant.
- Keep the gmail author email — already public in 4 repos — none.
- README "How this differs": draft A with Codex's edits (Cursor preferred its own draft) — none.
- Development stays on public `main`, plus a local pre-push guard (both models) — the checkout stays the live install — a guard backs up review and doesn't replace it.
- Personal tuning goes in an optional catalog override (both models) — public defaults stay stable — define the merge per key: `prefer` lists and the `delegates` array lose new public entries under a naive replace (Codex).

**Next items (one per session):**
1. Pre-push guard: an untracked `.git/hooks/pre-push`, optionally offered by `install.sh --hooks`. Since project names aren't sensitive, it should block secrets in outgoing commits: GitHub/OpenAI/Anthropic/AWS token patterns, private keys, `key|token|secret = <long value>`, and home paths. This session's scan patterns are a starting point; they gave no false positives except long path strings.
2. Catalog override: `catalog.local.json` (gitignored) or `~/.config/claude-delegate/catalog.json`, merged per key over `catalog.json`; tests and docs to match.
3. Others: `codex exec --output-schema` for machine-readable reviews; ideas in the survey's "Still open" (loop cost scoring, pre-launch approval policies, `.worktreeinclude`).

**Open:** the idle hold has not been seen live yet.
