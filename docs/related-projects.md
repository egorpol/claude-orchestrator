# Related projects

Combines the 2026-10-09 survey, its 2026-10-13 refresh and a 2026-10-13 wide survey (Codex gpt-6.1-sol @xhigh with live web search). Stars are as observed on 2026-10-13, and a date next to them is the last push. For the first two, stars, licences and dates come from the GitHub API, and the first 12 KB of each new entry's README was read. Wide-survey entries take them from that survey's `gh search` table or the repo page, and their capabilities are source or documentation findings, not local installs.

## Worth adopting or borrowing from

### [openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc)
34.1k★ · Apache-2.0 · last push 2026-07-08 · official, by OpenAI

A Claude Code plugin that wraps the Codex app-server:

- `/codex:review` and `/codex:adversarial-review` are read-only, with a JSON output schema; the adversarial review is steerable.
- `/codex:rescue` delegates a task (background, resume, `--model`/`--effort`).
- `/codex:transfer` turns the current Claude session into a resumable Codex thread.
- `/codex:status`, `/codex:result` and `/codex:cancel` manage background jobs.
- An optional review gate is a `Stop` hook that blocks until a Codex review passes. It can loop and drain quota.

- **Overlap with `delegate`:** `rescue` works in **your current checkout**, with no worktree isolation, and the plugin is Codex-only, with no Cursor support and no quota routing. Keep `delegate launch` for writes.
- **Worth taking:**
  - The review commands, as is: `/plugin marketplace add openai/codex-plugin-cc`.
  - Its prompting skill (`skills/gpt-5-4-prompting`), which structures briefs as XML-tagged contracts (`<task>`, `<structured_output_contract>`, `<default_follow_through_policy>`, `<verification_loop>`). It also says not to mix unrelated jobs in one run.
  - `schemas/review-output.schema.json`, which could pair with `codex exec --output-schema` for machine-readable reviews. Not taken (2026-10-30); see the open list below.
- **Dated:** its defaults reference gpt-5.4.

### [obra/superpowers](https://github.com/obra/superpowers)
298k★ · MIT · last push 2026-10-10 · active

A methodology-as-skills framework: `subagent-driven-development`, `dispatching-parallel-agents`, `using-git-worktrees`, `requesting-code-review`, `verification-before-completion`, `writing-plans`, `systematic-debugging`.

- **Patterns worth taking:**
  - Two-stage review per task: spec compliance first, then code quality.
  - **Fix rounds:** resume the same implementer for rounds 1-3, then switch to a fresh implementer on a more capable model from round 4.
  - A ledger of rulings: `Ruling: <decision> — <why> — <cost if wrong>`, so the orchestrator decides instead of stalling.
  - A final whole-branch review.
- **Caveat:** it is built around Claude's own subagents, and its skills trigger broadly. Installing it wholesale would overlap with `delegate`; borrow the patterns instead.

### [wshobson/agents](https://github.com/wshobson/agents)
40.5k★ · MIT · last push 2026-10-12 · active

A plugin marketplace whose agents and skills install into Claude Code, Codex, Cursor, OpenCode and others. It could give the Codex and Cursor delegates the same specialist reviewers Claude uses. Medium relevance.

## New since the first survey

Entries below were not linked in the 2026-10-09 doc (refresh through `claude-delegator`; wide-survey additions from Claude Octopus onward).

### [cobusgreyling/loop-engineering](https://github.com/cobusgreyling/loop-engineering)
11.5k★ · MIT · last push 2026-10-12

- Pattern library and CLI (`loop init`, `loop doctor`, `loop-audit`, `loop-cost`) for recurring agent loops around a repo — triage, PR babysitting, CI sweeps — with Claude, Codex, Grok or OpenCode as the worker tool.
- **Overlap:** both treat verify-and-persist as part of the system; `delegate` is one-shot worktrees from inside Claude Code, not a scheduled outer loop.
- **Worth borrowing:** scored loop readiness and cost tooling (`loop-cost`) to audit how expensive repeated delegate/review cycles are on a repo.

### [omnigent-ai/omnigent](https://github.com/omnigent-ai/omnigent)
10.8k★ · Apache-2.0 · last push 2026-10-13

- Open-source meta-harness: one server supervises Claude Code, Codex, Cursor, OpenCode, Pi and YAML-defined agents; syncs sessions across terminal, browser, phone and desktop; optional cloud sandboxes.
- **Overlap:** multi-harness orchestration and cross-agent review; Omnigent is a standalone server and GUI stack, not a Claude Code skill with per-task git worktrees.
- **Worth borrowing:** server-wide policies that pause for approval, cap spend, or limit tools before risky actions.

