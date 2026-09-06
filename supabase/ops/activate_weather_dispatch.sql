-- Run as postgres after installing, provisioning the dedicated Vault secret,
-- and merging the workflows' scheduled_for input and execution-time validation.
-- External prerequisite: verify net/vault/weather_scheduler API profiles reject
-- access and no exposed RPC/view grants arbitrary SQL or secret storage access.
begin;
do $activation$
declare
    jobs integer;
    credentials integer;
    role_name text;
begin
    if exists (select 1 from pg_catalog.pg_roles
               where rolname in ('anon', 'authenticated') and rolcanlogin) then
        raise exception 'UNTRUSTED_API_ROLES_MUST_HAVE_NOLOGIN';
    end if;
    foreach role_name in array array['anon', 'authenticated', 'service_role'] loop
        if has_schema_privilege(role_name, 'weather_scheduler', 'usage')
           or has_function_privilege(role_name, 'weather_scheduler.dispatch(text,boolean)', 'execute')
           or has_function_privilege(role_name, 'weather_scheduler.capture_responses()', 'execute')
           or has_table_privilege(role_name, 'weather_scheduler.dispatch_audit',
                'select,insert,update,delete,truncate,references,trigger') then
            raise exception 'DISPATCH_PRIVILEGE_HARDENING_REQUIRED';
        end if;
    end loop;
    select count(*) into credentials from vault.decrypted_secrets
    where name = 'weather_github_actions_token'
      and length(decrypted_secret) between 20 and 4096
      and decrypted_secret !~ '[[:space:]]';
    if credentials <> 1 then
        raise exception 'DEDICATED_GITHUB_VAULT_CREDENTIAL_REQUIRED';
    end if;
    select count(*) into jobs from cron.job
    where jobname in ('weather-github-hourly', 'weather-github-watchdog',
                     'weather-github-clv', 'weather-github-receipts')
      and username = 'postgres';
    if jobs <> 4 then
        raise exception 'EXPECTED_FOUR_POSTGRES_OWNED_SCHEDULER_JOBS';
    end if;
end;
$activation$;
select cron.alter_job(job_id := jobid, active := true)
from cron.job where jobname in (
    'weather-github-hourly', 'weather-github-watchdog', 'weather-github-clv', 'weather-github-receipts'
);
commit;
