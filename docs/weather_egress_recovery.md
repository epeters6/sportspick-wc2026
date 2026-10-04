# Weather research transfer budget and recovery

The October 4 outage is an application API restriction (`402`,
`exceed_egress_quota`). Weather and CLV workflows were disabled. A cron success
or accepted dispatch does not establish that evidence was collected.

## Incremental history

The learning runner keeps full research evidence in a compressed Actions cache.
Its first read bootstraps 120 days. Later reads fetch rows created or officially
resolved since the previous check-start watermark, with five minutes of overlap.
Merge by prediction ID; advance the watermark only after both streams complete.
Training and forward validation retain the existing 120-day decision window.
Training still runs after 24 hours; station collection hours are unchanged.

Predictions in these sources are append-only except for official outcome labels,
whose writes set `resolved_at`. Other corrections must explicitly invalidate or
reconcile affected cached IDs; this cache is not a generic table change feed.
Do not silently reset a corrupt or mismatched cache. Its error must be investigated.
Actions cache eviction can cause another bootstrap. Inspect `history_sync` in the
actual learning artifact: cached rows, downloaded rows, bootstrap, and watermark.
Do not interpret a green outer job as successful synchronization.

The segment fetcher accepts `--input` and bootstraps its cache from an existing
saved export. It refreshes only v3 forecasts and official labels, excluding unrelated
paper ledgers, obligations and claims. It preserves full validation metadata.
Run the existing audit without `--freeze`; the frozen study is not replaced.

## Safe recovery

1. Confirm the organization's egress reset date in the signed-in dashboard. Do
   not assume the calendar-month boundary. Optimization reduces future transfers;
   it cannot reverse already incurred usage.
2. Deploy the repair on main. Keep workflows disabled while REST returns 402.
3. Once a minimal REST read succeeds, restore Weather Paper Research and CLV
   Checkpoints only. Keep sports disabled and all loss stops intact.
4. Verify an eligible scheduled run, the isolated learning report, newly persisted
   forecasts at valid station hours, official labels, and checkpoint receipts.
   Do not manufacture missing historical prices or fire late fallback runs.
5. Monitor compact report summaries and changed records. Do not repeatedly export
   the full prediction history or download every historical workflow artifact.

No plan upgrade, spend-cap removal, live execution or loss reset is part of this
repair. Record the observation gap in the frozen prospective study. Two unresolved
paper positions must be reconciled with official results after service returns.
