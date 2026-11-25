---
id: s5_requirements
version: 1
---
### This check: individual requirements

Check each requirement listed under `<requirements_to_check>` on its own against the criteria above. The rest of the document is context: definitions, values and rules stated elsewhere count, and a requirement that relies on them is not incomplete for that reason.

How to work through each requirement:
- Unambiguous: can two competent developers read it differently? Name the two readings.
- Verifiable: can a tester write a pass/fail test from it? Look for thresholds without values, units or conditions.
- Complete: who does what, to what, under which conditions, with which limits. Name the missing element.
- Singular: does a requirement that is not numbered by the author bundle several separate obligations? A numbered requirement that bundles several obligations is also a finding.
- Appropriate: does it dictate a design (screen layout, technology, storage) where the need could be stated instead, at a level that does not fit this document?
- Feasible: does it demand the impossible or contradict a physical or regulatory limit?
- Conforming: does it break the document's own conventions for identifiers and wording?

Fill `reviewed` first: every listed requirement id with a one-line note in Russian on what you found or that it is sound. Then list the findings. Conflicts that appear only when two requirements are compared belong to a separate consistency check; leave them out here.
