---
id: s5_parent_child
version: 2
---
### This check: main document versus child specification

The profile names a main part and a child specification that details something the main part refers to. Compare them:
- everything the main part says the child provides (operations, fields, limits, behaviour) is specified there; name what is promised but absent;
- values and rules stated on both sides agree: limits, types, units, defaults, behaviour on change and on repetition;
- the child specification's own description of the operation matches what the main part needs from it.

Work in two passes. First fill `checklist`: one item for every operation, rule, prohibition, validation, limit and field the main part says, or clearly implies, the child must provide or enforce — including rules a request must be checked against, and data the user must see before confirming. Give the block of the main part that states it, and its status in the child: `ok` (where), `gap` (absent), `conflict` (stated differently). Then report every `gap` and `conflict` as a finding that quotes that block and, for a conflict, the child's side. In `analysis` (in Russian), sum up.
