import axios from "axios";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const api = axios.create({
  baseURL: BASE,
  timeout: 20_000,
});

// ─── Types ───────────────────────────────────────────────────────────────────

export type Platform = "twitter" | "tiktok" | "instagram" | "covers" | "youtube" | "actionnetwork" | "pickswise" | "reddit";
export type BetType =
  | "moneyline" | "draw" | "total_goals" | "total_runs" | "btts" | "spread"
  | "corners" | "cards" | "shots"
  | "team_shots" | "team_tackles" | "team_hits" | "team_strikeouts"
  | "team_total_goals" | "team_total_runs"
  | "first_half_goals" | "first_five_runs"
  | "player_scorer" | "player_assists" | "player_shots" | "player_strikeouts"
  | "player_hits" | "player_rbis" | "player_goals" | "player_tackles";
export type Sport = "football" | "mlb";

export interface Influencer {
  id: string;
  platform: Platform;
  handle: string;
  display_name?: string;
  profile_url?: string;
  avatar_url?: string;
  follower_count: number;
  elo_score: number;
  accuracy_rate: number;
  total_picks: number;
  correct_picks: number;
  pick_streak: number;
  consensus_score: number;
  avg_clv?: number;
  elo_by_sport?: Record<string, number>;
  avg_clv_by_sport?: Record<string, number>;
  last_scraped_at?: string;
  rank?: number;
  bio?: string;
}

export interface Match {
  id: string;
  home_team: string;
  away_team: string;
  scheduled_at: string;
  home_score?: number;
  away_score?: number;
  winner?: string;
  stage?: string;
  venue?: string;
  is_final: boolean;
  tournament: string;
  sport?: Sport;
  consensus_picks?: ConsensusPick[];
}

export interface Pick {
  id: string;
  influencer_id: string;
  platform: Platform;
  post_url?: string;
  raw_text: string;
  predicted_winner?: string;
  predicted_score?: string;
  confidence?: number;
  outcome: "pending" | "correct" | "incorrect" | "void";
  posted_at?: string;
  bet_type?: BetType;
  bet_line?: string;
  bet_subject?: string;
  market_prob_at_pick?: number;
  influencers?: Partial<Influencer>;
  matches?: Partial<Match>;
}

export interface ConsensusPick {
  id: string;
  match_id: string;
  predicted_winner: string;
  total_votes: number;
  weighted_score: number;
  confidence: number;
  raw_confidence?: number;
  calibrated_confidence?: number;
  pick_count?: number;
  home_probability?: number;
  draw_probability?: number;
  away_probability?: number;
  generated_at: string;
  matches?: Partial<Match>;
}

export interface Overview {
  total_influencers: number;
  total_picks: number;
  resolved_picks: number;
  correct_picks: number;
  overall_accuracy: number;
  total_matches: number;
  finished_matches: number;
}

export interface PlatformStats {
  influencers_by_platform: Record<string, number>;
  picks_by_platform: Record<string, number>;
  matches_by_sport: Record<string, number>;
  prop_picks_total?: number;
  mlb_prop_picks_total?: number;
  active_sources: {
    id: string;
    label: string;
    always_on: boolean;
    note?: string;
  }[];
}

export interface CalibrationSegment {
  total_resolved: number;
  hit_rate: number;
  brier_score: number;
  raw_brier_score: number;
  calibrated_brier_score: number;
}

export interface CalibrationSummary {
  total_resolved: number;
  brier_score: number;
  raw_brier_score?: number;
  calibrated_brier_score?: number;
  simulated_roi_pct: number;
  hit_rates_by_bucket: Record<string, { hit_rate: number; correct: number; total: number }>;
  hit_rates_by_bet_type: Record<string, { hit_rate: number; correct: number; total: number }>;
  hit_rates_2d?: Record<string, Record<string, { hit_rate: number; correct: number; total: number }>>;
  upset_trap?: Record<string, { hit_rate: number; correct: number; total: number; label?: string }>;
  picks_with_market_line?: number;
  moneyline?: CalibrationSegment;
  props?: CalibrationSegment;
  mlb?: CalibrationSegment & {
    calibration_curve?: Record<string, number>;
    ml_history_size?: number;
    using_sport_curve?: boolean;
  };
  calibration_curve?: Record<string, number>;
  ml_history_size?: number;
}

export interface AutobetTierStat {
  tier: string;
  label: string;
  settled: number;
  wins: number;
  win_rate: number;
  total_staked: number;
  total_pnl: number;
  roi_pct: number;
  avg_market_price: number;
  avg_edge: number;
  sharpe?: number | null;
}

export interface AutobetLearning {
  tier_stats: Record<string, AutobetTierStat>;
  sport_stats?: Record<string, {
    settled: number; wins: number; win_rate: number;
    total_staked: number; total_pnl: number; roi_pct: number; label?: string;
  }>;
  upset_trap?: Record<string, {
    settled: number; wins: number; win_rate: number;
    total_staked: number; total_pnl: number; roi_pct: number; label?: string;
  }>;
  bankroll_curve?: { at: string | null; bankroll: number; pnl_cumulative: number; bet_n: number }[];
  live_readiness?: {
    live_ready: boolean;
    settled_bets: number;
    min_settled_required: number;
    paper_roi_pct: number;
    min_roi_required_pct: number;
    total_pnl: number;
    message: string;
  };
  active_gates: Record<string, {
    tier?: string;
    min_edge: number;
    min_model_prob: number;
    adjusted: boolean;
    note: string;
  }>;
  min_tier_samples: number;
}

