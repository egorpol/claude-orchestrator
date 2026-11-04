# Implement brief

## `<task>`
<!-- Goal and intent (what must be true when done), not the mechanism.
     If you do prescribe a mechanism, add the invariant it serves and one adversarial case it must survive
     ("two adjacent declarations of different licences both survive"): a prescribed fix tends to handle the
     reported example and fail the case next to it.
     When a requirement adds a free/dispose/release path, also state the hot-path invariant it must keep
     ("Play after a work switch no slower than the old build") and the check that measures it: a memory bound
     alone invites freeing everything on each switch, a slowdown that tests and review don't see. -->
<task>

</task>

## `<context>`
<!-- Files this run owns; files not to touch. Cite the target repo's AGENTS.md; list only deltas from it.
     Before launch, list the brief's factual claims and the probe behind each. A claim made for every item
     (layout, field meaning, file naming) is probed on every item, outliers first: one outlier breaks a
     claim stated for all of them. Provenance ("both agents agreed", "the user chose") cites the ask or answer
     file by path: an uncited one can travel into public text untrue. -->
<context>

</context>

## `<schema_contract>`
<!-- What may change. Everything else must round-trip byte for byte.
     Name convention-stamped columns (dump_date, fetched_at, ...) that identity checks must ignore:
     "byte-identical" alone keeps a stale stamp, and a review can approve it.
     When the rule is about bytes, also say what the output should look like ("as the tool itself would write it")
     and name the whitespace case: taken literally, the rule keeps whitespace-only lines and costs a round. -->
<schema_contract>

</schema_contract>

## `<verification_loop>`
<!-- Tests to run, plus a real-data dry run with expected outcomes (exit code, key counts, sample rows).
     Compute the expected counts yourself by a different code path; ask for any difference with its cause, not a tuned rule.
     A smoke run reports one sanity count per output (rows, tracks, events, RMS) and flags degenerate ones (0-1 tracks,
     near-silent): "completed" alone can hide a degenerate output that passes every gate.
     Network code: besides injected-response tests, one test through the real adapter with a fake session
     (mixed-case headers, a body that must not be read): injected responses never reach the adapter's own bugs. -->
<verification_loop>

</verification_loop>

## `<default_follow_through_policy>`
<!-- Keep going until verify passes and conflicts with real data are flagged; do not stop at a partial patch. -->
<default_follow_through_policy>
Keep going until the verification loop passes and any conflict with real data is flagged in the final message.
</default_follow_through_policy>
