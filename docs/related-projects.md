# Related projects

Survey from 2026-10-09, refreshed on 2026-10-13. Star counts, licences and last-push dates come from the GitHub API on 2026-10-13. New entries came from eight GitHub searches; for each of them, the first 12 KB of its README was read.

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
  - `schemas/review-output.schema.json`, which could pair with `codex exec --output-schema` for machine-readable reviews.
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

Repos below were not linked in the 2026-10-09 doc. They are the closest matches from eight GitHub searches (pushed after 2026-07-01), capped at ten.

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

- TUI “CCB” hub: many CLI agents (including Codex, Claude, Cursor, Copilot) with stable inter-agent message queues, daemon-backed state, and mobile remote control.
- **Overlap:** multi-vendor coding CLIs in parallel; no quota-aware routing or orchestrator-side brief/verify contract like `delegate`.
- **Worth borrowing:** explicit message-queue graphs (`A → B → C`) if `delegate` ever chains agents in one item; nothing new for worktree isolation.

### [happier-dev/happier](https://github.com/happier-dev/happier)
1.9k★ · MIT · last push 2026-10-12

- Self-hostable desktop, web and mobile control room for 100+ coding agents; sessions can spawn subagents across agents (e.g. Claude asking Codex for review); end-to-end encrypted.
- **Overlap:** cross-agent delegation and review; Happier targets many concurrent sessions on your machines, not Claude-as-orchestrator with a single patch to merge.
- **Worth borrowing:** nothing new for the CLI skill shape; optional inspiration for notifying the user away from the desk while `delegate wait` runs.

### [zhnt/loushang](https://github.com/zhnt/loushang)
1.7k★ · Apache-2.0 · last push 2026-10-12

- Python “method-native” harness (`loushang code`): persistent sessions, tool governance, provider routing across many LLM backends, method-guided delivery.
- **Overlap:** routing and verified delivery; Loushang is its own CLI/workbench, not delegation from Claude Code to external CLIs.
- **Worth borrowing:** treating methods and acceptance as runtime objects; early for `delegate`, which already encodes verify in `--verify`.

### [tddworks/ClaudeBar](https://github.com/tddworks/ClaudeBar)
1.5k★ · Apache-2.0 · last push 2026-10-12

- macOS menu bar app tracking quota and reset windows for Claude, Codex, Cursor and many other coding providers, with alerts and optional session hooks.
- **Overlap:** quota visibility; `delegate quota` uses openusage on the CLI and drives routing, not a menu bar.
- **Worth borrowing:** low-quota notifications before starting a long `--verify` run; no change to routing math.

### [skills-directory/skill-codex](https://github.com/skills-directory/skill-codex)
1.5k★ · MIT · last push 2026-09-13

- Claude Code plugin/skill that wraps `codex exec` and session resume, with model and sandbox prompts and stderr suppressed by default so thinking tokens do not bloat context.
- **Overlap:** Codex-only delegation from Claude Code; no Cursor, worktrees, or cross-vendor review.
- **Worth borrowing:** default `2>/dev/null` on Codex stderr when only the summary should return to the orchestrator (plugin README).

### [awslabs/cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator)
1.4k★ · Apache-2.0 · last push 2026-10-12

- “CAO”: local `cao-server` starts provider CLIs (Claude Code, Codex, Cursor, Kiro, Copilot, OpenCode, and others) in isolated tmux sessions for a human supervisor.
- **Overlap:** multi-CLI coordination; CAO is tmux-first with a supervisor UI, not background worktrees launched from Claude with an exit packet.
- **Worth borrowing:** per-provider setup docs in-repo (Cursor CLI guide pattern); nothing new for quota routing.

### [kbwo/ccmanager](https://github.com/kbwo/ccmanager)
1.3k★ · MIT · last push 2026-10-12

- Terminal session manager for Claude Code, Codex, Cursor Agent and others across git worktrees and multiple repos; copies Claude session data between worktrees; `.worktreeinclude` for gitignored files in new worktrees.
- **Overlap:** worktree-per-agent parallelism; CCManager is interactive multiplexing, not brief/review/apply from the orchestrator.
- **Worth borrowing:** `.worktreeinclude` as a complement to `delegate launch --link` for env files the brief assumes exist.

### [jarrodwatts/claude-delegator](https://github.com/jarrodwatts/claude-delegator)
1.0k★ · MIT · last push 2026-10-12 · archived

- Claude Code plugin routing to GPT/Gemini experts via MCP; five specialist roles with read-only or write modes. README states Codex MCP removal broke Codex delegation; points users at `openai/codex-plugin-cc`.
- **Overlap:** Claude delegates outward to another vendor; no Cursor, worktrees, or structured review rounds.
- **Worth borrowing:** nothing new; historical precedent for expert routing before the official Codex plugin.

## Full orchestrators (alternatives, heavier)

