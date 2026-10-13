# Handoff (2026-10-13)

**State:** the 2026-10-12/13 camat field reports are processed, and the user's request for decisions in pickers is built. See "Field results, 2026-10-12/13" in [docs/optimization-plan.md](docs/optimization-plan.md).
- **Rules:** discrete choices go into `AskUserQuestion`, recommended option first. Lasting decisions (design, data policy, public text, publishing; not git yes/no or `/clear`) also get Codex's and Cursor's picks via `delegate ask - --agent both < q.md`, run in the background. Other new rules: a status line before several reads in a row; git approvals as a picker option.
- **Code:**
  - `ask --agent both` runs both vendors in parallel and keeps one answer if the other fails.
  - `cost --session latest` walks up to the nearest parent folder that has a project dir.
  - 57 tests pass; both changes were tested live (README "Verified behaviour (2026-10-13)").
- **SKILL.md:** copy `--verify` from the CI workflow; edit the spec before launch when a brief narrows it; duration data points.

**Open:**
- This session's edits and the user's two new reports plus JSONL rows are uncommitted. The user is asked at the end of the session.
- The idle hold has not been seen live yet (unchanged from 2026-10-12).

## Next item: make the repo public

**Decided (2026-10-13, picker; Codex: A, Cursor: A):** option A.
- One fresh commit on a new history.
- `feedback/` and `HANDOFF.md` become gitignored. Nextcloud still syncs them between the user's machines.
- `docs/optimization-plan.md` keeps the aggregate numbers as the public evidence.
- Future field reports stay private.

**Risks the second opinions raised, to handle in that item:**
1. Old clones keep the old history. Before rewriting, keep a `git bundle` of it in a gitignored or out-of-repo place.
2. README ("Field reports live under `feedback/`"), the plan's "Sources" list and CLAUDE.md all point into `feedback/` or call the remote private. Reword them for readers who don't have the folder.
3. The plan describes the Webern sessions; that repo (`webern_op27`) is private. The exact repo name is not in the plan (Cursor corrected the question), but it appears in `feedback/token_usage.jsonl`. Review the plan's narrative too, not just the names. The other projects it names are public: eu-pd-composers, PublicDomainSheetMusicFinder, camat_v2, audiospylt, egorpol.github.io (checked with `gh repo view`, 2026-10-13).

**Audit so far (2026-10-13):** no home paths or email addresses in tracked files; the author email appears only in commit metadata; history has 10 commits.

**Still to decide, as pickers:** licence (MIT or Apache-2.0); whether to author the fresh commit with the GitHub noreply email; whether to replace `main` on the same remote (force-push, needs explicit approval) or create a new repo. Licence and README positioning count as lasting decisions, so get second opinions.

**Also in that item:** refresh [docs/related-projects.md](docs/related-projects.md) (survey from 2026-10-09). The README then needs a short "how this differs" paragraph for outside readers. Quick wins not yet taken from that survey: machine-readable reviews via `codex exec --output-schema`, and superpowers' ruling ledger (`decision — why — cost if wrong`) in the handoff's decided cases.
