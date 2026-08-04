-- Calibration v2 evaluation support + durable one-minute CLV scheduler.
-- Required Vault secrets: clv_project_url and clv_publishable_key.
-- The function validates the publishable key itself and is deployed with
-- platform verify_jwt disabled because modern API keys are not JWTs.

create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net;

grant usage on schema cron to postgres;
grant all privileges on all tables in schema cron to postgres;

alter table public.model_predictions enable row level security;

drop policy if exists "service_role_all_model_predictions" on public.model_predictions;
create policy "service_role_all_model_predictions"
    on public.model_predictions
    for all
    to service_role
    using (true)
    with check (true);

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
                where name = 'clv_publishable_key'
            )
        ),
        body := jsonb_build_object('scheduled_at', now()),
        timeout_milliseconds := 15000
    ) as request_id
    where exists (
        select 1 from vault.decrypted_secrets where name = 'clv_project_url'
    )
      and exists (
        select 1 from vault.decrypted_secrets where name = 'clv_publishable_key'
    );
    $job$
);
