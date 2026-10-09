# Implement brief

## `<task>`
<!-- Goal and intent (what must be true when done), not the mechanism. -->
<task>

</task>

## `<context>`
<!-- Files this run owns; files not to touch. Cite the target repo's AGENTS.md; list only deltas from it. -->
<context>

</context>

## `<schema_contract>`
<!-- What may change. Everything else must round-trip byte for byte. -->
<schema_contract>

</schema_contract>

## `<verification_loop>`
<!-- Tests to run, plus a real-data dry run with expected outcomes (exit code, key counts, sample rows). -->
<verification_loop>

</verification_loop>

## `<default_follow_through_policy>`
<!-- Keep going until verify passes and conflicts with real data are flagged; do not stop at a partial patch. -->
<default_follow_through_policy>
Keep going until the verification loop passes and any conflict with real data is flagged in the final message.
</default_follow_through_policy>
