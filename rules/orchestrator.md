# Orchestrator rules

From the claude-orchestrator field reports (`feedback/`). They load in every session, including sessions that never invoke the `delegate` skill. The session-mode hook (`hooks/session-mode.py`) injects the parts itself, per session; this file imports both for installs without that hook. `reference.md` in this folder holds narrow tips by topic, read on demand.

@hygiene.md
@delegation.md
