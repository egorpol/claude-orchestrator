# Field report: homepage table grid, site audit, eu-pd-composers note (2026-10-11/12)

Orchestrator: Claude Opus 5.5 (session `032191ec`). Started after `/clear` from `egorpol.github.io/private/HANDOFF.md`. Three items in one session, because the user's second prompt bundled two: (1) fix tables the user "doesn't see properly", (2) a full read-only site audit by Codex and Cursor, (3) a new Notes post about the rebuilt eu-pd-composers project. Nothing committed: the table fix, the note draft and a CHANGELOG line are uncommitted; the audit synthesis is handed off. Machine-readable rows: [`token_usage.jsonl`](token_usage.jsonl), [`runs.jsonl`](runs.jsonl).

## Flow and cost

| # | Step | Outcome |
|---|---|---|
| 1 | Handoff, then built HTML and live CSS checked | 3 `<table>`s present, table CSS deployed |
| 2 | Headless Firefox at 1280/390 px, light/dark; headless Brave on the live URL | Tables render the same everywhere: row rules, no column lines, no frame. Not reproduced |
| 3 | Asked the user what "not properly" looks like | "No lines within and around the table" |
| 4 | Codex `table-grid` (gpt-6.1-sol @medium, 58 s, +4/−2) | Full cell borders, padding, surface fills; verify (jekyll build + grep) passed; Codex's own Firefox crashed in the sandbox |
| 5 | Orchestrator screenshots: Pixel/Plain × light/dark × 1280/390 | Grid correct; narrow table's frame hugs its columns; applied, run cleaned up |
| 6 | `shoot.sh`: build + 72 full-page screenshots of 9 pages | Inputs for the visual audit |
| 7 | Codex `note-facts` on the other repo (@high, read-only, 7 m 23 s) | 40 KB sourced fact sheet with conflicts and a privacy-flags section |
| 8 | Codex `audit-tech` (@xhigh, read-only, 15 m 22 s) and Cursor `audit-design` (auto, read-only, 1 m 10 s), detached, one `delegate wait` | Reports 15.6 KB and 12.5 KB; not yet read |
| 9 | Picker: angle, agent paragraph, r020 handling | 3 recommended options taken |
| 10 | Live viewer manifest + data tallied by script | Live is r019, not the r020 in the checkout; every headline number matched |
| 11 | Note drafted, three unsupported claims removed, build checked | Draft at `_posts/2026-10-12-…revisited.md` |
| 12 | Context guard at ~152k; handoff written | Audit synthesis left for a fresh session |

Session up to this log: 76 calls, 6.70M cache read, 207k cache write, 59k output, 1.38M cost units; context 37k → 164k.

Quota: claude-7d 11% → 10% (pace 0.7×, resets in ~23 h), codex-7d 99% → 95%, codex-5h 100% → 75%, cursor-auto 61% → 61% (3.0×).

## What worked

- **Fact sheet with a privacy section before public writing.** The research brief asked for sources per fact, a second count for each headline number, and a list of what must not go public. Codex found a real conflict (CHANGELOG "no PD flips" vs a 778 → 777 change from an identity re-key) and flagged unpublished paper plans, teaching plans, quota figures, a private repo and an old GitHub handle. The note avoided all of them without another round.
- **Checking what is deployed, not what is checked out.** The README in the local checkout describes r020 (open PR); the live viewer serves r019. A note quoting the README would have described an unreleased feature. One `curl` of the viewer's `manifest.json`, then a 30-line tally script over the live JSON, confirmed every number in the note.
- **Pickers with numbers.** One AskUserQuestion with measured figures in each option settled angle, agent disclosure and release scope in one reply.
- **Orchestrator-made screenshots for visual delegates.** Delegates cannot run a browser reliably (Codex's Firefox crashed). A script that builds the site, pins `data-chrome`/`data-theme` in copied HTML (theme-init stripped) and shoots every variant gave both auditors the same evidence.
- **Detached audits plus one `delegate wait`.** Two read-only audits ran while the note research and writing went on; no polling.

## Friction

1. **Ten calls to not reproduce a vague visual complaint.** "I don't see tables properly" was tested in two engines, two widths, two themes and on the live URL before asking. The issue was taste, not a bug: faint row rules and no column lines.
   - **Lesson:** when the first screenshot looks correct, show it and ask what is missing before widening the test matrix.
2. **Delegating a six-line CSS change cost more than doing it.** Brief, launch, verify, screenshots and apply took about eight orchestrator calls for +4/−2. The user asked for Codex, so it was right here, but inline would have been about three calls.
   - **Lesson:** for changes under ~10 lines, do them inline unless the user asks to delegate; delegate the verification work (screenshots) instead, if anything.
3. **Dark mode could not be pinned by attribute alone.** `data-theme="dark"` on `<html>` was overridden because `theme.js` re-reads `prefers-color-scheme`; the screenshot came out light.
   - **Lesson:** for headless Firefox dark mode, set `user_pref("ui.systemUsesDarkTheme", 1)` in a `user.js` of a dedicated profile, in addition to any attribute.
4. **Headless capture artefacts.** `firefox --screenshot` captures the window, not the page; tall windows plus a `min-height: 100vh` layout leave a blank band above the footer, and 404 came out at 8,192 px. The briefs had to explain both.
   - **Lesson:** state capture artefacts in the brief, or measure document height first and size the window to it.
5. **A substring count gave a wrong cross-check.** Counting IMSLP rights flags with `'eu' in flag` matched `pd_eu_rost` as well as `nonpd_eu`: 252 instead of the README's 65. Exact-value matching reproduced 65 and 4,527.
   - **Lesson:** tally categorical flags by exact value; when a check disagrees with the source, inspect the value list before trusting either.
6. **`pkill -f <pattern>` killed the calling shell** (exit 144, twice), because the pattern appeared in its own command line.
   - **Lesson:** stop a local server with `fuser -k PORT/tcp` or by saved PID.
7. **Brave headless wrote nothing without `--no-sandbox`** and gave no error on the first try.
8. **A 40 KB fact sheet was read in full** (~10k tokens). It was worth it here, but a cap (≤15 KB, sections on request) would have done for a 1,000-word note.
9. **Three items in one session.** The guard fired at ~152k as the audits returned; the 28 KB of reports were left for a fresh session rather than read at 164k per call.
