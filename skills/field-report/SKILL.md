---
name: field-report
description: Write a field report on the current Claude Code session into the claude-orchestrator feedback folder. `delegate report` fills in the session's cost, quota and delegate runs; Claude then writes the flow, what worked, friction and suggestions. Use only when the user asks for a field report, a session log or feedback for claude-orchestrator.
argument-hint: "[topic]"
---

# Field report

A field report records what one orchestrator session did and what it cost, so that lessons can be folded into the claude-orchestrator rules, `delegate` SKILL.md, CLI and hook later. Reports go to `feedback/` in the claude-orchestrator checkout. That folder is gitignored and stays local.

1. **Topic:** use `$ARGUMENTS` as a kebab-case slug of 2–5 words. If it is empty, derive one from the session's main item.
2. **Numbers:** from the project root, run

   ```bash
   ~/.claude/skills/delegate/scripts/delegate report --topic <slug> --item "<one-line summary of the session's work>"
   ```

   It reads this session's transcript (via `CLAUDE_CODE_SESSION_ID`) and:
   - writes the skeleton `feedback/<date>-<project>-<topic>.md`, with calls, tokens, context, cost units, idle rebuilds, the context at each typed prompt (tagged where the context guard nudged or held it), quota and delegate runs already filled in;
   - upserts the session's row in `feedback/token_usage.jsonl`;
   - copies this session's delegate runs into `feedback/runs.jsonl`.

   It prints the skeleton's path. The numbers are a snapshot: the calls that write the report come after it.
   - **An earlier session:** if the user means an earlier session of this project, not the current one, add `--session <id-prefix or transcript path>`. `ls -t ~/.claude/projects/<cwd with / replaced by ->/*.jsonl` lists the transcripts, newest (the current session) first; confirm the pick with the user. Write the narrative from that session's project `HANDOFF.md`, commits and transcript. Hand a large transcript to `delegate ask` for a step-by-step summary instead of reading it here. A fresh session reporting on an old one also avoids resuming it, which would rewrite its whole context at 2× once the cache has expired.
   - Exit 1 means the file already exists. Pass `--force` only if this session wrote that file; otherwise pick another topic.
   - Exit 2 means it found no feedback folder (a `--copy` install). Ask the user for a folder, then pass `--feedback-dir DIR`, or have them set `CLAUDE_ORCHESTRATOR_FEEDBACK`.
3. **Narrative:** replace every `<!-- -->` placeholder from what happened in this session, not from other sessions or memory.
   - **Opening paragraph:** where the session started (a handoff file or the first prompt); the items done; the delegates run; the commits (hashes); what was left uncommitted or unpushed.
   - **Flow and cost:** one row per step: what was done, and the outcome with numbers.
   - **What worked** and **Friction:** concrete, with numbers (calls, minutes, tokens, rounds) and the cause of each. Friction that cost calls or rounds is the most useful part of a report.
   - **Suggestions for claude-orchestrator:** changes to the rules, SKILL.md, the CLI or the hook, each tied to a friction item.
   - **Not in this repo's scope:** lessons that belong to the project itself.

   Keep the report under about 7 KB. Project names, session IDs and quota percentages are fine to include. Never include secrets (tokens, keys, credentials).
4. **Nothing else:** don't edit other files in the claude-orchestrator checkout. Folding the lessons in is a separate session there.
5. **Reply** with the report's path and the 2–3 main lessons, one line each.
