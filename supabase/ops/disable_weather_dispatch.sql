-- Stop only this dispatcher; retain audit evidence and leave legacy jobs unchanged.
-- Already accepted GitHub runs are not cancelled by disabling future cron ticks.
begin;
select cron.alter_job(job_id := jobid, active := false)
from cron.job where jobname in (
    'weather-github-hourly', 'weather-github-watchdog', 'weather-github-clv', 'weather-github-receipts'
);
commit;
