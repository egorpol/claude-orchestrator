# Preregistration: control-session experiment, Max period

Registered on 2026-10-30, the date of this file's first commit in the public repo. Everything above **Amendments** stays as committed; later changes go into that section with a date and a reason.

## Status at registration

- **Period 1** (2026-10-13 to 10-26, weights 50/25/25, Pro plan) is published in [field-results.md](field-results.md#2026-10-13-to-10-20-control-session-arms-first-numbers): USD per finished item 5.30 / 5.76 / 5.09 (orchestrator / claude-only / vanilla) on 21 / 9 / 7 finished items, too few to compare. It stays exploratory.
- **Period 2** began when the weights changed to 70/15/15 at 2026-10-26 17:20:51 UTC, after Claude moved to the Max plan (13:25–17:01 UTC). Up to the last start before this file was written (2026-10-30 04:30 UTC) it held 214 drawn sessions in 10 projects plus 2 outside any git repo; 11 were ghosts. None is rated, and no arm comparison has been run on them: a search of all Claude Code transcripts finds no `delegate cost --by-mode` run after the weight change. The field reports folded in on 2026-10-30 give per-session costs for 14 period-2 sessions, not split or compared by arm.
- **Allocation, seen before registration:** the 214 draws under 70/15/15, ghosts included, came out 141 / 48 / 25 against 149.8 / 32.1 / 32.1 expected (χ² p ≈ 0.007). Every recorded mode follows from its `u` and weights, so the excess lies in where `u` fell; the `u` values are all distinct and pass a KS test against uniform (D 0.058, critical value 0.093). No mechanism was found; the draw code is unchanged.
- **Design questions** (window, blocking, treatment drift, primary outcome) went to Codex and Cursor before registration. Both picked the options adopted below on all four, and their risk notes are folded in.

## Question and hypotheses

Does handing work to Codex and Cursor lower what Claude spends per finished piece of work, compared with the same session rules without delegation?

- **H1 (primary):** orchestrator sessions cost less list-price Claude USD per finished item than claude-only sessions.
- **H2 (secondary, no direction claimed):** vanilla against claude-only, the effect of the injected rules.

## Design

- **Arms:**
  - `orchestrator`: rules plus delegation;
  - `claude-only`: the same hygiene rules, with `delegate launch`, `followup`, `review` and `ask` denied;
  - `vanilla`: no injected rules, the same denials.

  In every arm, the context guard still asks for a handoff at large context; its delegate nudge runs in orchestrator sessions only. `hooks/session-mode.py` draws each new session (startup or `/clear`) with `random.Random()` seeded from the OS, weights 70/15/15 from `~/.config/claude-orchestrator/session-mode.json`.
- **Blinding:** the rater is blind to the mode. The user and Claude see it, so the estimate includes any change in how the user works in each arm, such as clearing a session early.
- **Unit and eligibility:** a drawn session, i.e. its first start row has a `u` and is not forced, excluded or unassigned. The tool's own repo is excluded from the draw. Intent-to-treat: a session stays in the arm of its first start row, including early `/clear` re-rolls. Ghosts (a summary row with no transcript and no calls: the session ended before its first call, as in `delegate cost --by-mode`) add 0 USD and 0 items, so they leave the ratio unchanged; they are left out of per-session means. Ghosts and re-rolls are reported per arm.
- **Registration time T0:** the UTC committer timestamp of the first commit on `main` that adds this file. A session's period follows its first start row's `ts`; a resume or compact never moves it.
- **Run-in:** period-2 sessions whose first start is before T0 are analysed apart, as exploratory.
- **Confirmatory window:** first starts strictly after T0 and strictly before the close. The close is the earlier of 2026-11-30 00:00 UTC and the `ts` of the rating row that first rates the 60th distinct non-ghost claude-only session whose first start is after T0 and before that date, counted in log order. A session counts once, at its first rubric-v2 rating row; sessions that started before T0 never advance this count. Sessions that start before the close stay in, also when they end or are rated after it. Progress is checked from counts only (drawn, ghost and rated sessions per arm), never from costs or item statuses by arm. 60 is a precision target, not a power calculation: the null bootstrap below puts the 95% range of the ratio at 0.76–1.27 there.
- **No blocking:** assignment stays simple randomisation. Project imbalance between arms is part of the sampling error the bootstrap measures.
- **Treatment as maintained:** the repo keeps changing during the window, and since both arms with rules read them live, the estimand is assignment to each policy as maintained over the window, in claude-only as well as orchestrator. From T0, each start row also records `checkout` (HEAD of the hook's own checkout; null unless git's top level is that folder), `dirty` (whether `git status --porcelain` lists any change, untracked files included and ignored ones not), `context_sha256` (SHA-256 of the injected context without the spare-pool line, whose numbers change every few minutes; null for vanilla) and `spare` (that line as text). These are fingerprints, not snapshots: `checkout` names committed files only, a dirty tree can't be rebuilt, and the hash covers the injected text, which names `rules/reference.md` by path, not by content. Sessions with a dirty tree stay in under intent-to-treat; their count is reported per arm. Field reports on single sessions may still change the rules; nobody reads outcomes compared by arm before the freeze.

## Outcomes

- **Primary:** list-price Claude USD (main transcript plus subagents) from each session's latest summary row, which `delegate report` prices with `catalog.json` when the session ends, summed over the arm's analysis set (see Freeze), divided by that set's finished items under rubric v2. Field-report items are left out of the item counts; their USD stays in. Prices of models already in the catalog at T0 change only through an amendment. A session whose latest summary has `usd_unpriced_calls` above 0 or a null `usd_list_price` (none among the 353 sessions drawn to date) is left out of both sums and reported per arm.
- **Secondary:**
  - vanilla against claude-only on the primary measure;
  - finished share of items per arm;
  - the primary measure with unclear items counted as finished;
  - size-weighted finished items (S 1, M 3, L 8, XL 20);
  - estimated Claude 7d pool points per finished item, a linear transform of the primary: USD times the claude-7d rate of the first `cost --calibrate --save` run on or after 2026-11-02 00:00 UTC with `--since 2026-10-26T17:01:00Z`, so only Max-plan quota pairs count. The saved file's SHA-256 goes into Amendments before any by-arm look; without a usable fit, this measure is reported as unavailable;
  - Codex and Cursor tokens per orchestrator session, from the run log.
- **Descriptive only:** item type and size per arm, and the share of continuation items. The rater assigns these after the session, so the mode can affect them.
- **Exploratory, no code yet:** active minutes per finished item, from call timestamps with gaps over 15 minutes left out. Regressions are not measured.

## Analysis

- **Rating:** default `delegate rate` runs (no `--force`, no `--session`) with rubric v2 and Cursor Auto, unchanged, as sessions end. A run rates every eligible session that has no rubric-v2 rating or whose transcript now has more typed prompts than its latest rating covers. One run includes the rater's built-in validation retry; a failure leaves no row, so every later run tries that session again. A last default run starts at the freeze. Claude Code deletes transcripts after `cleanupPeriodDays`; on 2026-10-30, before T0, it was raised from the default 30 to 90 days on the machine that records the experiment, past the freeze.
- **Freeze:** outcomes are read 7 days after the close, after that last run, from each session's latest summary and rating rows. A rating is current when its `prompts` equals the number of typed prompts in the session's transcript then (`rate.typed_prompts`, the rater's own test), so a session resumed with new prompts whose re-rating failed has none. The analysis set is the window's drawn sessions with a summary (the hook back-fills one after an hour of quiet), not a ghost, without unpriced calls and with a current rating. Every other session is left out of both sums and reported per arm with its reason; missingness is reported, not modelled.
- **Estimate:** per arm, the ratio of sums above. H1 is R = (USD_o × F_c) / (USD_c × F_o), with USD and finished items F of orchestrator (o) and claude-only (c). A positive value over 0 is +∞; 0 over a positive value is 0.
- **Uncertainty:** 10,000 bootstrap resamples of the analysis set from one Python `random.Random(0)`. Each replicate visits the arms in sorted order; an arm with n sessions, sorted by first-start time in UTC and then session id, draws n of them with n successive `rng.randrange(n)` calls, across projects, so arm sizes stay fixed and the project mix varies as random allocation lets it. A drawn session brings its USD and finished items together, and R is recomputed from the sums. Each sensitivity that resamples starts from a fresh `random.Random(0)`. The 95% interval is the 250th and 9,750th of the 10,000 sorted replicates (nearest rank), with +∞ kept in the sort and its share reported. If the observed R or any replicate is 0/0, the interval is reported as [0, +∞], inconclusive. Project-level ratios are reported only for projects with rated sessions in both arms; the rest are listed as undefined. Project is the first start row's `repo_dir`, or its `cwd` outside a git repo.
- **Reading:** "orchestrator cheaper" if the interval's upper end is below 1.0, "more expensive" if its lower end is above 1.0, otherwise inconclusive. The interval is reported in every case.
- **Sensitivity:**
  - the unclear-as-finished and size-weighted variants;
  - resampling project-days instead of sessions, since sessions hand work on to each other through HANDOFF.md across arms: each replicate visits the projects in sorted order, lists the sorted UTC dates of the first starts of the project's analysis sessions, and draws d of its d dates with d successive `rng.randrange(d)` calls; each drawn date brings all of that project's analysis sessions with that date, in every arm. Handoffs across days stay a limitation;
  - leaving out continuation items and the USD that `rate.item_costs` (as of T0) assigns to them by call timestamps; field-report USD stays in. A session whose transcript is gone by then is left out of this variant and reported per arm;
  - leaving out sessions over 8 hours from first to last call;
  - before and after one commit inside the window: among first-parent commits on `main` with committer timestamps in the window, the one with the most added plus deleted lines under `rules/` and `hooks/` against its first parent (`git diff --numstat`; `tests/` doesn't count), ties going to the earliest timestamp, then the lower SHA. Sessions split by first start before its committer timestamp, or at or after it. Picked from git before any by-arm look; no split if no commit in the window changed those folders.
- **Allocation:** arm counts are reported against the weights; the per-arm ratios need no reweighting.
- **Code:** `delegate cost --by-mode` computes per-arm ratios today, but only as an exploratory view: it reopens transcripts and keeps the finished items of sessions whose USD is null or partly unpriced. The confirmatory code computes the primary from the frozen summary and rating rows, reading transcripts only to count typed prompts, with the exclusions above. The start-row fields land in the same commit as this file. That code, the window filter, the freeze and the bootstraps are to be added and committed as written here, with tests on fake logs, before any by-arm comparison.

## Null spread used for the window size

Resampling period 1's 46 rated sessions pooled across arms (USD 5.37 per finished item), with the orchestrator arm 70/15 times the claude-only arm, the 95% range of the orchestrator / claude-only ratio under no difference is 0.69–1.39 at 30 rated claude-only sessions, 0.76–1.27 at 60 and 0.81–1.22 at 90.

## Amendments

- **2026-10-31, registration date.** The first commit adding this file is `04ff5aa`, committed 2026-10-31 00:29:58 UTC, not 2026-10-30 as the opening line says (it was written the day before). T0 is that commit's timestamp, as Design defines it, so no rule changes.
