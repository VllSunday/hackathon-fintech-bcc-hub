---
id: s5_requirements
version: 3
---
### This check: individual requirements

Check each requirement listed under `<requirements_to_check>` on its own against the criteria above. The rest of the document is context: definitions, values and rules stated elsewhere count, and a requirement that relies on them is not incomplete for that reason.

How to work through each requirement: answer the «Check» questions of every criterion card above for it, and look for the typical violations each card names. Before reporting, read the card's «Not a violation» line.

Fill `reviewed` first: for every listed requirement, go through every criterion card above and put in `violated` the codes of the criteria it breaks (an empty list when it meets all of them), with a one-line `note` in Russian. Then report one finding for every code in `violated`. Conflicts that appear only when two requirements are compared belong to a separate consistency check; leave them out here.
