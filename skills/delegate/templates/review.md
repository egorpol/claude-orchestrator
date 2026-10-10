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
     Also challenge the brief: reviews that only checked compliance approved a wrong physics criterion
     and a broken repo convention (2026-10-10). -->
<check_against>
- original brief, and whether its acceptance criteria are right for the domain and the repo's conventions
- `verify.log` / verify exit code
- dry-run output vs expected outcomes
</check_against>