### [SeemSeam/claude_codex_bridge](https://github.com/SeemSeam/claude_codex_bridge)
3.6k★ · NOASSERTION · last push 2026-10-10

- TUI hub for many CLI agents (Codex, Claude, Cursor, Copilot) with inter-agent queues and mobile remote.
- **Overlap:** multi-vendor CLIs in parallel; no quota routing or brief/verify contract.
- **Worth borrowing:** message-queue graphs (`A → B → C`) if agents ever chain in one item.

### [happier-dev/happier](https://github.com/happier-dev/happier)
1.9k★ · MIT · last push 2026-10-12

- Self-hostable control room for 100+ coding agents; cross-agent subagents (e.g. Claude asking Codex for review).
- **Overlap:** cross-agent delegation; targets many concurrent sessions, not one patch to merge.
- **Worth borrowing:** notify the user away from the desk while `delegate wait` runs.

### [zhnt/loushang](https://github.com/zhnt/loushang)
1.7k★ · Apache-2.0 · last push 2026-10-12

- Method-native harness with tool governance and provider routing; its own CLI, not Claude→external CLI delegation.
- **Worth borrowing:** methods and acceptance as runtime objects (early; `delegate` already has `--verify`).

### [skills-directory/skill-codex](https://github.com/skills-directory/skill-codex)
1.5k★ · MIT · last push 2026-09-13

- Claude Code skill wrapping `codex exec`/resume; stderr suppressed so thinking tokens do not bloat context.
- **Overlap:** Codex-only; no Cursor, worktrees, or cross-vendor review.
- **Worth borrowing:** default `2>/dev/null` on Codex stderr when only the summary returns.

### [awslabs/cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator)
1.4k★ · Apache-2.0 · last push 2026-10-12

- Local `cao-server` starts provider CLIs in isolated tmux sessions for a human supervisor.
- **Overlap:** multi-CLI coordination; tmux/UI, not background worktrees with an exit packet.
- **Worth borrowing:** per-provider setup docs in-repo.

### [kbwo/ccmanager](https://github.com/kbwo/ccmanager)
1.3k★ · MIT · last push 2026-10-12

- Session manager across worktrees/repos; copies Claude session data; `.worktreeinclude` for gitignored files.
- **Overlap:** worktree parallelism; interactive multiplexing, not brief/review/apply.
- **Worth borrowing:** nothing now; `.worktreeinclude` was dropped on 2026-10-28 because `delegate launch --link` already brings gitignored paths into the worktree.

### [jarrodwatts/claude-delegator](https://github.com/jarrodwatts/claude-delegator)
1.0k★ · MIT · last push 2026-10-12 · archived

- Claude plugin routing to GPT/Gemini experts via MCP; Codex path broken after MCP removal; points at `openai/codex-plugin-cc`.
- **Worth borrowing:** nothing new; historical expert-routing precedent.

### [nyldn/claude-octopus](https://github.com/nyldn/claude-octopus)
4.2k★ · last push 2026-10-11 · wide survey 2026-10-13

- Claude Code plugin that dispatches external CLI providers with distinct roles, adversarial review, and consensus gates (includes Cursor).
- **Overlap:** closest larger skill/plugin alternative to `delegate`.
- **Worth borrowing:** bounded disagreement resolution and provider-contribution reporting.

### [majiayu000/harness](https://github.com/majiayu000/harness)
84★ · last push 2026-10-11 · wide survey 2026-10-13

- Rust fleet control plane: agent lifecycles, independent reviews, execution policies, queues, observability around Claude/Codex.
- **Overlap:** review and governance; heavier than a Claude skill.
- **Worth borrowing:** enforce reviewer independence in code.

