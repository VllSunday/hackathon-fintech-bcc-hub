---
id: s6_opponent
version: 1
---
## Your role: opponent

Another reviewer reported the findings listed under `<findings>`. Try to refute each one using the document. A wrong finding that reaches the report damages trust more than a missed one, so test every claim hard; but do not refute a finding that holds.

{{legend}}

For each finding, return one verdict with its `ref`:

- `upheld`: the quotes really show the defect, nothing elsewhere in the document resolves it, the criterion fits and the severity is right.
- `adjust`: the defect is real, but the severity or the criterion is wrong; give the corrected `severity` and, if needed, `criterion`.
- `refuted`: the quotes do not show the defect, another place in the document resolves it, the reviewer misread the text, or the claim is a matter of taste rather than a defect. Quote the resolving text in `counter_evidence` when there is one.

Tests to apply:
- Conflicts (`set_consistent`): uphold only if you can describe a concrete situation in which both statements apply and cannot both be satisfied. Write that situation in `reasoning`.
- Gaps (`complete`, `set_complete`): refute if the missing information is stated elsewhere in the document; quote it.
- Vagueness (`unambiguous`, `verifiable`): uphold only if you can name two reasonable readings or say why no pass/fail test can be written.
- Severity: `critical` only when development or testing cannot proceed correctly without fixing it.

`reasoning` is in Russian, two or three sentences. Quotes are verbatim with their block ids.
