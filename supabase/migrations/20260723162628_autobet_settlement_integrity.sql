-- Phase 4c-A: exact-match settlement identity and immutable correction audit.

alter table public.autobets
    add column if not exists event_date date,
    add column if not exists strategy text,
    add column if not exists settlement_version text,
    add column if not exists settlement_match_id uuid
        references public.matches(id) on delete set null,
    add column if not exists settlement_corrected_at timestamptz;

create index if not exists autobets_event_date_idx
    on public.autobets(event_date);

create index if not exists autobets_strategy_idx
    on public.autobets(strategy);

create index if not exists autobets_settlement_version_idx
    on public.autobets(settlement_version);

-- event_date is the event's calendar date, never the row's settlement date.
update public.autobets a
set event_date =
    case
        when a.sport = 'weather'
             and a.metadata->>'target_date' is not null
            then (a.metadata->>'target_date')::date
        when a.match_id is not null
            then (
                select (m.scheduled_at at time zone 'America/New_York')::date
                from public.matches m
                where m.id = a.match_id
            )
        else null
    end
where a.event_date is null;

update public.autobets
set strategy =
    case
        when sport = 'weather'
            then 'weather_' || coalesce(metadata->>'metric', 'high')
        when sport ilike 'mlb%'
            then 'legacy_consensus_mlb'
        when sport ilike '%football%' or sport ilike '%soccer%'
            then 'legacy_consensus_football'
        else 'legacy_other'
    end
where strategy is null;

create table if not exists public.autobet_settlement_audit (
    id uuid primary key default uuid_generate_v4(),
    autobet_id uuid not null
        references public.autobets(id) on delete cascade,
    correction_version text not null,
    action text not null,
    reason text not null,
    prior_status text,
    corrected_status text,
    prior_pnl double precision,
    corrected_pnl double precision,
    match_id uuid references public.matches(id) on delete set null,
    external_id text,
    scheduled_at timestamptz,
    winner text,
    details jsonb not null default '{}'::jsonb,
    attempted_at timestamptz not null default now(),
    applied_at timestamptz,
    unique (autobet_id, correction_version)
);

create index if not exists autobet_settlement_audit_bet_idx
    on public.autobet_settlement_audit(autobet_id);

alter table public.autobet_settlement_audit enable row level security;

revoke all on public.autobet_settlement_audit
from anon, authenticated;

grant all on public.autobet_settlement_audit
to service_role;
