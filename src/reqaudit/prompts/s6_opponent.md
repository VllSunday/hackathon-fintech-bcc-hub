---
id: s6_opponent
version: 3
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

Then test the proposed fix (`suggestion`) by counterfactual, for every finding you do not refute: imagine the document with the suggestion applied.
- `fix_works: true` when the defect is gone: the vague word is replaced by a testable value, the missing rule is stated, or one side of the conflict is changed so that your concrete situation no longer breaks; and the new wording contradicts no other place in the document.
- `fix_works: false` when the defect would remain (for example «уточнить формулировку» or a value still left open), or when the new wording would conflict with another statement; then write in `better_fix`, in Russian, a fix that passes this test. Keep values the document already fixes; where only the author can choose a value, name exactly what must be decided. Refer to places the way the document does (its labels such as «Ф-2», section or table names), never by block ids: the reader of the report does not see them.

`reasoning` is in Russian, two or three sentences. Quotes are verbatim with their block ids.
