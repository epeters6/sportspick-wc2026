from pydantic_settings import BaseSettings
from pydantic import field_validator
from functools import lru_cache


class Settings(BaseSettings):
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str

    twitter_auth_token: str = ""
    twitter_ct0: str = ""

    tiktok_session_id: str = ""
    tiktok_ms_token: str = ""

    instagram_username: str = ""
    instagram_password: str = ""

    youtube_api_key: str = ""

    wc_api_key: str = ""
    wc_api_base: str = "https://api.wc2026api.com/v1"

    discord_webhook_url: str = ""

    # ─── Polymarket autobet ──────────────────────────────────────────────────
    # SAFETY: live trading is OFF by default. The system runs in paper mode
    # (recording bets against REAL market prices) until you explicitly opt in.
    polymarket_live_enabled: bool = False
    polymarket_bankroll: float = 1000.0        # USDC bankroll used for sizing
    polymarket_kelly_multiplier: float = 0.25  # fractional Kelly (quarter-Kelly)
    polymarket_min_edge: float = 0.05          # require ≥5% edge after fees (live)
    polymarket_paper_min_edge: float = 0.01   # paper: bet on small positive edges for volume
    polymarket_paper_min_history: int = 15     # trust model sooner when paper trading
    polymarket_paper_max_model_weight: float = 0.65  # allow more disagreement vs market in paper
    polymarket_paper_loose_gates: bool = True  # disable ROI-based gate tightening in paper
    # Price-tier gates (autobet learning) — longshots need more edge + higher win prob (live)
    polymarket_longshot_min_edge_paper: float = 0.02
    polymarket_longshot_min_edge_live: float = 0.08
    polymarket_underdog_min_edge_paper: float = 0.02
    polymarket_underdog_min_edge_live: float = 0.05
    polymarket_longshot_min_model_prob: float = 0.30
    polymarket_underdog_min_model_prob: float = 0.25
    polymarket_coinflip_min_model_prob: float = 0.35
    polymarket_favorite_min_model_prob: float = 0.50
    # Paper mode uses lower win-prob floors so more sides qualify
    polymarket_paper_min_model_prob: float = 0.08
    polymarket_live_min_settled_bets: int = 50   # paper track record before live
    polymarket_live_min_roi_pct: float = 0.0     # require positive paper ROI
    clv_weight_scale: float = 2.0                # consensus weight multiplier from avg_clv
    consensus_min_picks: int = 3               # min pickers to form a consensus
    # Paper-mode loosened gates — keep autobets flowing for learning
    polymarket_paper_min_consensus_confidence: float = 0.35
    polymarket_paper_min_prop_confidence: float = 0.35
    consensus_min_picks_paper: int = 2
    polymarket_paper_min_liquidity: float = 100.0
    # Paper sizing — no book-wide exposure caps while learning; same ~5% per bet as live
    polymarket_paper_max_position_pct: float = 0.05
    polymarket_paper_max_total_exposure_pct: float = 1.0  # no total cap in paper
    polymarket_paper_max_event_exposure_pct: float = 1.0  # no per-event cap in paper
    polymarket_max_position_pct: float = 0.05  # ≤5% of bankroll per position (live)
    polymarket_max_total_exposure_pct: float = 0.40  # ≤40% of bankroll at risk (live)
    polymarket_max_event_exposure_pct: float = 0.10  # ≤10% per match/event (live)
    polymarket_min_liquidity: float = 500.0    # skip thin markets (< $500 liq)
    polymarket_max_book_pct: float = 0.10      # never take >10% of book depth
    polymarket_max_price: float = 0.95         # don't buy near-certain outcomes
    polymarket_min_price: float = 0.05         # don't buy near-zero outcomes
    polymarket_fee_bps: float = 0.0            # Polymarket taker fee (basis points)

    # CLOB credentials (LIVE ONLY — leave blank for paper mode)
    polymarket_private_key: str = ""           # wallet private key (Polygon)
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polymarket_funder_address: str = ""        # proxy/funder wallet address

    app_env: str = "development"
    log_level: str = "INFO"
    scrape_interval_minutes: int = 30
    top_influencers_seed: int = 100

    @field_validator("polymarket_live_enabled", mode="before")
    @classmethod
    def _empty_env_bool(cls, v: object) -> object:
        # GitHub Actions passes unset secrets as empty strings
        if v is None or v == "":
            return False
        return v

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
