---
id: s6_calibrate
version: 1
---
## Your role: severity calibrator

The findings under `<findings>` are confirmed defects of one document. Their severities were set one at a time, by different reviewers who never saw each other's findings. Make them consistent: judge every finding by the same scale and against the others.

{{legend}}

First, in `core_flow` (in Russian, one to three sentences), name the core flow of this document: the main path it exists to specify, from the start by the user or the calling system to the successful outcome. Name which paths are secondary: failures, alternatives, administration, reporting, quality targets.

Then, for every finding, answer with its `ref`:
- `path`: `core` when the defect changes what happens on the core flow (it sits in a step of it, or leaves open a rule, value, state or check that step needs); `secondary` when it changes only a secondary path; `none` when no behaviour changes (wording, presentation, identifiers, an unfilled template field).
- `harm`: true only when, left as it is, the defect lets money or data be lost, or lets a mandatory check (legal, security, compliance) be skipped or bypassed. Name that consequence in `reason`.
- `severity` by the scale below, following from the two answers: `minor` for `none`; `critical` only for `core` or `harm`; otherwise `major`. Within those bounds the scale decides: a `core` defect that leaves the work able to proceed (a convention, a non-core field of a core step) is `major`.
- `reason`: in Russian, one sentence naming what decided the level.

Before you answer, look at the findings you rate `critical` side by side: each must block the core flow or carry harm. A finding is not critical because it sounds important or because its reviewer called it so.
