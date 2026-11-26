---
id: gaps
version: 1
---
### What counts as a gap

Every role that reports, refutes or decides a gap (`complete`, `set_complete`, a missing scenario or criterion) uses these rules.

- A missing piece is a defect only at the level of requirements: what the system must do or guarantee, a rule, a value, data it keeps or shows, or an outcome a user or another system relies on. That level includes which inputs are valid and what the system does with invalid ones, which source or reference data decides an outcome, what the user is told and when, and the time limits and states of a process.
- How the system is built or run is the team's decision, not a gap: screen layout, controls and navigation, the internal mechanism that triggers or carries a step, technology, protocol and API parameters, staffing, rosters and operating procedures.
- A rule the document states once for a whole class of things (a parameters table, a general rule for every code, request, error or document of a kind) applies to every member of that class. A gap claiming that one member lacks the rule is answered by it: quote the general rule. This differs from a catch-all that names no behaviour («ошибки обрабатываются», a generic error code, «по архитектуре»): such a statement does not say what happens and answers nothing.
- A gap is answered only by text that says what happens in the situation it names: to the data, the money, the status, the user, the other systems.
