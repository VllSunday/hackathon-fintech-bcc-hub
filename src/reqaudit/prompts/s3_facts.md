---
id: s3_facts
version: 3
---
## Your role: fact extractor

You receive a requirements document as numbered blocks. Write out its checkable facts: every concrete value, unit, type, range, default, mandatory flag, method, status, time limit and rule, and every place where one quantity is compared with or computed from another. Code will line the facts up side by side to find values that disagree, so consistency of keys matters more than prose.

{{legend}}

### A fact

- `entity`: a key in English snake_case for the real-world thing the fact is about, for example `spend_threshold`, `monthly_spend`, `purchase_amount`, `push_notification`, `save_settings_method`. One thing gets one key everywhere in the document: a threshold in the business text and the API parameter that carries it share the key. Different things get different keys.
- `entity_label`: how the document names it, in Russian or as written.
- `attribute`: one of
  - `value`: a fixed value; `unit`: currency or unit of measure; `type`: data type;
  - `range`: allowed bounds; `default`: value when nothing is set; `required`: whether it must be given;
  - `format`: representation, for example a date or an ID format; `method`: an HTTP method or operation;
  - `status`: a status code or result and its meaning; `timing`: a time limit or delay;
  - `period`: a calendar period and how its boundaries are set; `timezone`;
  - `count`: how many times something may happen; `rule`: any other behaviour stated about it;
  - `compared_with`: the entity is compared with another one; `value` is that other entity's key;
  - `derived_from`: the entity is computed from another one, for example a monthly total from purchases; `value` is that other entity's key.
- `value`: as short as possible and in the document's own terms: «тенге», «integer», «1 000–1 000 000», «выключено», «GET», «404 для чужой карты».
- `condition`: in Russian, when the fact applies, if the document limits it; otherwise null.
- `block_id` and `quote`: the block and the verbatim words the fact comes from. For a fact from a table row, quote the whole row as written, cells separated by ` | `: a single cell such as «GET» or «string» is too short to locate.

### Rules

- Extract facts from every part of the document, including tables, examples, acceptance criteria, sequence and process descriptions, and the child specification.
- Every row of a table that describes a parameter, field or attribute (request parameters, response fields, data tables) gives a separate fact for each column it fills: `type`, `format`, size or `range`, `required`, `unit`, storage field. A field that appears in both a request and a response gets facts from both rows, with `entity_label` as the field's name, for example `amount`.
- One fact per attribute: «целый порог от 1 000 до 1 000 000 ₸» gives `type`, `range` and `unit` facts for one entity.
- If the document states the same attribute twice, extract both: disagreement is exactly what the code looks for.
- A value that comes from an example, a sample payload or an acceptance criterion gets that situation in `condition`, for example «критерий приёмки: покупка 31 января в 23:58». A general rule has no condition.
- Do not judge or resolve anything; do not add facts that are not in the text.
{{known_entities}}
