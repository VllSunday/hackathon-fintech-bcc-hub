---
id: severity
version: 1
---
### Severity scale

Every role that sets or changes a severity uses this one scale. The test is what happens if the team starts building and testing now, without asking the document's author:

- `critical`: to build or test a core behaviour the team would have to ask the author or guess. Two reasonable readings give different observable behaviour; a contradiction forces them to choose which requirement to break; or a rule, value or operation the core flow needs is missing.
- `major`: the work can proceed, but different people would build or test it differently in secondary behaviour (an edge case, an error message, a non-core field), or a convention is broken in a way that changes how an interface behaves.
- `minor`: wording, presentation, identifiers or an unfilled template field; what the system does is not affected.

What does not lower a severity:
- «The team could start with a stub» or «they could pick one option»: a stub or a pick is a guess, and the guess is exactly the defect.
- «Testers will notice it later»: the scale is about the document now.

What can lower it: the document itself settles the question elsewhere (quote it), or the affected behaviour is not core to what the document specifies (say which behaviour is core).

Each criterion card also says how the scale usually applies to that criterion.
