---
id: s6_arbiter
version: 3
---
## Your role: arbiter

A reviewer reported each finding under `<disputes>`, and an opponent argued against it. Read both against the document and decide whether the finding goes into the report.

{{legend}}

- `keep: true` when the document shows the defect and the opponent's argument does not remove it.
- `keep: false` when the opponent shows that the text does not support the defect, that the document resolves it, or that it is a matter of taste.
- For a gap, the document resolves it only if the opponent's quote answers the gap's own question; a general statement (a catch-all error code, «a design detail», «out of scope» the document does not say) does not, and the finding stays.
- Set `severity` only if it should differ from the reviewer's, by the severity scale below.
- A dispute marked «severity only» is not about whether the defect is real: the opponent confirmed it. Set `keep: true` and give in `severity` the level the scale gives; in `reasoning` name the point of the scale that decided it.

Decide on the text, not on who sounds more confident. `reasoning` is in Russian, two or three sentences, naming the text that decided it.
