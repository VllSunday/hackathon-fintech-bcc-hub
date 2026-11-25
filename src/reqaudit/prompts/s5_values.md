---
id: s5_values
version: 1
---
### This check: values across the document

Under `<fact_table>` you get the document's facts lined up by entity: values, units, types, ranges, defaults, mandatory flags, methods, time limits, periods and comparisons, each with its quote. Under `<code_found_discrepancies>` code lists where those facts disagree. Code only compares strings; it cannot tell a real conflict from values that legitimately differ.

1. Judge every discrepancy code found. Report it when the values cannot both hold or when the document does not say which one applies; quote each side. Skip it when a condition, an example's situation or a definition in the document explains the difference.
2. Then read the fact table yourself for what code cannot see: a quantity compared with another in different units or types without a conversion, a default that contradicts a mandatory flag, a range in one place and a different one in another, a type in a request that differs from the type in a response, an example or acceptance criterion whose values break a stated rule.

In `analysis` (in Russian), go through the code's discrepancies one by one with your decision, then list what else you checked. Every finding quotes the statements themselves from the document, not the fact table.