macOS menu-bar quota widgets (e.g. [tddworks/ClaudeBar](https://github.com/tddworks/ClaudeBar), 1.5k★) track reset windows for Claude, Codex, Cursor and others; little use on Linux. For a Linux quota CLI see [Quotio](https://github.com/nguyenphutrong/quotio/blob/master/apps/cli/README.md) (4.9k★, last push 2026-10-12): versioned JSON snapshots and a loopback REST API.

## Full orchestrators (alternatives, heavier)

These are interactive supervisors, mostly GUI or tmux based, where a human watches many agents. `delegate` has a different shape: Claude orchestrates from inside a session. They are worth a look if you want a dashboard over many concurrent runs.

| repo | ★ | license | notes |
|---|---|---|---|
| [ruvnet/ruflo](https://github.com/ruvnet/ruflo) | 74.5k | — | formerly claude-flow; Claude/Codex swarms, memory, MCP, routing, policy (last push 2026-10-13) |
| [Yeachan-Heo/oh-my-claudecode](https://github.com/Yeachan-Heo/oh-my-claudecode) | 39.8k | MIT | "teams-first" multi-agent orchestration for Claude Code; Codex and Cursor workers in tmux panes |
| [BloopAI/vibe-kanban](https://github.com/BloopAI/vibe-kanban) | 28.3k | Apache-2.0 | kanban board of agent tasks, worktree per task |
| [superset-sh/superset](https://github.com/superset-sh/superset) | 15.1k | other | agentic IDE, many agents in parallel |
| [OrchestratorInc/agent-orchestrator](https://github.com/OrchestratorInc/agent-orchestrator) | 13.1k | Apache-2.0 | desktop/web/CLI; every session gets its own worktree, branch and PR; 25+ harnesses including Cursor |
| [smtg-ai/claude-squad](https://github.com/smtg-ai/claude-squad) | 8.6k | AGPL-3.0 | tmux + worktrees for Claude Code, Codex, OpenCode, Amp |
| [standardagents/dmux](https://github.com/standardagents/dmux) | 1.8k | MIT | worktree agent multiplexer |
| [johannesjo/parallel-code](https://github.com/johannesjo/parallel-code) | 1.0k | MIT | Claude Code, Codex and Gemini side by side in worktrees (no Cursor) |
| [Enderfga/claw-orchestrator](https://github.com/Enderfga/claw-orchestrator) | 0.6k | MIT | one runtime for Claude Code, Codex, Cursor Agent and more; "a run only counts as done when checks pass" |
| [LilithGames/a2wave](https://github.com/LilithGames/a2wave) | 27 | — | governed services over agent CLIs; queues, A2A, isolated eval worktrees (last push 2026-10-13) |

## Model routing and bridges (not needed now)

- [musistudio/claude-code-router](https://github.com/musistudio/claude-code-router) (37.6k★, MIT): points Claude Code itself at other models. This replaces the orchestrator's model; it does not delegate.
- [router-for-me/CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (55.0k★, last push 2026-10-13): subscription-backed providers through compatible APIs; changes transport rather than delegating CLI work.
- [kaitranntt/ccs](https://github.com/kaitranntt/ccs) (2.9k★, last push 2026-10-01): provider profiles, isolated accounts, quota visibility, CLIProxy account-pool routing (fill-first plus affinity).
- [BeehiveInnovations/pal-mcp-server](https://github.com/BeehiveInnovations/pal-mcp-server) (11.8k★; the former zen-mcp-server): MCP tools for multi-model consensus and a bridge to Codex/Gemini CLIs. Its last push was 2025-12, so it looks stale.
- cursor-agent bridges: [raphaelluethy/cursor-acp](https://github.com/raphaelluethy/cursor-acp) (ACP client), [Azhi-ss/cursorcli2api](https://github.com/Azhi-ss/cursorcli2api) (HTTP API shim).
- [coder/agentapi](https://github.com/coder/agentapi) is archived.

## Quota-aware routing

Routers that read live subscription headroom before choosing a worker or account. No production-ready selector covering Claude, Codex, Cursor and Gemini together was verified (wide survey 2026-10-13).

- [wr/quota-router](https://github.com/wr/quota-router) (0★, retained): injects Claude/Codex quota snapshots before Agent calls; rejects expired snapshots; reads Codex rollout rate-limit events without launching Codex. **Take:** treat stale quota as unknown.
- [IuCC123/CLIProxyAPI-Rust](https://github.com/IuCC123/CLIProxyAPI-Rust) (35★): routes API sessions by subscription headroom, renewal, weekly allowance, concurrent load and session affinity. **Take:** keep sessions sticky to preserve caches.
- [Quotio](https://github.com/nguyenphutrong/quotio/blob/master/apps/cli/README.md) (4.9k★): Linux CLI with versioned JSON snapshots; its beta Claude/Gemini/Cursor catalog routes have offline test coverage; live acceptance is unverified. **Take:** explicit confidence, freshness and schema versions.

## Observability

- [disler/claude-code-hooks-multi-agent-observability](https://github.com/disler/claude-code-hooks-multi-agent-observability) (1.5k★, last push 2026-02)
- [donvito/agent-monitor](https://github.com/donvito/agent-monitor) (83★): traces, tokens and cost for Codex and Claude Code subagents
- [junyeong-ai/hatel](https://github.com/junyeong-ai/hatel/blob/main/README.en.md) (0★, retained): joins native OTel and lifecycle hooks into local session/project/agent-type usage. **Take:** respect cumulative versus delta metric temporality.

## Measurement and evaluation

### ccusage family

Use [ccusage](https://github.com/ccusage/ccusage) ([v20.0.30](https://github.com/ccusage/ccusage/releases/tag/v20.0.30), 19.0k★ on 2026-10-13) as the pinned offline reference for Claude session totals including subagents: it maps `<session>/subagents/*.jsonl` to the parent, dedupes requestless `msg_…` IDs across timestamps (fixed in v20.0.27; v20.0.26 can double-count), and prices 5-minute vs 1-hour cache writes separately. Read `totalCost`, not the sum of `entries[].costUSD`. External Codex/Cursor delegates still need the run ledger.

```bash
ccusage claude session --id <UUID> --json --mode calculate --offline
```

- [Piebald-AI/splitrail](https://github.com/Piebald-AI/splitrail): reads subagents but identifies conversations by transcript-path hash (no parent attribution); no TTL pricing.
- [cobra91/better-ccusage](https://github.com/cobra91/better-ccusage): reads subagents as separate filename sessions; no parent rollup; no TTL pricing.

### Claude Code OpenTelemetry

Anthropic's [monitoring docs](https://code.claude.com/docs/en/monitoring-usage#available-metrics-and-events): `claude_code.cost.usage` (USD); `claude_code.token.usage` (`input`, `output`, `cacheRead`, `cacheCreation`; input excludes cache). `api_request` adds estimated cost, four token counts, model, duration, request ID and query source. Cost and token metrics carry `query_source=main|subagent|auxiliary` plus agent, skill, plugin and MCP names. [Beta traces](https://code.claude.com/docs/en/monitoring-usage#span-attributes) nest subagents (`agent_id`/`parent_agent_id`). [`subagent_completed.total_tokens`](https://code.claude.com/docs/en/monitoring-usage#subagent-completed-event) is final-request only. No cache-TTL split in standard metrics; use transcripts for TTL. VS Code `app.entrypoint=claude-vscode` is [documented](https://code.claude.com/docs/en/vs-code#configure-settings); Cursor-hosted export is unverified.

### Controlled comparisons

**[Evaluating AGENTS.md](https://arxiv.org/html/2602.11988v1).** Ablation on 300 SWE-bench Lite + 138 AGENTbench; resolution, steps, cost. Generated context +20–23% cost, lower resolution. Copy matched repo state and independent tests; extra standing instructions are not free wins.

**[SkillsBench](https://arxiv.org/html/2602.12670v1)** ([repo](https://github.com/benchflow-ai/skillsbench), 1.8k★). 84 tasks, seven configs, 7,308 trajectories, deterministic verifiers. Curated skills +16.2 pp; self-generated negligible. Copy repeats, hidden verifiers, leakage checks; measure activation (paper has internal count inconsistencies).

**[Analyzing and Mitigating Cost-Inefficient Behaviors](https://arxiv.org/html/2609.30725v1).** 1,200 trajectories diagnosed; 200 Verified + 100 Pro held out, three baseline reps. Skills cut cost 7.88–41.73% in 6/8 settings. Copy held-out split and cost-per-success; do not treat its best % as expected saving.

**[Harness or Model?](https://arxiv.org/html/2609.11987v1).** Same-model native vs neutral: 80 tasks/contrast, 792/800 graded. Neither average solve-rate contrast established an advantage; corrected observed-usage costs favoured native harnesses, but billed ordering stays unresolved without Anthropic usage. Copy raw events and separate success/completion; avoid one usage normalizer (cache semantics broke their first cost analysis).

**[Dealwatch three-arm benchmark](https://github.com/Dealwatch/claude-code-orchestration-benchmark)** (0★, retained). Three arms × two bundles, one run/cell; Opus orchestration $5.79 vs solo $6.15 within 29% pilot noise; configured workers ran 0× in ten main runs. Copy fixture self-tests, denied Agent tools in solo, anonymized reviews, realized-delegation checks. ~10 sessions/arm is a pilot.

**[Reveneau’s token benchmark](https://reveneau.com/guides/claude-code-token-benchmark).** 45 runs (9×5) on a 30-line task. Extra 301-line CLAUDE.md: median $0.161→$0.239. Copy fresh fixtures and external checks; do not extrapolate to long sessions.

**[Context Mode evidence](https://context-mode.com/benchmarks/data/v4-opus55-W1-W2-W3-W4.json).** Alternating A/B; its multi-agent workload: 20 pairs, −15.2% cost. Copy frozen deployments. Avoid its policy: the [index](https://context-mode.com/benchmarks/data/index.json) withholds non-winners and this set has zero per-call tokens.

**[METR early-2025 study](https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/).** 16 developers, 246 issues, randomised to AI allowed or disallowed; elapsed time +19% with AI. Copy assignment before work. A [later update](https://metr.org/blog/2026-02-24-uplift-update/) flags selection bias and bad timing for concurrent agents.

**[Claude Code `plugin eval`](https://code.claude.com/docs/en/plugin-evals#score-against-the-no-plugin-baseline).** `--ablation with-without`; default 3 runs/arm; grader Δ and cost. Use for bounded skill/rule checks; assert outcomes and activation.

## Lists to browse

[hesreallyhim/awesome-claude-code](https://github.com/hesreallyhim/awesome-claude-code) (55.5k★) · [VoltAgent/awesome-claude-code-subagents](https://github.com/VoltAgent/awesome-claude-code-subagents) (25.7k★) · [andyrewlee/awesome-agent-orchestrators](https://github.com/andyrewlee/awesome-agent-orchestrators) (2.2k★) · [bradAGI/awesome-cli-coding-agents](https://github.com/bradAGI/awesome-cli-coding-agents) (1.3k★) · [anthropics/skills](https://github.com/anthropics/skills) (181k★, official)

## Improvements drawn from the above

Done on 2026-10-09:
1. `delegate followup NAME --brief F | --from-review N [--fresh --model M]`: the next round runs in the same worktree and resumes the agent session (superpowers' fix-round policy; `--fresh` is the escalation path).
2. `delegate review NAME`: a read-only, cross-vendor review that checks against every round's brief, records a verdict, findings and spec compliance, and keeps before/after diff digests that prove it did not modify anything.
3. `--verify CMD` on launch and followup, plus `delegate verify NAME`. Checks run after the agent exits, so "done" means the checks passed (claw-orchestrator's idea).
4. The brief guidance in SKILL.md now includes the Codex plugin's tagged-contract advice.
5. `openai/codex-plugin-cc` is installed (user scope); the review gate is left off.

Done on 2026-10-13 (wide-survey recommendations):
1. `7bc654a`: `delegate cost --session` prices at list price including subagent transcripts (`<session>/subagents/agent-*.jsonl`, deduped), with 5-minute and 1-hour cache writes priced separately; matches `ccusage@20.0.30` to 1e-6 on two sessions (`docs/verified-behaviour.md`). Covers survey recommendations 1 and 2.
2. `184bde7`: quota snapshots (`~/.cache/claude-delegate/quota.jsonl`) at session start/end, each run start, and from a timer; each reading has a `stale` flag. `delegate route` still uses openusage (can lag ~1 h, shown as DATA AGE); `--source direct` reads live. Recommendation 5 partly done.
3. `698e857`, `b705445`: per-session summary rows log delegate commands; `delegate cost --by-mode` counts launches/runs per arm; `delegate rate` is a blinded item rater. Covers recommendation 4.
4. Later, the rest of recommendation 5: `daf151a` (2026-10-27) leaves stale readings out of `delegate quota --unused`, and `b6603b2` (2026-10-28) re-reads a Codex pool over 15 min old with `--source direct` before a dispatch.

Still open:
- Machine-readable reviews via `codex exec --output-schema` (the plugin ships `review-output.schema.json`). Dropped 2026-10-30: the regex parse of the Markdown reviews held on all 88 recorded reviews with a last message (49 Codex, 39 Cursor): every verdict parsed, no review gave two, and each review's finding bullets matched its severity tags (239 findings); cursor-agent has no schema option, so a schema would cover Codex reviews only. Reopen as `followup --from-review N` with a chosen subset of findings if forwarding a review's wrong findings (about one per review) costs measurable rounds.
- Loop cost / readiness scoring for repeated delegate cycles (`cobusgreyling/loop-engineering`, `loop-cost` / `loop-audit`).
- Optional pre-launch spend or approval policies before destructive delegate actions (`omnigent-ai/omnigent`).
- Carry gitignored env files into new worktrees via a `.worktreeinclude` convention (`kbwo/ccmanager`). Dropped 2026-10-28: `delegate launch --link` covers it.
- Control-session design is decided and [preregistered](preregistration.md) (2026-10-30): per-session 70/15/15 orchestrator / claude-only / vanilla since 2026-10-26, intent-to-treat, metric = USD per finished item with a bootstrap interval. Blocked assignment was not built: the interval comes from a bootstrap that resamples each arm across projects, with whole project-days as a sensitivity (2026-10-30, Codex and Cursor agreed). Of recommendation 3, regressions (not measured) and elapsed time (exploratory) remain open.
