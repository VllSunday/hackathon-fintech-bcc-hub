---
id: s5_parent_child
version: 3
---
### This check: main document versus child specification

The profile names a main part and a child specification that details something the main part refers to. Compare them:
- everything the main part says the child provides (operations, fields, limits, behaviour) is specified there; name what is promised but absent;
- values and rules stated on both sides agree: limits, types, units, defaults, behaviour on change and on repetition;
- the child specification's own description of the operation matches what the main part needs from it.

Work in two passes. First fill `checklist` as a traceability matrix: one item for every requirement of the main part (R-… ids from the index, in `unit_id`), with the block that states it and its status in the child: `ok` (where the child provides or enforces it), `gap` (the child should provide or enforce it and does not: an operation, a field, an input the operation needs to run, a check or prohibition the request must pass, data the user must see before confirming), `conflict` (stated differently), or `n/a` (nothing for the interface to do). Add items without `unit_id` for anything else the child must provide. Then report every `gap` and `conflict` as a finding that quotes that block and, for a conflict, the child's side. In `analysis` (in Russian), sum up.
