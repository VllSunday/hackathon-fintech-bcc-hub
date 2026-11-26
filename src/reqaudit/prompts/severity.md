---
id: severity
version: 2
---
### Severity scale

Every role that sets or changes a severity uses this one scale. The test is what happens if the team starts building and testing now, without asking the document's author:

First name the core flow: the main path the document exists to specify, from the user's or the calling system's start to the successful outcome. Failure, alternative, administrative and reporting paths are secondary.

- `critical`: to build or test a core behaviour the team would have to ask the author or guess. Two reasonable readings give different observable behaviour; a contradiction forces them to choose which requirement to break; or a rule, value or operation the core flow needs is missing.
- `major`: the work can proceed, but different people would build or test it differently in secondary behaviour (an edge case, a failure or alternative path, an error message, a non-core field), or a convention is broken in a way that changes how an interface behaves. A gap in a secondary path is `major` unless, left open, it lets money or data be lost or a mandatory check (legal, security, compliance) be skipped: then it is `critical`.
- `minor`: wording, presentation, identifiers or an unfilled template field; what the system does is not affected.

What does not lower a severity:
- «The team could start with a stub» or «they could pick one option»: a stub or a pick is a guess, and the guess is exactly the defect.
- «Testers will notice it later»: the scale is about the document now.

What can lower it: the document itself settles the question elsewhere (quote it), or the affected behaviour is not core to what the document specifies (say which behaviour is core).

Each criterion card also says how the scale usually applies to that criterion.
