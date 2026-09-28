# Baseline pipeline funnel (M0)

Generated 2026-09-28 by `python -m content_platform.audit.funnel --workspace .`.
Read-only inventory of available evidence. Missing recent history is marked `unknown`,
never inferred. See the iteration plan section 3 (M0).

# Pipeline funnel — /Users/xuhouchang/Documents/coding/content-hub

| date | collected | scored | candidates | selected | qualified | enqueued | drafts | status | failure |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| 2026-08-22 | 99 | unknown | 5 | 2 | unknown | 8 | 0 | partial | model_failure |

Evidence paths:
- 2026-08-22 -> /Users/xuhouchang/Documents/coding/content-hub/platform/jobs/2026-08-22
    - collected: /Users/xuhouchang/Documents/coding/content-hub/platform/jobs/2026-08-22/collect-daily/job.json
    - scored: unknown (curated materials.json / collect artifacts)
    - article_candidates: /Users/xuhouchang/Documents/coding/content-hub/platform/jobs/2026-08-22/collect-daily/job.json
    - selected: /Users/xuhouchang/Documents/coding/content-hub/platform/datasets/2026-08-22/article_selection.json
    - qualified_finals: unknown (article topic ledger (empty ledger means 0 qualified finals))
    - enqueued: /Users/xuhouchang/Documents/coding/content-hub/queue/pending.jsonl
