# Running the fixed archive without a local computer

The `Weather Historical Backfill` workflow uses a standard GitHub-hosted Linux
runner. It is dormant until an explicit handoff enables the repository variable
`WEATHER_BACKFILL_HOSTED_ENABLED`. Merging the workflow alone does not transfer
ownership, publish evidence, or start another importer.

Each invocation retains the existing 1,000-location batch, 3,000 rolling-hour,
6,000 rolling-24-hour and UTC-day limits. Provider/evidence errors retain their
24-hour cooldown. It needs no Supabase key, trading credential, subscription or
increased budget. The original full-archive plan and final retest stay fixed.

## Handoff procedure

1. Merge the tested implementation. Disable local *backfill continuation* in
   the existing heartbeat, retaining ordinary weather monitoring every four
   hours. Verify the worker process has finished and all archive locks are gone.
2. Write `full_backfill_owner.json` in the historical receipts directory with
   version `1`, the existing plan ID, and executor `github_actions`. Existing
   local workers then refuse to download. Do not remove a lock to force this.
3. Use `backend.weather_backfill_checkpoint.pack` to create
   `000000-checkpoint.zip` from the existing archive, receipts, market-input
   file and retest directory. It includes both quota ledgers and all evidence
   in those explicitly selected directories, including missing-data receipts.
   It rejects active locks, mismatched plans, data hashes and missing ledgers.
4. Create a **draft** release tagged
   `weather-backfill-state-2026-04-03-2026-10-19`, targeting the merged commit.
   Upload the bundle and `000000-checkpoint.json`, containing `version: 1`,
   `generation: 0`, the plan ID, `bundle_sha256`, and `complete: false`.
   Keep this release draft; do not publish it. Download and verify the seed
   before enabling the hosted job.
5. Set `WEATHER_BACKFILL_HOSTED_ENABLED=true`, then dispatch one ordinary
   backfill batch. Verify its new committed checkpoint and budget counters.
   Record the ownership change in `full_history_backfill` without advancing
   the database-monitor watermark. The local heartbeat should only inspect
   hosted progress; it must not run a second importer.

## Checkpoint safety

Draft-release assets retain evidence without relying on an evictable Actions
cache or expiring artifact. Each generation appends a lease, bundle and commit
manifest; prior generations are never deleted. Upload/download hashing verifies
the new bundle before its manifest becomes authoritative. A missing latest
manifest stops the next job rather than restoring an old download budget.
Investigate interrupted generations; never erase the lease to bypass a stop.

Only the allowlisted weather directories and market-input JSON enter the bundle.
The existing market-input file contains saved forecast evidence; no new database
export is performed. No environment files, credentials or repository secrets
are included. Keep repository access and release permissions as they are.

GitHub documents that draft releases are listed only to users with push access,
and release assets are limited to 1,000 per release and under 2 GiB each, with
no total release-size or bandwidth cap. The checkpoint reader separately caps
expanded evidence at 8 GB and each compressed bundle at 2 GB. It preserves
earlier immutable dataset revisions instead of deleting them to fit a cache.
Sources: [draft release access](https://docs.github.com/en/rest/releases/releases#list-releases),
[release asset limits](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases#storage-and-bandwidth-quotas).

After verified completion, subsequent invocations do no downloads or retraining.
Disable the backfill workflow after retrieving/verifying the complete checkpoint;
keep ordinary weather jobs and monitoring active. Before any ownership transfer
back to local, stop hosted scheduling and import its **latest committed** quota
ledger and evidence. Never resume from the stale pre-handoff local ledger.
