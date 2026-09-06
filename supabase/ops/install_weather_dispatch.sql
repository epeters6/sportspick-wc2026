-- Run as postgres. Installs INACTIVE jobs; activate_weather_dispatch.sql is separate.
-- No credential is embedded here. Provision Vault secret weather_github_actions_token
-- with access to this repository only and Actions:write before activation.
begin;

create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net;

-- pg_net's queue contains outbound Authorization headers. Extension defaults can
-- expose it to application roles even though net is not a Data API schema.
-- These privileges must be removed BEFORE the dedicated token is provisioned.
-- Existing postgres-owned cron jobs retain access; no legacy job is reactivated.
-- Preserve generic net schema/functions/response access for existing consumers.
-- Queue UPDATE is also dangerous: it could redirect a queued authenticated call.
revoke all on net.http_request_queue from public, anon, authenticated, service_role;
revoke all on vault.decrypted_secrets from public, anon, authenticated, service_role;
revoke all on vault.secrets from public, anon, authenticated, service_role;
-- Supabase extension objects may be owned by supabase_admin, so do not rely on
-- the PUBLIC grants just removed to give postgres its runtime access.
grant usage on schema net, vault to postgres;
grant select on net._http_response, vault.decrypted_secrets to postgres;
grant execute on function net.http_post(text, jsonb, jsonb, jsonb, integer) to postgres;
do $storage_privileges$
declare
    role_name text;
begin
    foreach role_name in array array['anon', 'authenticated', 'service_role'] loop
        if has_table_privilege(role_name, 'net.http_request_queue', 'select,insert,update,delete,truncate,references,trigger')
           or has_table_privilege(role_name, 'vault.decrypted_secrets', 'select,insert,update,delete,truncate,references,trigger')
           or has_table_privilege(role_name, 'vault.secrets', 'select,insert,update,delete,truncate,references,trigger') then
            raise exception 'APPLICATION_ROLE_CAN_READ_DISPATCH_CREDENTIAL_STORAGE';
        end if;
    end loop;
end;
$storage_privileges$;

create schema if not exists weather_scheduler authorization postgres;
revoke all on schema weather_scheduler from public, anon, authenticated, service_role;
alter default privileges for role postgres in schema weather_scheduler
    revoke all on tables from public, anon, authenticated, service_role;
alter default privileges for role postgres in schema weather_scheduler
    revoke all on sequences from public, anon, authenticated, service_role;
alter default privileges for role postgres in schema weather_scheduler
    revoke execute on functions from public, anon, authenticated, service_role;

create table if not exists weather_scheduler.dispatch_audit (
    id bigint generated always as identity primary key,
    workflow text not null check (workflow in ('weather_cycle.yml', 'clv_checkpoints.yml')),
    slot_start timestamptz not null,
    attempt text not null check (attempt in ('primary', 'watchdog', 'checkpoint')),
    requested_at timestamptz not null default clock_timestamp(),
    request_id bigint unique,
    state text not null check (state in (
        'queued', 'accepted', 'http_error', 'transport_error', 'response_missing',
        'missing_credential', 'enqueue_failed', 'skipped'
    )),
    reason text,
    http_status integer,
    response_at timestamptz,
    recorded_at timestamptz,
    github_run_id bigint,
    github_run_url text,
    unique (workflow, slot_start, attempt)
);
alter table weather_scheduler.dispatch_audit owner to postgres;
alter table weather_scheduler.dispatch_audit enable row level security;
create index if not exists dispatch_audit_pending
    on weather_scheduler.dispatch_audit (request_id)
    where state in ('queued', 'response_missing');
create index if not exists dispatch_audit_requested
    on weather_scheduler.dispatch_audit (requested_at desc);

-- Persist only allowlisted receipt fields before pg_net expires its response rows.
-- A 200/204 receipt establishes acceptance, never successful workflow execution.
create or replace function weather_scheduler.capture_responses()
returns integer
language plpgsql security invoker set search_path = pg_catalog
as $function$
declare
    receipt record;
    payload jsonb;
    run_id bigint;
    count_recorded integer := 0;
