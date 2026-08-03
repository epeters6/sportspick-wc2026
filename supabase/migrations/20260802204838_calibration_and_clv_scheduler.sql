-- Calibration v2 evaluation support + durable one-minute CLV scheduler.
-- Required Vault secrets: clv_project_url, clv_anon_key, and
-- clv_scheduler_secret. Set the same scheduler secret as CLV_SCHEDULER_SECRET
-- in the Edge Function environment.

create extension if not exists pg_cron;
create extension if not exists pg_net with schema extensions;

alter table public.model_predictions enable row level security;

drop policy if exists "service_role_all_model_predictions" on public.model_predictions;
create policy "service_role_all_model_predictions"
    on public.model_predictions
    for all
    to service_role
    using (true)
    with check (true);

drop policy if exists "public_read_model_predictions" on public.model_predictions;
create policy "public_read_model_predictions"
    on public.model_predictions
    for select
    to anon, authenticated
    using (true);

create index if not exists idx_model_predictions_source_event
    on public.model_predictions (source, event_key);
create index if not exists idx_model_predictions_unresolved
    on public.model_predictions (source, resolved_at)
    where resolved_at is null;

select cron.schedule(
    'clv-checkpoints-edge-v2',
    '* * * * *',
    $job$
    select net.http_post(
        url := (
            select decrypted_secret
            from vault.decrypted_secrets
            where name = 'clv_project_url'
        ) || '/functions/v1/clv-checkpoints',
        headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'apikey', (
                select decrypted_secret
                from vault.decrypted_secrets
                where name = 'clv_anon_key'
            ),
            'Authorization', 'Bearer ' || (
                select decrypted_secret
                from vault.decrypted_secrets
                where name = 'clv_anon_key'
            ),
            'x-clv-scheduler-secret', (
                select decrypted_secret
                from vault.decrypted_secrets
                where name = 'clv_scheduler_secret'
            )
        ),
        body := jsonb_build_object('scheduled_at', now()),
        timeout_milliseconds := 15000
    ) as request_id
    where exists (
        select 1 from vault.decrypted_secrets where name = 'clv_project_url'
    )
      and exists (
        select 1 from vault.decrypted_secrets where name = 'clv_anon_key'
    )
      and exists (
        select 1 from vault.decrypted_secrets where name = 'clv_scheduler_secret'
    );
    $job$
);
