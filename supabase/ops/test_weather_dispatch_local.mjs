// Run: node supabase/ops/test_weather_dispatch_local.mjs <isolated npm directory>
// Requires @electric-sql/pglite in that directory. No Supabase/network connection.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const requireRuntime = createRequire(pathToFileURL(resolve(process.argv[2], 'package.json')));
const { PGlite } = requireRuntime('@electric-sql/pglite');
const directory = dirname(fileURLToPath(import.meta.url));
const db = new PGlite();
const load = (name) => readFile(resolve(directory, name), 'utf8');
let checks = 0;
const check = (condition, description) => {
  assert.ok(condition, description);
  checks += 1;
  console.log(`PASS ${description}`);
};
const scalar = async (sql) => Object.values((await db.query(sql)).rows[0])[0];
async function expectRejected(sql, expected) {
  let rejection = '';
  try { await db.exec(sql); } catch (error) { rejection = error.message; }
  await db.exec('rollback');
  check(rejection.includes(expected), expected);
}

try {
  // Real PostgreSQL executes the production function bodies. Only the unavailable
  // hosted extensions/services are stubbed: HTTP is a table insert, cron is a table.
  await db.exec(`
    create role anon;
    create role authenticated;
    create role service_role bypassrls;
    create schema net;
    create schema vault;
    create schema cron;
    create table public.app_settings (key text primary key, value jsonb not null,
      updated_at timestamptz default now());
    create table net.http_request_queue (id bigserial primary key, url text,
      body jsonb, headers jsonb);
    create table net._http_response (id bigint, status_code integer, content_type text,
      headers jsonb, content text, timed_out boolean, error_msg text,
      created timestamptz not null default now());
    create function net.http_post(url text, body jsonb default '{}',
      params jsonb default '{}', headers jsonb default '{}', timeout_milliseconds int default 2000)
      returns bigint language plpgsql security definer set search_path = pg_catalog as $$
      declare request_id bigint;
      begin
        insert into net.http_request_queue(url, body, headers)
          values (url, body, headers) returning id into request_id;
        return request_id;
      end; $$;
    create table vault.secrets (id bigserial primary key, name text unique, secret text);
    create view vault.decrypted_secrets as select id, name, secret as decrypted_secret from vault.secrets;
    create function vault.create_secret(secret text, name text) returns bigint language plpgsql as $$
      declare secret_id bigint;
      begin insert into vault.secrets(secret, name) values (secret, name) returning id into secret_id;
        return secret_id; end; $$;
    grant usage on schema net, vault to public;
    grant all on all tables in schema net to public;
    grant all on all tables in schema vault to service_role;
    create table cron.job (jobid bigserial primary key, jobname text unique,
      schedule text, command text, username text default current_user, active boolean default true);
    create table cron.job_run_details (jobid bigint, start_time timestamptz,
      end_time timestamptz, status text);
    create function cron.schedule(job_name text, job_schedule text, job_command text)
      returns bigint language plpgsql as $$
      declare result bigint;
      begin insert into cron.job(jobname, schedule, command)
        values (job_name, job_schedule, job_command)
        on conflict (jobname) do update set schedule=excluded.schedule, command=excluded.command
        returning jobid into result; return result; end; $$;
    create function cron.alter_job(job_id bigint, active boolean)
      returns void language sql as $$update cron.job set active=$2 where jobid=$1$$;
  `);
  const install = (await load('install_weather_dispatch.sql'))
    .replace(/^create extension if not exists pg_cron with schema pg_catalog;\r?$/m, '')
    .replace(/^create extension if not exists pg_net;\r?$/m, '');
  await db.exec(install);
  await db.exec(install);
  check(await scalar('select count(*)::int from cron.job') === 4, 'install/reinstall produces exactly four jobs');
  check(await scalar('select count(*)::int from cron.job where active') === 0, 'install/reinstall keeps every job inactive');
  check(await scalar('select count(*)::int from net.http_request_queue') === 0, 'installation sends no HTTP requests');
  check(await scalar(`select bool_and(not has_schema_privilege(r, 'weather_scheduler', 'usage')
      and not has_function_privilege(r, 'weather_scheduler.dispatch(text,boolean)', 'execute')
      and not has_table_privilege(r, 'net.http_request_queue', 'select,insert,update,delete,truncate,references,trigger')
      and not has_table_privilege(r, 'vault.decrypted_secrets', 'select,insert,update,delete,truncate,references,trigger'))
      from unnest(array['anon','authenticated','service_role']) as roles(r)`), 'application roles cannot access dispatcher or credential storage');
  check(await scalar(`select bool_and(has_schema_privilege(r, 'net', 'usage')
      and has_function_privilege(r, 'net.http_post(text,jsonb,jsonb,jsonb,integer)', 'execute')
      and has_table_privilege(r, 'net._http_response', 'select'))
      from unnest(array['anon','authenticated','service_role']) as roles(r)`), 'generic pg_net usage/functions/responses remain available');
  check(await scalar(`select bool_and(not prosecdef) from pg_proc p join pg_namespace n
      on n.oid=p.pronamespace where n.nspname='weather_scheduler'`), 'scheduler functions are SECURITY INVOKER');

  const activate = await load('activate_weather_dispatch.sql');
  await expectRejected(activate, 'DEDICATED_GITHUB_VAULT_CREDENTIAL_REQUIRED');
  const results = await db.exec(await load('test_weather_dispatch_rollback.sql'));
  check(results.some(result => result.rows.some(row => row.result?.startsWith('rollback tests passed'))),
    'unmodified rollback SQL passes missing-token, duplicate, watchdog, receipt and allowlist assertions');
  check(await scalar('select count(*)::int from net.http_request_queue') === 0, 'rollback leaves zero network queue rows');
  check(await scalar('select count(*)::int from weather_scheduler.dispatch_audit') === 0, 'rollback restores audit state');
  check(await scalar('select count(*)::int from vault.secrets') === 0, 'rollback removes the synthetic credential');

  await db.exec(`select vault.create_secret('local_test_credential_not_real_000000', 'weather_github_actions_token');`);
  await db.exec(activate);
  check(await scalar('select count(*)::int from cron.job where active') === 4, 'activation enables four jobs with a valid test credential');
  await db.exec(await load('disable_weather_dispatch.sql'));
  check(await scalar('select count(*)::int from cron.job where active') === 0, 'disable stops only the new jobs');
  await db.exec('grant update on net.http_request_queue to anon');
  await expectRejected(activate, 'DISPATCH_PRIVILEGE_HARDENING_REQUIRED');
  await db.exec('revoke update on net.http_request_queue from anon');
  await db.exec(await load('weather_dispatch_evidence.sql'));
  check(true, 'evidence queries execute without credentials or raw request fields');
  console.log(`Completed ${checks} checks using ${await scalar('select version()')}. HTTP and cron are local stubs.`);
} catch (error) {
  console.error(`FAIL ${error.message}`);
  process.exitCode = 1;
} finally {
  await db.close();
}
