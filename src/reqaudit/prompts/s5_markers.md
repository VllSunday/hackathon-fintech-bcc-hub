---
id: s5_markers
version: 1
---
### Marker words found by code

Under `<marker_candidates>` code lists words in these requirements that often signal a defect: subjective terms, loopholes, open lists, unmeasurable time, absolutes, «и/или», unfinished parts, generic wording. A marker is a candidate, not a defect. Decide on every one in `markers`, before the findings:

- `defect: true` when, in this requirement and this document, the word leaves the behaviour open or untestable; then report the finding under the fitting criterion and quote the words around the marker;
- `defect: false` when the context makes it precise, for example a term the document defines, or a word that is not part of the obligation; say why in `reason`, in Russian.
