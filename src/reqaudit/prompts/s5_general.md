---
id: s5_general
version: 1
---
### This check: the whole document, read without relying on its recognised structure

Code found that the structure recognised for this document covers it poorly: many blocks were not assigned to any requirement or could not be classified. The other checks rely on that structure, so this check reads the document as a whole and must not rely on it.

Under `<blocks_outside_requirements>` the user message lists the blocks that are not part of any requirement in the index. Read them first:
- a block that obliges the system, a user or a process to something (must, shall, should, is required to, «должен», «обязан», «необходимо», a rule, a limit, a value the system must keep) is a requirement the index missed: check it against every criterion in the list above and report its defects with an empty `unit_ids` and the quote from that block;
- a block with no obligation (context, goals, examples, approvals) needs no finding.

Then check the document as a whole: statements that contradict each other wherever they stand, behaviour left undefined, terms used but never defined, parts that look unfinished.

Do not repeat defects of requirements in the index that their own check would find: report here what lies outside the index or across the whole document. In `analysis` (in Russian), say which blocks outside the index you read as requirements and what else you checked.