begin
    for receipt in
        select a.id as audit_id, r.status_code, r.created, r.timed_out,
               r.error_msg is not null as has_error, r.content
        from weather_scheduler.dispatch_audit a
        join net._http_response r on r.id = a.request_id
        where a.state in ('queued', 'response_missing')
    loop
        run_id := null;
        if receipt.status_code = 200 and not coalesce(receipt.timed_out, false)
           and not receipt.has_error and length(receipt.content) <= 65536 then
            begin
                payload := receipt.content::jsonb;
                if payload->>'workflow_run_id' ~ '^[1-9][0-9]{0,18}$' then
                    run_id := (payload->>'workflow_run_id')::bigint;
                end if;
            exception when others then
                run_id := null;
            end;
        end if;
        update weather_scheduler.dispatch_audit
        set state = case
                when coalesce(receipt.timed_out, false) or receipt.has_error then 'transport_error'
                when receipt.status_code in (200, 204) then 'accepted'
                else 'http_error' end,
            reason = case
                when coalesce(receipt.timed_out, false) then 'HTTP_TIMEOUT'
                when receipt.has_error then 'HTTP_TRANSPORT_ERROR'
                when receipt.status_code = 200 and run_id is null then 'ACCEPTED_WITHOUT_RUN_ID'
                when receipt.status_code = 204 then 'ACCEPTED_WITHOUT_RUN_ID'
                when receipt.status_code = 200 then 'ACCEPTED_NOT_COMPLETED'
                else 'HTTP_REJECTED' end,
            http_status = receipt.status_code,
            response_at = receipt.created,
            recorded_at = clock_timestamp(),
            github_run_id = run_id,
            github_run_url = case when run_id is not null then
                'https://github.com/epeters6/sportspick-wc2026/actions/runs/' || run_id::text end
        where id = receipt.audit_id;
        count_recorded := count_recorded + 1;
    end loop;
    -- A missing response is ambiguous; it must not be reported as successful.
    update weather_scheduler.dispatch_audit
    set state = 'response_missing', reason = 'NO_HTTP_RESPONSE', recorded_at = clock_timestamp()
    where state = 'queued' and requested_at < clock_timestamp() - interval '5 minutes';
    return count_recorded;
end;
$function$;

create or replace function weather_scheduler.dispatch(p_workflow text, p_watchdog boolean default false)
returns bigint
language plpgsql security invoker set search_path = pg_catalog
as $function$
declare
    dispatch_time timestamptz := clock_timestamp();
    slot timestamptz;
    attempt_name text;
    audit_id bigint;
    network_id bigint;
    endpoint text;
    token text;
    token_count integer;
    latest jsonb;
    latest_time timestamptz;
    skip_reason text;