export interface AutobetSummary {
  mode: "paper" | "live";
  starting_bankroll: number;
  bankroll: number;
  total_pnl: number;
  roi_pct: number;
  settled_bets: number;
  win_rate: number;
  open_bets: number;
  open_exposure: number;
  total_staked: number;
  learning?: AutobetLearning;
  live_readiness?: AutobetLearning["live_readiness"];
}

export interface AutobetRow {
  question: string;
  outcome_name: string;
  bet_type?: string;
  bet_line?: string | null;
  bet_subject?: string | null;
  sport?: Sport | null;
  mode: "paper" | "live";
  model_prob: number;
  market_price: number;
  edge: number;
  stake: number;
  status: "open" | "won" | "lost" | "void" | "rejected";
  pnl?: number;
  created_at: string;
  resolved_at?: string;
  reject_reason?: string;
}

export interface SimulatedBetRow {
  id: string;
  predicted_outcome?: string;
  bet_type?: string;
  bet_line?: string | null;
  bet_subject?: string | null;
  confidence?: number;
  edge?: number;
  bet_size?: number;
  outcome?: string | null;
  pnl?: number;
  created_at: string;
  resolved_at?: string;
  matches?: Partial<Match>;
}

export interface PaperTradingSummary {
  bankroll: number;
  starting_bankroll: number;
  total_pnl: number;
  total_bets: number;
  pending_bets: number;
  win_rate: number;
  roi_pct: number;
  total_wagered: number;
}

// ─── API calls ───────────────────────────────────────────────────────────────

export async function fetchLeaderboard(params?: {
  limit?: number;
  sort_by?: string;
  platform?: string;
}): Promise<{ influencers: Influencer[]; total: number }> {
  const { data } = await api.get("/influencers", { params });
  return data;
}

export async function fetchInfluencer(id: string): Promise<{
  influencer: Influencer;
  recent_picks: Pick[];
  history: { snapshot_date: string; elo_score: number; accuracy_rate: number; elo_rank?: number; accuracy_rank?: number }[];
}> {
  const { data } = await api.get(`/influencers/${id}`);
  return data;
}

export async function fetchMatches(params?: {
  upcoming_only?: boolean;
  stage?: string;
  sport?: Sport;
  limit?: number;
}): Promise<{ matches: Match[]; total: number }> {
  const { data } = await api.get("/matches", { params });
  return data;
}

export async function fetchMatchPicks(matchId: string): Promise<{
  match: Match;
  picks: Pick[];
  consensus: ConsensusPick[];
}> {
  const { data } = await api.get(`/matches/${matchId}/picks`);
  return data;
}

export async function fetchRecommendations(
  limit = 10,
  sport?: Sport,
): Promise<{ recommendations: ConsensusPick[] }> {
  const { data } = await api.get("/recommendations", { params: { limit, sport } });
  return data;
}

export async function fetchOverview(): Promise<Overview> {
  const { data } = await api.get("/stats/overview");
  return data;
}

export async function fetchPlatformStats(): Promise<PlatformStats> {
  const { data } = await api.get("/stats/platforms");
  return data;
}

export async function fetchRecentPicks(params?: {
  limit?: number;
  sport?: Sport;
  platform?: string;
}): Promise<{ picks: Pick[]; total: number }> {
  const { data } = await api.get("/picks/recent", { params });
  return data;
}

export async function fetchCalibration(): Promise<CalibrationSummary> {
  const { data } = await api.get("/trading/calibration");
  return data;
}

export async function fetchAutobets(limit = 50): Promise<{
  summary: AutobetSummary;
  bets: AutobetRow[];
}> {
  const { data } = await api.get("/trading/autobet", { params: { limit } });
  return data;
}

export async function fetchPaperTrading(): Promise<PaperTradingSummary> {
  const { data } = await api.get("/trading/paper");
  return data;
}

export async function fetchPropPicks(params?: {
  limit?: number;
  bet_type?: BetType;
  sport?: Sport;
}): Promise<{ picks: Pick[]; total: number }> {
  const { data } = await api.get("/picks/props", { params });
  return data;
}

export async function fetchTrackedPicks(params?: {
  limit?: number;
  sport?: Sport;
}): Promise<{ picks: Pick[]; total: number }> {
  const { data } = await api.get("/trading/tracked-picks", { params });
  return data;
}

export async function fetchSimulatedBets(limit = 50): Promise<{
  bets: SimulatedBetRow[];
  total: number;
}> {
  const { data } = await api.get("/trading/simulated", { params: { limit } });
  return data;
}

export async function triggerAutobetRun(): Promise<{ summary: AutobetSummary; resolved: number }> {
  const { data } = await api.post("/trading/autobet/run");
  return data;
}
