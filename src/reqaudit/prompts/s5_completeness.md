---
id: s5_completeness
version: 4
---
### This check: completeness and the document as a whole

Look for what is missing or unfinished:
- behaviour left undefined: errors and invalid input, failures and unavailability of other systems, repeated or duplicate events, concurrent changes, period and time-zone boundaries, limits;
- quality requirements the document's own scope needs but lacks: security and personal data, availability, audit and logging, monitoring, rollback or switching the feature off;
- unfinished template parts: empty version, date, approvals, checklists without answers, sections left "to be filled";
- requirements without identifiers or priorities, statements duplicated in several places;
- goals no requirement serves, or requirements that serve no stated goal.

Work in two passes. First fill `checklist`, built from this document's own content:
- every external system, service or component it names: one item each for its failure, timeout or unavailability, and, where an operation spans systems, for a partial outcome (one step done, the next one failed);
- every input a user or another system provides: one item for invalid, missing or repeated values;
- every limit, period and status: one item for its boundaries and what happens past them; for a status, how it is entered and how it is left, including when no one acts (a status that nothing leaves except the happy path keeps the process stuck). The list under `<limits_and_states>` names those the document states; cover each;
- every quality area from the list above that this document's scope needs.
Give the block that names the thing, and the status: `ok` (only where the document answers it; never because the gap seems unimportant, since importance goes into severity), `gap`, `conflict` or `n/a`. Then report every `gap` and `conflict` as a finding that quotes that block and names exactly what is missing. Report a domain gap only when this document's own scope needs it. Mark `n/a` an item whose answer is a design or operations decision rather than a requirement: the gap is what the system does or guarantees in that situation, not how it is built or staffed. In `analysis` (in Russian), sum up.
