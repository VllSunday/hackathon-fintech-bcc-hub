---
id: s5_acceptance
version: 1
---
### This check: acceptance criteria

- Each acceptance criterion must follow from the requirements: recompute its expected result from the rules and report every criterion whose result disagrees, quoting the criterion and the rule.
- Each functional requirement needs at least one criterion. Report requirements without any, quoting the requirement.
- Each criterion must be checkable: precondition, action and expected result are concrete.
- The boundary and negative cases the requirements define (limits, exclusions, errors, repeats, period boundaries) need criteria; report the most important missing ones, quoting the rule that defines the case.

In `analysis` (in Russian), first map each criterion to the requirement it tests and recompute its expected result.
