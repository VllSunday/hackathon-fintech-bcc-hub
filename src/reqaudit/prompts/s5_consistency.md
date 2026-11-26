---
id: s5_consistency
version: 2
---
### This check: consistency of the whole set

Find statements that cannot all be true or cannot all be implemented together:
- the same parameter given different values: limits, periods, counts, formats, units (for example minutes in one place and seconds in another, or a currency and its minor unit), time zones, defaults;
- rules that exclude each other, or a rule and its exception that overlap without saying which wins;
- a process or sequence description that contradicts a stated rule;
- one term used for different things, or different terms for one thing;
- an acceptance criterion whose expected result contradicts a requirement.

Work in two steps inside `analysis` (in Russian): first list the parameters and rules that are stated more than once or in different sections, then compare them. For every conflict, quote each statement and describe in the explanation a concrete situation in which they clash.

{{scope_note}}
