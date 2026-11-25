---
id: s5_api_contract
version: 1
---
### This check: interface specification

Check the interface specification on its own:
- the HTTP method fits the operation (reading versus changing data, safety and idempotency);
- URLs, parameters (types, sizes, ranges, mandatory flags, units) and response fields (types, when present) are defined and consistent with each other;
- success and error results: how the client tells them apart, whether an error can arrive with a success status;
- status codes: listed codes, their meanings, the example bodies under each, and whether they agree;
- authorisation and access errors: missing session, someone else's resource, missing resource;
- contradictions between tables, examples and prose inside the specification.

In `analysis` (in Russian), walk through the specification section by section.
