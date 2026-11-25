---
id: s5_completeness
version: 1
---
### This check: completeness and the document as a whole

Look for what is missing or unfinished:
- behaviour left undefined: errors and invalid input, failures and unavailability of other systems, repeated or duplicate events, concurrent changes, period and time-zone boundaries, limits;
- quality requirements the document's own scope needs but lacks: security and personal data, availability, audit and logging, monitoring, rollback or switching the feature off;
- unfinished template parts: empty version, date, approvals, checklists without answers, sections left "to be filled";
- requirements without identifiers or priorities, statements duplicated in several places;
- goals no requirement serves, or requirements that serve no stated goal.

Quote the nearest text that shows the gap and name exactly what is missing. Report a domain gap only when this document's own scope needs it. In `analysis` (in Russian), list the areas you checked and what you found in each.
