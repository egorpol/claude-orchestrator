# Related projects

Survey from 2026-10-09: GitHub search plus a web search, with stars and last push read via the GitHub API on that date. I read the READMEs and key files of the first two entries; for the rest I only read the descriptions.

## Worth adopting or borrowing from

### [openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc)
34.0k★ · Apache-2.0 · last push 2026-07-08 · official, by OpenAI

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
297k★ · MIT · active

A methodology-as-skills framework: `subagent-driven-development`, `dispatching-parallel-agents`, `using-git-worktrees`, `requesting-code-review`, `verification-before-completion`, `writing-plans`, `systematic-debugging`.

- **Patterns worth taking:**
  - Two-stage review per task: spec compliance first, then code quality.
  - **Fix rounds:** resume the same implementer for rounds 1-3, then switch to a fresh implementer on a more capable model from round 4.
  - A ledger of rulings: `Ruling: <decision> — <why> — <cost if wrong>`, so the orchestrator decides instead of stalling.
  - A final whole-branch review.
- **Caveat:** it is built around Claude's own subagents, and its skills trigger broadly. Installing it wholesale would overlap with `delegate`; borrow the patterns instead.

### [wshobson/agents](https://github.com/wshobson/agents)
40.3k★ · MIT · active

A plugin marketplace whose agents and skills install into Claude Code, Codex, Cursor, OpenCode and others. It could give the Codex and Cursor delegates the same specialist reviewers Claude uses. Medium relevance.

## Full orchestrators (alternatives, heavier)

These are interactive supervisors, mostly GUI or tmux based, where a human watches many agents. `delegate` has a different shape: Claude orchestrates from inside a session. They are worth a look if you want a dashboard over many concurrent runs.

| repo | ★ | license | notes |
|---|---|---|---|
| [Yeachan-Heo/oh-my-claudecode](https://github.com/Yeachan-Heo/oh-my-claudecode) | 39.7k | MIT | "teams-first" multi-agent orchestration for Claude Code; Codex and Cursor workers in tmux panes |
| [BloopAI/vibe-kanban](https://github.com/BloopAI/vibe-kanban) | 28.3k | Apache-2.0 | kanban board of agent tasks, worktree per task |
| [superset-sh/superset](https://github.com/superset-sh/superset) | 15.0k | other | agentic IDE, many agents in parallel |
| [OrchestratorInc/agent-orchestrator](https://github.com/ComposioHQ/agent-orchestrator) | 13.0k | Apache-2.0 | desktop/web/CLI; every session gets its own worktree, branch and PR; 25+ harnesses including Cursor |
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

[hesreallyhim/awesome-claude-code](https://github.com/hesreallyhim/awesome-claude-code) (55k★) · [VoltAgent/awesome-claude-code-subagents](https://github.com/VoltAgent/awesome-claude-code-subagents) (25.6k★) · [andyrewlee/awesome-agent-orchestrators](https://github.com/andyrewlee/awesome-agent-orchestrators) (2.1k★) · [bradAGI/awesome-cli-coding-agents](https://github.com/bradAGI/awesome-cli-coding-agents) (1.3k★) · [anthropics/skills](https://github.com/anthropics/skills) (180k★, official)

## Candidate improvements for `delegate`, drawn from the above

1. **`delegate followup NAME --brief fixes.md`:** resume the same agent session in the same worktree with review findings (`codex exec resume <id>`, `cursor-agent --resume <chatId>`), following superpowers' fix-round policy.
2. **Cross-vendor review step:** after a write delegate finishes, run a read-only review on a different vendor (Codex reviews Cursor's work, and the other way round) before Claude's own review.
3. **`--verify CMD`:** the launcher runs the project's checks after the agent exits and records the result, so "done" means the checks passed (claw-orchestrator's idea).
4. **Brief template:** the Codex plugin's XML contract tags in `SKILL.md`'s brief guidance.
5. **Read-only reviews with `--output-schema`:** structured findings Claude can parse.
