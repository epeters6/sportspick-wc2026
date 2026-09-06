# Weather scheduler deployment

These scripts are not automatically applied migrations. The Supabase CLI was not
installed in the authoring environment. Review and apply them as `postgres`.

1. Apply `install_weather_dispatch.sql`. Its transaction installs four **inactive**
   jobs in a private schema; it neither provisions a token nor sends requests.
   It also revokes application-role access specifically to `net.http_request_queue`
   and `vault.secrets`/`vault.decrypted_secrets`. Queue updates could redirect a
   request containing authorization headers, so all direct queue privileges are
   removed. Generic `net` schema, HTTP functions and response-table access remain
   unchanged. Existing jobs owned by `postgres` retain access. Verify no application
   role can access credential storage before provisioning the new token.
2. Provision Vault secret `weather_github_actions_token` with a dedicated GitHub
   credential restricted to `epeters6/sportspick-wc2026`, Actions write permission.
   Do not copy a token into committed SQL, cron text, an audit row, or logs.
3. Confirm `main` contains the optional `scheduled_for` dispatch input on both
   workflows and validates its age immediately before executing Python. Weather
   requests must remain in their original UTC hour. CLV requests expire after five
   minutes. Inputs carry the actual dispatch timestamp, never a backdated forecast.
   All weather entrypoints must also reject starts after minute 34 of the hour,
   leaving the entire 25-minute job budget before the next hour boundary.
4. Apply `activate_weather_dispatch.sql`, then use `weather_dispatch_evidence.sql`.
   Check the returned GitHub run URLs for actual start/completion and stage results.
   Verify two automatic CLV intervals and the next eligible local weather hour.

Hourly weather requests run at minute 7. The minute-27 watchdog skips a current-hour
forecast claim, a current-hour report for the frozen experiment, or a primary
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
Vault, or the existing CLV cron job. Credential revocation is separate. Retain the
queue/Vault privilege hardening; rollback does not restore secret-read access.

## Local validation

`test_weather_dispatch_local.mjs` executes the SQL function bodies in PGlite's
PostgreSQL runtime with in-memory cron, HTTP and Vault fixtures. It makes no
Supabase connection or HTTP request. Only the two hosted extension installation
statements are removed; the production installer, activation/disable/evidence
queries and rollback test bodies execute as written.

```sh
npm install --prefix reports/weather-dispatch-sql-check --ignore-scripts --no-audit --no-fund --no-save @electric-sql/pglite@0.5.8
node supabase/ops/test_weather_dispatch_local.mjs reports/weather-dispatch-sql-check
```

The 15 local checks passed on PostgreSQL 18.3/PGlite 0.5.8. The deployed Supabase
project uses PostgreSQL 17, so managed extension ownership/permissions and actual
delivery must still be verified after deployment. The atomic unique constraint is
the concurrency guard; local duplicate tests use a single database connection.
`test_weather_dispatch_rollback.sql` is also provided for operator validation on an
inactive installation before provisioning the real credential. Run that entire
file in one transaction; its fake credentials and enqueued requests roll back.

References: [GitHub dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event),
[Supabase Cron](https://supabase.com/docs/guides/cron),
[pg_net delivery and response retention](https://supabase.com/docs/guides/database/extensions/pg_net).