These are interactive supervisors, mostly GUI or tmux based, where a human watches many agents. `delegate` has a different shape: Claude orchestrates from inside a session. They are worth a look if you want a dashboard over many concurrent runs.

| repo | ★ | license | notes |
|---|---|---|---|
| [Yeachan-Heo/oh-my-claudecode](https://github.com/Yeachan-Heo/oh-my-claudecode) | 39.8k | MIT | "teams-first" multi-agent orchestration for Claude Code; Codex and Cursor workers in tmux panes |
| [BloopAI/vibe-kanban](https://github.com/BloopAI/vibe-kanban) | 28.3k | Apache-2.0 | kanban board of agent tasks, worktree per task |
| [superset-sh/superset](https://github.com/superset-sh/superset) | 15.1k | other | agentic IDE, many agents in parallel |
| [OrchestratorInc/agent-orchestrator](https://github.com/OrchestratorInc/agent-orchestrator) | 13.1k | Apache-2.0 | desktop/web/CLI; every session gets its own worktree, branch and PR; 25+ harnesses including Cursor |
| [smtg-ai/claude-squad](https://github.com/smtg-ai/claude-squad) | 8.6k | AGPL-3.0 | tmux + worktrees for Claude Code, Codex, OpenCode, Amp |
| [standardagents/dmux](https://github.com/standardagents/dmux) | 1.8k | MIT | worktree agent multiplexer |
| [johannesjo/parallel-code](https://github.com/johannesjo/parallel-code) | 1.0k | MIT | Claude Code, Codex and Gemini side by side in worktrees (no Cursor) |
| [Enderfga/claw-orchestrator](https://github.com/Enderfga/claw-orchestrator) | 0.6k | MIT | one runtime for Claude Code, Codex, Cursor Agent and more; "a run only counts as done when checks pass" |

## Model routing and bridges (not needed now)

- [musistudio/claude-code-router](https://github.com/musistudio/claude-code-router) (37.6k★, MIT): points Claude Code itself at other models. This replaces the orchestrator's model; it does not delegate.
- [BeehiveInnovations/pal-mcp-server](https://github.com/BeehiveInnovations/pal-mcp-server) (11.8k★; the former zen-mcp-server): MCP tools for multi-model consensus and a bridge to Codex/Gemini CLIs. Its last push was 2025-12, so it looks stale.
- cursor-agent bridges: [raphaelluethy/cursor-acp](https://github.com/raphaelluethy/cursor-acp) (ACP client), [Azhi-ss/cursorcli2api](https://github.com/Azhi-ss/cursorcli2api) (HTTP API shim).
- [coder/agentapi](https://github.com/coder/agentapi) is archived.

## Observability

- [disler/claude-code-hooks-multi-agent-observability](https://github.com/disler/claude-code-hooks-multi-agent-observability) (1.5k★, last push 2026-02)
- [donvito/agent-monitor](https://github.com/donvito/agent-monitor) (83★): traces, tokens and cost for Codex and Claude Code subagents

## Lists to browse

[hesreallyhim/awesome-claude-code](https://github.com/hesreallyhim/awesome-claude-code) (55.5k★) · [VoltAgent/awesome-claude-code-subagents](https://github.com/VoltAgent/awesome-claude-code-subagents) (25.7k★) · [andyrewlee/awesome-agent-orchestrators](https://github.com/andyrewlee/awesome-agent-orchestrators) (2.2k★) · [bradAGI/awesome-cli-coding-agents](https://github.com/bradAGI/awesome-cli-coding-agents) (1.3k★) · [anthropics/skills](https://github.com/anthropics/skills) (181k★, official)

## Improvements drawn from the above

Done on 2026-10-09:
1. `delegate followup NAME --brief F | --from-review N [--fresh --model M]`: the next round runs in the same worktree and resumes the agent session (superpowers' fix-round policy; `--fresh` is the escalation path).
2. `delegate review NAME`: a read-only, cross-vendor review that checks against every round's brief, records a verdict, findings and spec compliance, and keeps before/after diff digests that prove it did not modify anything.
3. `--verify CMD` on launch and followup, plus `delegate verify NAME`. Checks run after the agent exits, so "done" means the checks passed (claw-orchestrator's idea).
4. The brief guidance in SKILL.md now includes the Codex plugin's tagged-contract advice.
5. `openai/codex-plugin-cc` is installed (user scope); the review gate is left off.

Still open:
- Machine-readable reviews via `codex exec --output-schema` (the plugin ships `review-output.schema.json`).
- Loop cost / readiness scoring for repeated delegate cycles (`cobusgreyling/loop-engineering`, `loop-cost` / `loop-audit`).
- Optional pre-launch spend or approval policies before destructive delegate actions (`omnigent-ai/omnigent`).
- Carry gitignored env files into new worktrees via a `.worktreeinclude` convention (`kbwo/ccmanager`).
