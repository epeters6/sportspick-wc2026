-- Run as postgres on the existing installed dispatcher. Safe to apply repeatedly.
-- Replaces only dispatch(text,boolean); existing ownership and ACLs are preserved.
-- Does not alter cron jobs, audit history, credentials, grants or shared extensions.
-- Do not rerun install_weather_dispatch.sql against an active scheduler.
begin;

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
    active_experiment_id text;
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
        -- Read the baseline identity once. Experiment versions may advance while
        -- historical claims remain immutable; never consult another version's slot.
        select value into latest from public.app_settings where key = 'weather_cycle_latest';
        active_experiment_id := latest #>> '{experiment,id}';
        if active_experiment_id is null or active_experiment_id !~
            '^weather_forward_[0-9]{4}_(0[1-9]|1[0-2])_v[1-9][0-9]*$' then
            -- Primary delivery still bootstraps a missing report. The watchdog
            -- cannot safely determine duplicate claims without a valid identity.
            skip_reason := 'ACTIVE_WEATHER_EXPERIMENT_UNKNOWN';
        elsif exists (
            select 1 from public.app_settings
            where key = 'weather_forecast_slot:' || active_experiment_id || ':' ||
                to_char(slot at time zone 'UTC', 'YYYYMMDD"T"HH24')
        ) then
            skip_reason := 'FORECAST_SLOT_ALREADY_ATTEMPTED';
        else
            begin
                latest_time := (latest->>'timestamp')::timestamptz;
            exception when others then
                latest_time := null;
            end;
            if latest_time >= slot and latest_time <= dispatch_time then
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

commit;
