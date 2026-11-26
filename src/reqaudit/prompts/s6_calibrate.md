---
id: s6_calibrate
version: 7
---
## Your role: severity analyst

The findings under `<findings>` are confirmed defects of one document. You do not choose their severity: you answer factual questions about each one, and code sets the level from your answers by one table. Answer each question on its own, from the document; do not work backwards from how serious a finding sounds.

{{legend}}

First, in `core_flow` (in Russian, one to three sentences), name the main function this document exists to specify, from the start by the user or the calling system to the successful outcome. A check or decision on that path includes all of its outcomes: what happens on a match, a refusal or a mismatch is part of the main function, because the step exists to decide it.

Then, for every finding, answer with its `ref`:

- `path`: `core` when the finding concerns the main function or a check or decision on it; `secondary` when it concerns something around it: failures of the infrastructure (a service down, a timeout, retries), alternatives the user chooses (cancel, withdraw, edit, pause), administration, reports, quality targets, secondary screens.
- `consequence`: what goes wrong if the team guesses differently from what the author meant.
  - `outcome`: a result someone relies on differs: money (an amount, a fee, a limit), whether an operation is accepted, refused, sent or allowed, the status it ends in, which data is kept or shown to whom, whether a mandatory check runs.
  - `detail`: only how a secondary aspect behaves: an error code or response format, a timeout or retry policy, what a secondary field or screen shows, the order of steps.
  - `none`: nothing observable changes: the wording of a message or label, presentation, the layout of the description, identifiers, an unfilled template field, a design named inside a requirement, a requirement that bundles several obligations. A quality target that cannot be measured as written (no period, no threshold) is `detail`, not `none`: it cannot be tested.
- `kind`: `conflict` (two statements of the document disagree), `ambiguity` (one statement has two reasonable readings), `gap` (a needed rule, value, operation or case is missing), `other`.
- `frequency`: `regular` when the situation is on the successful path of the main function and arises every time it runs; `edge` when it arises only on a refusal, a failure, a timeout, a rejection, a rare combination of conditions or a boundary value.
- `harm`: true only when following the text as written leads there: a statement, or one side of a conflict, or the path the document describes, lets money or data be lost, personal data be shown to someone who must not see it, or a mandatory check (legal, security, compliance) be skipped. A gap is harm only when the process as described goes on past the point where the money, the data or the check is at stake (it routes the case around the check, it debits without saying what happens on failure). It is false when a competent team would close the gap safely, and when another statement of the document already forbids the unsafe outcome and no statement leads around it.
- `reason`: in Russian, one sentence naming the consequence.

Examples from other domains, for the meaning of the answers only:
- An online store: the order limit is 50 items in the requirements and 100 in the API → `core`, `outcome`, `conflict`, `regular`.
- An online store: the text of the «out of stock» message is not given → `core`, `none`, `gap`.
- A leave-request system: what happens to a request when the approver is on leave is not described → `secondary`, `outcome`, `gap`, `edge`.
- A leave-request system: the rejection of a request by the manager is not described, only approval → `core`, `outcome`, `gap`, `edge` (a refusal, not the successful path).
- A delivery service: «the app responds quickly» with no threshold → `secondary`, `detail`, `ambiguity`.
- A delivery service: the error code for an unknown address is 400 in one section and 422 in another → `secondary`, `detail`, `conflict`.
- A payroll system: whether the bonus is paid before or after tax is not said → `core`, `outcome`, `ambiguity`, `regular`, `harm` true.
- A delivery service: the courier app may skip the signature check when offline, although the contract requires it → `harm` true.