begin
    if p_workflow is null or p_workflow not in ('weather_cycle.yml', 'clv_checkpoints.yml')
       or p_watchdog is null or (p_workflow = 'clv_checkpoints.yml' and p_watchdog) then
        raise exception 'WORKFLOW_NOT_ALLOWLISTED';
    end if;
    perform weather_scheduler.capture_responses();
    if p_workflow = 'weather_cycle.yml' then
        slot := date_trunc('hour', dispatch_time at time zone 'UTC') at time zone 'UTC';
        attempt_name := case when p_watchdog then 'watchdog' else 'primary' end;
        endpoint := 'https://api.github.com/repos/epeters6/sportspick-wc2026/actions/workflows/weather_cycle.yml/dispatches';
    else
        slot := to_timestamp(floor(extract(epoch from dispatch_time) / 300) * 300);
        attempt_name := 'checkpoint';
        endpoint := 'https://api.github.com/repos/epeters6/sportspick-wc2026/actions/workflows/clv_checkpoints.yml/dispatches';
    end if;

    -- The unique slot claim is atomic across duplicate/concurrent cron invocations.
    insert into weather_scheduler.dispatch_audit (workflow, slot_start, attempt, requested_at, state)
    values (p_workflow, slot, attempt_name, dispatch_time, 'queued')
    on conflict (workflow, slot_start, attempt) do nothing
    returning id into audit_id;
    if audit_id is null then
        return null;
    end if;

    if p_watchdog then
        if exists (
            select 1 from public.app_settings
            where key = 'weather_forecast_slot:weather_forward_2026_09_v1:' ||
                to_char(slot at time zone 'UTC', 'YYYYMMDD"T"HH24')
        ) then
            skip_reason := 'FORECAST_SLOT_ALREADY_ATTEMPTED';
        else
            select value into latest from public.app_settings where key = 'weather_cycle_latest';
            begin
                latest_time := (latest->>'timestamp')::timestamptz;
            exception when others then
                latest_time := null;
            end;
            if latest #>> '{experiment,id}' = 'weather_forward_2026_09_v1'
               and latest_time >= slot and latest_time <= dispatch_time then
                skip_reason := 'CURRENT_HOUR_CYCLE_RECORDED';
            end if;
        end if;
        -- Never create an immediate duplicate when a primary request is recent.
        if skip_reason is null and exists (
            select 1 from weather_scheduler.dispatch_audit
            where workflow = p_workflow and slot_start = slot and attempt = 'primary'
              and requested_at > dispatch_time - interval '15 minutes'
        ) then
            skip_reason := 'PRIMARY_REQUEST_RECENT';
        end if;
        if skip_reason is not null then
            update weather_scheduler.dispatch_audit set state = 'skipped', reason = skip_reason
            where id = audit_id;
            return null;
        end if;
    end if;

    select count(*), max(decrypted_secret) into token_count, token
    from vault.decrypted_secrets where name = 'weather_github_actions_token';
    if token_count <> 1 or token is null or length(token) < 20 or length(token) > 4096
       or token ~ '[[:space:]]' then
        token := null;
        update weather_scheduler.dispatch_audit
        set state = 'missing_credential', reason = 'MISSING_OR_INVALID_VAULT_CREDENTIAL'
        where id = audit_id;
        return null;
    end if;

    begin
        network_id := net.http_post(
            url := endpoint,
            headers := jsonb_build_object(
                'Accept', 'application/vnd.github+json',
                'Content-Type', 'application/json',
                'Authorization', 'Bearer ' || token,
                'User-Agent', 'sportspick-weather-paper-scheduler',
                'X-GitHub-Api-Version', '2026-03-10'
            ),
            body := jsonb_build_object(
                'ref', 'main',
                'inputs', jsonb_build_object('scheduled_for',
                    to_char(dispatch_time at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
            ),
            timeout_milliseconds := 15000
        );
        if network_id is null then
            raise exception 'NO_REQUEST_ID';
        end if;
        update weather_scheduler.dispatch_audit set request_id = network_id where id = audit_id;
    exception when others then
        -- Never copy SQLERRM, HTTP headers, request bodies or credentials to logs.
        update weather_scheduler.dispatch_audit
        set state = 'enqueue_failed', reason = 'ENQUEUE_SQLSTATE_' || sqlstate
        where id = audit_id;
        network_id := null;
    end;
    token := null;
    return network_id;
end;
$function$;

alter function weather_scheduler.capture_responses() owner to postgres;
alter function weather_scheduler.dispatch(text, boolean) owner to postgres;
revoke all on all tables in schema weather_scheduler from public, anon, authenticated, service_role;
revoke all on all sequences in schema weather_scheduler from public, anon, authenticated, service_role;
revoke all on all functions in schema weather_scheduler from public, anon, authenticated, service_role;

-- These names only belong to this new dispatcher. Existing cron jobs are untouched.
select cron.schedule('weather-github-hourly', '7 * * * *',
    $$select weather_scheduler.dispatch('weather_cycle.yml');$$);
select cron.schedule('weather-github-watchdog', '27 * * * *',
    $$select weather_scheduler.dispatch('weather_cycle.yml', true);$$);
select cron.schedule('weather-github-clv', '2-57/5 * * * *',
    $$select weather_scheduler.dispatch('clv_checkpoints.yml');$$);
select cron.schedule('weather-github-receipts', '* * * * *',
    $$select weather_scheduler.capture_responses();$$);
select cron.alter_job(job_id := jobid, active := false)
from cron.job where jobname in (
    'weather-github-hourly', 'weather-github-watchdog', 'weather-github-clv', 'weather-github-receipts'
);

commit;
