# Review brief

## `<focus>`
<!-- What to prioritise (e.g. race conditions, schema drift, off-by-one). -->
<focus>
</focus>

## `<risk_areas>`
<!-- Modules or paths most likely to be wrong: parsers, validation, matching, accounting, guards. -->
<risk_areas>
</risk_areas>

## `<check_against>`
<!-- Compare the patch to: the original brief, verify.log, and any dry-run output.
     Also challenge the brief: a review that only checks compliance approves a wrong criterion
     or a broken repo convention. -->
<check_against>
- original brief, and whether its acceptance criteria are right for the domain and the repo's conventions
- each invariant the brief states, against the adjacent cases, not only the example it names
- each new free/dispose/release call: what the next use costs, measured old vs new (a work-switch
  regression passed every gate and an APPROVE)
- `verify.log` / verify exit code
- dry-run output vs expected outcomes
</check_against>
