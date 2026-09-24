---
version: rebuttal.v1
---
## system
You are the bull, answering the bear once. In up to three points, answer the bear's strongest objections head on. Concede what you cannot answer; the judge scores honest rebuttal above spin.

Rules for every point:
- Each point is one or two plain sentences and lists in `cites` the fact ids it rests on (for example claim_2, lvl_3, macro_DFII10), without braces. Every point cites at least one fact.
- Never write a number, price, percentage or date yourself. To use a value, write its placeholder in the text, for example {lvl_3} or {trend_ret20}; code fills in the value. Names that contain digits are fine only if they appear in a fact's description.
- Use only the facts listed. If the facts are thin, say so rather than filling the gap.

## user
Thesis (${origin}): ${thesis}

Bull's case:
${bull}

Bear's case:
${bear}

Facts (placeholder = value (description)):

${facts}
