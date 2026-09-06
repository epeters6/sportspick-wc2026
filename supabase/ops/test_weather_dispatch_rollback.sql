-- Operator-only integration checks BEFORE credential provisioning or activation.
-- Run the entire file in one connection/transaction. It ends with ROLLBACK.
-- pg_net does not transmit enqueued requests before COMMIT, so no HTTP is sent.
-- The executing database owner must be able to insert mocked net responses.
-- Never run individual statements outside this transaction.
begin;
do $tests$
declare
    request_one bigint;
    request_two bigint;
    recorded integer;
    audit_count integer;
    current_slot timestamptz := date_trunc('hour', clock_timestamp() at time zone 'UTC') at time zone 'UTC';
    claim_key text;
begin
    if exists (select 1 from cron.job where active and jobname in (
        'weather-github-hourly', 'weather-github-watchdog', 'weather-github-clv', 'weather-github-receipts'
    )) then
        raise exception 'TEST_REQUIRES_INACTIVE_SCHEDULER';
    end if;
    if exists (select 1 from vault.decrypted_secrets where name = 'weather_github_actions_token') then
        raise exception 'RUN_TEST_BEFORE_REAL_CREDENTIAL_PROVISIONING';
    end if;
    -- All deletes, fake credentials, audit rows and network queue rows roll back.
    delete from weather_scheduler.dispatch_audit;
    request_one := weather_scheduler.dispatch('weather_cycle.yml');
    if request_one is not null or not exists (
        select 1 from weather_scheduler.dispatch_audit
        where state = 'missing_credential' and reason = 'MISSING_OR_INVALID_VAULT_CREDENTIAL'
    ) then
        raise exception 'MISSING_CREDENTIAL_DID_NOT_FAIL_CLOSED';
    end if;
    if weather_scheduler.dispatch('weather_cycle.yml') is not null
       or (select count(*) from weather_scheduler.dispatch_audit) <> 1 then
        raise exception 'DUPLICATE_MISSING_CREDENTIAL_ATTEMPT';
    end if;

    perform vault.create_secret('scheduler_test_credential_not_real_000000', 'weather_github_actions_token');
    delete from weather_scheduler.dispatch_audit;
    request_one := weather_scheduler.dispatch('weather_cycle.yml');
    if request_one is null or weather_scheduler.dispatch('weather_cycle.yml') is not null then
        raise exception 'PRIMARY_SLOT_NOT_IDEMPOTENT';
    end if;
    if (select count(*) from weather_scheduler.dispatch_audit) <> 1 then
        raise exception 'DUPLICATE_PRIMARY_AUDIT';
    end if;
    -- Mimic a primary whose HTTP response was lost and has had 20 minutes to run.
    update weather_scheduler.dispatch_audit
    set requested_at = clock_timestamp() - interval '20 minutes';
    delete from public.app_settings where key = 'weather_cycle_latest';
    claim_key := 'weather_forecast_slot:weather_forward_2026_09_v1:' ||
        to_char(current_slot at time zone 'UTC', 'YYYYMMDD"T"HH24');
    delete from public.app_settings where key = claim_key;
    request_two := weather_scheduler.dispatch('weather_cycle.yml', true);
    if request_two is null or weather_scheduler.dispatch('weather_cycle.yml', true) is not null
       or (select count(*) from weather_scheduler.dispatch_audit) <> 2 then
        raise exception 'WATCHDOG_EXCEEDED_TWO_REQUESTS_PER_HOUR';
    end if;
    if not exists (select 1 from weather_scheduler.dispatch_audit where state = 'response_missing') then
        raise exception 'LOST_RESPONSE_FALSELY_REPORTED_SUCCESS';
    end if;

    delete from weather_scheduler.dispatch_audit where attempt = 'watchdog';
    insert into public.app_settings (key, value)
    values (claim_key, '{"run_id":"synthetic-failed-attempt","status":"failed"}'::jsonb);
    if weather_scheduler.dispatch('weather_cycle.yml', true) is not null
       or not exists (select 1 from weather_scheduler.dispatch_audit
                      where reason = 'FORECAST_SLOT_ALREADY_ATTEMPTED' and state = 'skipped') then
        raise exception 'FAILED_FORECAST_CLAIM_WAS_RETRIED';
    end if;
    delete from public.app_settings where key = claim_key;
    delete from weather_scheduler.dispatch_audit where attempt = 'watchdog';
    insert into public.app_settings (key, value) values ('weather_cycle_latest', jsonb_build_object(
        'experiment', jsonb_build_object('id', 'weather_forward_2026_09_v1'),
        'timestamp', clock_timestamp()
    ));
    if weather_scheduler.dispatch('weather_cycle.yml', true) is not null
       or not exists (select 1 from weather_scheduler.dispatch_audit
                      where reason = 'CURRENT_HOUR_CYCLE_RECORDED') then
        raise exception 'CURRENT_HOUR_REPORT_DID_NOT_SUPPRESS_WATCHDOG';
    end if;

    request_two := weather_scheduler.dispatch('clv_checkpoints.yml');
    if request_two is null or weather_scheduler.dispatch('clv_checkpoints.yml') is not null then
        raise exception 'CLV_SLOT_NOT_IDEMPOTENT';
    end if;
    insert into net._http_response (id, status_code, content, timed_out, created)
    values (request_one, 200, '{"workflow_run_id":123456789}', false, clock_timestamp()),
           (request_two, 401, '{"message":"synthetic rejection"}', false, clock_timestamp());
    recorded := weather_scheduler.capture_responses();
    if recorded <> 2 or not exists (
        select 1 from weather_scheduler.dispatch_audit where request_id = request_one
          and state = 'accepted' and reason = 'ACCEPTED_NOT_COMPLETED'
          and github_run_id = 123456789
          and github_run_url = 'https://github.com/epeters6/sportspick-wc2026/actions/runs/123456789'
    ) or not exists (
        select 1 from weather_scheduler.dispatch_audit where request_id = request_two
          and state = 'http_error' and http_status = 401 and github_run_id is null
    ) then
        raise exception 'HTTP_RECEIPTS_NOT_CAPTURED_HONESTLY';
    end if;
    if exists (select 1 from weather_scheduler.dispatch_audit a
               where row_to_json(a)::text like '%scheduler_test_credential%') then
        raise exception 'CREDENTIAL_LEAKED_INTO_AUDIT';
    end if;
    if weather_scheduler.capture_responses() <> 0 then
        raise exception 'RECEIPTS_PROCESSED_TWICE';
    end if;
    begin
        perform weather_scheduler.dispatch('unapproved.yml');
        raise exception 'ALLOWLIST_TEST_FAILED';
    exception when raise_exception then
        if sqlerrm <> 'WORKFLOW_NOT_ALLOWLISTED' then raise; end if;
    end;
end;
$tests$;
select 'rollback tests passed; no HTTP requests committed' as result;
rollback;
