# Weather scheduler deployment

These scripts are not automatically applied migrations. The Supabase CLI was not
installed in the authoring environment. Review and apply them as `postgres`.

1. Apply `install_weather_dispatch.sql`. Its transaction installs four **inactive**
   jobs in a private schema; it neither provisions a token nor sends requests.
   It first verifies the existing `pg_cron` and `pg_net` extensions without issuing
   extension DDL, avoiding Supabase's extension privilege-maintenance event trigger.
   It restricts only the new `weather_scheduler` schema, functions and audit table.
   Existing Supabase-managed `net` and Vault privileges remain unchanged. It checks
   that `anon`/`authenticated` have NOLOGIN and postgres has required managed access.
   Before provisioning a credential, verify that the actual Data API rejects
   `Accept-Profile: net`, `vault`, and `weather_scheduler`, using both an anon key
   and the trusted service-role key. A GET selecting only `id` with `limit=0` should
   return HTTP 406/PGRST106. Do not print keys, token-bearing columns or headers.
   Also verify these schemas are absent from the GraphQL/API search path and no
   public view or callable RPC exposes their objects or executes arbitrary SQL.
2. Provision Vault secret `weather_github_actions_token` with a dedicated GitHub
   credential restricted to `epeters6/sportspick-wc2026`, Actions write permission.
   Do not copy a token into committed SQL, cron text, an audit row, or logs.
3. Confirm `main` contains the optional `scheduled_for` dispatch input on both
   workflows and validates its age immediately before executing Python. Weather
   requests must remain in their original UTC hour. CLV requests expire after five
   minutes. Inputs carry the actual dispatch timestamp, never a backdated forecast.
   All weather entrypoints must also reject starts after minute 34 of the hour,
   leaving the entire 25-minute job budget before the next hour boundary.
4. Apply `activate_weather_dispatch.sql` after the API-boundary checks, then use
   `weather_dispatch_evidence.sql`.
   Check the returned GitHub run URLs for actual start/completion and stage results.
   Verify two automatic CLV intervals and the next eligible local weather hour.

This follows Supabase's managed `pg_net`/Vault model. Queue rows temporarily contain
the outbound authorization header; managed object-level queue grants are not the
same as public API exposure. Supabase excludes `net` from the Data API, while the
untrusted API roles cannot log in directly. `service_role` is a trusted server
credential and retains its existing database privileges, including Vault access;
this design does not claim to hide the GitHub token from trusted database/server
operators. Keep those credentials out of browser code and do not expose SQL RPCs,
secret-bearing views, or the managed schemas. API exposure must be rechecked if
those settings or public database functions change.

Hourly weather requests run at minute 7. The minute-27 watchdog skips a current-hour
forecast claim, a current-hour report for the active baseline experiment, or a primary
request less than 15 minutes old. Otherwise it sends at most one additional request
in that hour. GitHub may have accepted the first request but delayed execution;
the workflow concurrency group, execution-time expiry and immutable forecast-hour
claim are therefore required. No failed forecast claim is removed or retried.

CLV requests run at minutes 2, 7, 12, ... 57. Duplicate invocations within a slot
cannot enqueue again. A failed CLV delivery waits for the next five-minute slot;
the collector retains its existing timestamp and checkpoint eligibility rules.
The SQL does not activate the existing legacy observer or any legacy workflow.

HTTP receipts are copied every minute because `pg_net` normally retains responses
for six hours. `accepted` only means GitHub returned HTTP 200/204. A valid returned
run ID is saved as a fixed-repository link; completion must be checked separately.
Missing/expired credentials, transport errors, HTTP rejections and missing receipts
remain visible. A DB/worker crash may lose a `pg_net` request or response; the audit
keeps that uncertainty rather than claiming success. No network call is sent until
the SQL transaction commits.

Use `disable_weather_dispatch.sql` to stop future requests while retaining evidence.
This does not cancel already accepted GitHub runs. For complete removal after the
audit is archived, unschedule **only** the four `weather-github-*` names listed in
that file, then drop `weather_scheduler`; do not drop shared `pg_cron`, `pg_net`,
Vault, or the existing CLV cron job. Credential revocation is separate. Managed
queue/Vault privileges are not changed by installation or rollback.

## Advancing the paper baseline

For an already installed, active scheduler, apply only
`upgrade_weather_dispatch_experiment.sql` as `postgres`. It replaces
`weather_scheduler.dispatch(text,boolean)` without changing cron jobs, their active
states, permissions, credentials or audit history. Do not rerun the installer:
installation deliberately marks dispatcher jobs inactive.

The watchdog reads `weather_cycle_latest.experiment.id` once and checks only that
experiment's immutable forecast-hour claim. Supported identities have the form
`weather_forward_YYYY_MM_vN` (a valid two-digit month and positive version); v1 and
v2 both work. Historical v1 claims remain intact when v2 becomes active. A missing,
empty or unsupported identity records `ACTIVE_WEATHER_EXPERIMENT_UNKNOWN` and skips
the watchdog; primary hourly delivery still bootstraps a new cycle report. The
existing time gates and primary-request duplicate protection remain in effect.

After applying the incremental SQL, use `weather_dispatch_evidence.sql` to confirm
that all four jobs remain active, then verify actual GitHub cycle completion and
the next eligible forecast claim for the new baseline. SQL request acceptance
alone does not establish that a forecast executed.

## Local validation

`test_weather_dispatch_local.mjs` executes the SQL function bodies in PGlite's
PostgreSQL runtime with in-memory cron, HTTP and Vault fixtures. It makes no
Supabase connection or HTTP request. The hosted-extension availability check is
tested for failure when extensions are absent, then bypassed for the fixtures;
the remaining installer, activation/disable/evidence queries and rollback test
bodies execute as written.

```sh
npm install --prefix reports/weather-dispatch-sql-check --ignore-scripts --no-audit --no-fund --no-save @electric-sql/pglite@0.5.8
node supabase/ops/test_weather_dispatch_local.mjs reports/weather-dispatch-sql-check
```

The 22 local checks passed on PostgreSQL 18.3/PGlite 0.5.8, including v1/v2
watchdog claims, invalid identities, and incremental-upgrade preservation. The deployed Supabase
project uses PostgreSQL 17, so managed extension ownership/permissions and actual
delivery must still be verified after deployment. The atomic unique constraint is
the concurrency guard; local duplicate tests use a single database connection.
`test_weather_dispatch_rollback.sql` is also provided for operator validation on an
inactive installation before provisioning the real credential. Run that entire
file in one transaction; its fake credentials and enqueued requests roll back.

References: [GitHub dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event),
[Supabase Cron](https://supabase.com/docs/guides/cron),
[pg_net permissions, delivery and response retention](https://supabase.com/docs/guides/database/extensions/pg_net#permissions).
