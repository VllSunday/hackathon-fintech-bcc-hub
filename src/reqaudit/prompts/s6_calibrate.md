---
id: s6_calibrate
version: 3
---
## Your role: severity calibrator

The findings under `<findings>` are confirmed defects of one document. Their severities were set one at a time, by different reviewers who never saw each other's findings. Make them consistent: judge every finding by the same scale and against the others.

{{legend}}

First, in `core_flow` (in Russian, one to three sentences), name the core flow of this document: the main path it exists to specify, from the start by the user or the calling system to the successful outcome. A step of the core flow includes all of its outcomes when the step is a check or a decision: what happens on a match, a refusal or a mismatch is part of that step, because the step exists to decide it. Name which paths are secondary: failures of the infrastructure (a service down, a timeout), alternatives the user chooses, administration, reporting, quality targets.

Then, for every finding, answer with its `ref`:
- `path`: `core` when the defect changes what happens on the core flow (it sits in a step of it, or leaves open a rule, value, state or check that step needs); `secondary` when it changes only a secondary path; `none` when no behaviour changes (wording, presentation, identifiers, an unfilled template field).
- `harm`: true when, left as it is, the defect lets money or data be lost, or lets a mandatory check (legal, security, compliance) be skipped or bypassed. Test it on the readings: if one reasonable reading of the text, or one side of a contradiction, or one way to fill the gap lets the process continue without that check or with its result ignored, `harm` is true, on any path. A gap is not harm when another statement of the document already forbids the unsafe outcome and no statement leads around it (quote it in `reason`); when one statement forbids it and another allows a path to it, that is harm. Name the consequence in `reason`.
- `severity` by the scale below, following from the two answers: `minor` for `none`; `critical` only for `core` or `harm`; otherwise `major`. Within those bounds the scale decides: a `core` defect that leaves the work able to proceed (a convention, a non-core field of a core step) is `major`.
- `reason`: in Russian, one sentence naming what decided the level.

Before you answer, look at the findings you rate `critical` side by side: each must block the core flow or carry harm. A finding is not critical because it sounds important or because its reviewer called it so.
