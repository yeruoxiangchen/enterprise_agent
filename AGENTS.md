# Strategy implementation expectations

User requirements recorded on 2026-11-14:

- Develop substantive agent capabilities by examining and reusing maintained
  multi-agent implementations and relevant research mechanisms.
- Prefer real upstream modules over small handwritten substitutes for mature
  orchestration, context management, memory maintenance, and recovery mechanisms.
- Treat a few added prompts, regexes, or ad hoc control branches as engineering
  changes. Do not present them as a completed experience-learning strategy.
- Distinguish direct library reuse, adapted research mechanisms, and this
  project's own implementation. Record upstream versions and attribution.
- Deliver the mechanism through a complete lifecycle and actual task behavior.
  Check correctness, failure handling, success rate, and inference cost against
  an appropriate baseline. Code-only checks do not establish model task gains.
- An experience-learning feature includes trajectory-based induction, feedback,
  updates, and validation; a manually authored rule collection is labeled as such.

The current source-reviewed implementation direction and module mapping are in
[HARNESS_REDESIGN_20261114.md](HARNESS_REDESIGN_20261114.md).
The root workspace I/O policy and the user's monitoring preferences still apply.
