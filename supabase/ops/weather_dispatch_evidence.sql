-- Run as postgres. No token, headers, raw HTTP response or request body is selected.
-- Enqueue success, HTTP acceptance, and completed GitHub execution are distinct.
select jobname, schedule, active, username from cron.job
where jobname in ('weather-github-hourly', 'weather-github-watchdog',
                 'weather-github-clv', 'weather-github-receipts')
order by jobname;

select id, workflow, slot_start, attempt, requested_at, request_id, state, reason,
       http_status, response_at, recorded_at, github_run_id, github_run_url
from weather_scheduler.dispatch_audit
order by requested_at desc limit 100;

select j.jobname, d.start_time, d.end_time, d.status
from cron.job_run_details d join cron.job j on j.jobid = d.jobid
where j.jobname in ('weather-github-hourly', 'weather-github-watchdog',
                   'weather-github-clv', 'weather-github-receipts')
order by d.start_time desc limit 40;

select n.nspname, p.proname, p.prosecdef as security_definer,
       has_function_privilege('anon', p.oid, 'execute') as anon_execute,
       has_function_privilege('authenticated', p.oid, 'execute') as authenticated_execute,
       has_function_privilege('service_role', p.oid, 'execute') as service_role_execute
from pg_proc p join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'weather_scheduler';

select role_name,
       has_schema_privilege(role_name, 'net', 'usage') as net_usage,
       has_table_privilege(role_name, 'net.http_request_queue', 'select') as queue_select,
       has_table_privilege(role_name, 'vault.decrypted_secrets', 'select') as vault_decrypted_select
from unnest(array['anon', 'authenticated', 'service_role']) as roles(role_name);
