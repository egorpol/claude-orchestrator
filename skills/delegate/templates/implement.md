# Implement brief

## `<task>`
<!-- Goal and intent (what must be true when done), not the mechanism.
     If you do prescribe a mechanism, add the invariant it serves and one adversarial case it must survive
     ("two adjacent declarations of different licences both survive"). Two prescribed fixes handled the
     reported example and failed the case next to it (2026-10-11).
     When a requirement adds a free/dispose/release path, also state the hot-path invariant it must keep
     ("Play after a work switch no slower than the old build") and the check that measures it: a memory bound
     alone led Codex to free every decoded sound on each switch, ~400 ms slower per Play (2026-11-02). -->
<task>

</task>

## `<context>`
<!-- Files this run owns; files not to touch. Cite the target repo's AGENTS.md; list only deltas from it.
     Before launch, list the brief's factual claims and the probe behind each. A claim made for every item
     (layout, field meaning, file naming) is probed on every item, outliers first: "staves 4–5", stated for
     17 works, did not hold for one and was probed 41 s after launch (2026-11-03). Provenance ("both agents agreed",
     "the user chose") cites the ask or answer file by path; one such sentence reached a public plan
     though only 2 of 3 picks had been asked. -->
<context>

</context>

## `<schema_contract>`
<!-- What may change. Everything else must round-trip byte for byte.
     Name convention-stamped columns (dump_date, fetched_at, ...) that identity checks must ignore:
     "byte-identical" once made Codex keep a stale dump_date, and the review approved it.
     When the rule is about bytes, also say what the output should look like ("as the tool itself would write it")
     and name the whitespace case: Codex kept six whitespace-only wrapper lines to honour it, which cost a round. -->
<schema_contract>

</schema_contract>

## `<verification_loop>`
<!-- Tests to run, plus a real-data dry run with expected outcomes (exit code, key counts, sample rows).
     Compute the expected counts yourself by a different code path; ask for any difference with its cause, not a tuned rule.
     A smoke run reports one sanity count per output (rows, tracks, events, RMS) and flags degenerate ones (0-1 tracks,
     near-silent): "completed on all three excerpts" hid a 1-track render that every gate passed (2026-11-04).
     Network code: besides injected-response tests, one test through the real adapter with a fake session
     (mixed-case headers, a body that must not be read). Three rounds of injected tests missed a header-case bug. -->
<verification_loop>

</verification_loop>

## `<default_follow_through_policy>`
<!-- Keep going until verify passes and conflicts with real data are flagged; do not stop at a partial patch. -->
<default_follow_through_policy>
Keep going until the verification loop passes and any conflict with real data is flagged in the final message.
</default_follow_through_policy>
