-- Phase 4c-B: durable sports-shadow identity, probabilities, and outcomes.

alter table public.clv_obligations
    add column if not exists event_id text,
    add column if not exists event_start timestamptz,
    add column if not exists model_prob double precision,
    add column if not exists market_prob double precision,
    add column if not exists selected_team text,
    add column if not exists home_team text,
    add column if not exists away_team text,
    add column if not exists match_id uuid
        references public.matches(id) on delete set null,
    add column if not exists game_pk bigint,
    add column if not exists shares double precision,
    add column if not exists stake double precision,
    add column if not exists settlement_status text
        not null default 'pending',
    add column if not exists settlement_result boolean,
    add column if not exists settlement_pnl double precision,
    add column if not exists settled_at timestamptz,
    add column if not exists settlement_source text;

alter table public.clv_obligations
    add constraint clv_model_prob_bounds
        check (model_prob is null or model_prob between 0 and 1),
    add constraint clv_market_prob_bounds
        check (market_prob is null or market_prob between 0 and 1),
    add constraint clv_settlement_status_check
        check (
            settlement_status in
            ('pending', 'won', 'lost', 'void', 'unavailable')
        );

-- Cover the Phase 4c foreign keys flagged by the database advisor.
create index if not exists autobets_settlement_match_id_idx
    on public.autobets(settlement_match_id);

create index if not exists autobet_settlement_audit_match_id_idx
    on public.autobet_settlement_audit(match_id);

create index if not exists clv_obligations_match_id_idx
    on public.clv_obligations(match_id);
