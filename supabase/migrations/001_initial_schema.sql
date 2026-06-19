-- ═══════════════════════════════════════════════════════════════════════════
-- Sports Pick Tracker — Initial Schema
-- ═══════════════════════════════════════════════════════════════════════════

-- ─── Extensions ─────────────────────────────────────────────────────────────
create extension if not exists "uuid-ossp";
create extension if not exists "pg_trgm"; -- fuzzy text search

-- ─── Enums ──────────────────────────────────────────────────────────────────
create type platform_type as enum ('twitter', 'tiktok', 'instagram', 'youtube');
create type pick_outcome as enum ('pending', 'correct', 'incorrect', 'void');
create type sport_type as enum ('football', 'basketball', 'baseball', 'nfl', 'nhl', 'stocks');

-- ─── influencers ─────────────────────────────────────────────────────────────
create table influencers (
    id              uuid primary key default uuid_generate_v4(),
    platform        platform_type not null,
    handle          text not null,
    display_name    text,
    profile_url     text,
    follower_count  bigint default 0,
    bio             text,
    avatar_url      text,
    is_active       boolean default true,
    added_at        timestamptz default now(),
    last_scraped_at timestamptz,

    -- ML ranking fields
    elo_score       float default 1000.0,
    accuracy_rate   float default 0.0,   -- 0.0 – 1.0
    total_picks     int default 0,
    correct_picks   int default 0,
    pick_streak     int default 0,       -- current win/loss streak
    consensus_score float default 0.0,   -- how often agrees with the crowd

    unique (platform, handle)
);

-- ─── matches ─────────────────────────────────────────────────────────────────
create table matches (
    id              uuid primary key default uuid_generate_v4(),
    sport           sport_type not null default 'football',
    tournament      text not null,       -- e.g. "FIFA World Cup 2026"
    home_team       text not null,
    away_team       text not null,
    scheduled_at    timestamptz not null,
    finished_at     timestamptz,
    home_score      int,
    away_score      int,
    winner          text,                -- home_team | away_team | 'draw'
    external_id     text unique,         -- ID from the sports API
    stage           text,                -- Group Stage / Quarterfinal etc.
    venue           text,
    is_final        boolean default false
);

-- ─── picks ───────────────────────────────────────────────────────────────────
create table picks (
    id              uuid primary key default uuid_generate_v4(),
    influencer_id   uuid not null references influencers(id) on delete cascade,
    match_id        uuid references matches(id) on delete set null,
    platform        platform_type not null,
    post_url        text,
    post_id         text,
    raw_text        text not null,
    predicted_winner text,               -- extracted prediction
    predicted_score  text,               -- e.g. "3-1"
    confidence      float,              -- 0.0 – 1.0 extracted or model-assigned
    outcome         pick_outcome default 'pending',
    posted_at       timestamptz,
    scraped_at      timestamptz default now(),
    resolved_at     timestamptz,

    unique (platform, post_id)
);

-- ─── influencer_stats snapshot (materialised daily) ─────────────────────────
create table influencer_stats_history (
    id              uuid primary key default uuid_generate_v4(),
    influencer_id   uuid not null references influencers(id) on delete cascade,
    snapshot_date   date not null default current_date,
    elo_score       float,
    accuracy_rate   float,
    total_picks     int,
    correct_picks   int,
    elo_rank        int,
    accuracy_rank   int,

    unique (influencer_id, snapshot_date)
);

-- ─── consensus_picks (aggregated recommendations) ───────────────────────────
create table consensus_picks (
    id              uuid primary key default uuid_generate_v4(),
    match_id        uuid not null references matches(id) on delete cascade,
    predicted_winner text not null,
    total_votes     int default 0,
    weighted_score  float default 0.0,  -- Elo-weighted vote share
    confidence      float default 0.0,  -- final recommendation confidence
    top_influencers uuid[],             -- top agreeing influencers
    generated_at    timestamptz default now(),

    unique (match_id, predicted_winner)
);

-- ─── Indexes ────────────────────────────────────────────────────────────────
create index on influencers (elo_score desc);
create index on influencers (accuracy_rate desc);
create index on influencers (platform);
create index on picks (influencer_id);
create index on picks (match_id);
create index on picks (outcome);
create index on picks (scraped_at desc);
create index on matches (scheduled_at);
create index on matches (tournament);
create index on influencer_stats_history (snapshot_date desc);

-- ─── Row Level Security ─────────────────────────────────────────────────────
alter table influencers             enable row level security;
alter table matches                 enable row level security;
alter table picks                   enable row level security;
alter table influencer_stats_history enable row level security;
alter table consensus_picks         enable row level security;

-- Public read access for the dashboard (anon key can read)
create policy "public_read_influencers"
    on influencers for select using (true);

create policy "public_read_matches"
    on matches for select using (true);

create policy "public_read_picks"
    on picks for select using (true);

create policy "public_read_stats"
    on influencer_stats_history for select using (true);

create policy "public_read_consensus"
    on consensus_picks for select using (true);

-- Service role can do everything (backend writes using service_role key)
create policy "service_role_all_influencers"
    on influencers for all using (auth.role() = 'service_role');

create policy "service_role_all_matches"
    on matches for all using (auth.role() = 'service_role');

create policy "service_role_all_picks"
    on picks for all using (auth.role() = 'service_role');

create policy "service_role_all_stats"
    on influencer_stats_history for all using (auth.role() = 'service_role');

create policy "service_role_all_consensus"
    on consensus_picks for all using (auth.role() = 'service_role');
