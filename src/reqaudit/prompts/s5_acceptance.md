---
id: s5_acceptance
version: 3
---
### This check: acceptance criteria

- Each acceptance criterion must follow from the requirements: recompute its expected result from the rules and report every criterion whose result disagrees, quoting the criterion and the rule.
- Each functional requirement needs at least one criterion. Report requirements without any, quoting the requirement.
- Each criterion must be checkable: precondition, action and expected result are concrete.
- The boundary and negative cases the requirements define (limits, exclusions, errors, repeats, period boundaries) need criteria; report the most important missing ones, quoting the rule that defines the case.

Fill `checklist` first: one item for every acceptance criterion (a table row or a scenario), with its block, the rule it tests, and the status `ok` when the expected result follows from the rules or `conflict` when recomputing it from the rules gives something else. For every item also give `tests`: the report ids (R-…) of the requirements whose rules the criterion exercises; leave it empty only when no requirement states the rule it tests. Requirements without criteria are not checklist items: report only the most important of them as findings. In `analysis` (in Russian), sum up.
