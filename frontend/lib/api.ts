import axios from "axios";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const api = axios.create({ baseURL: BASE });

// ─── Types ───────────────────────────────────────────────────────────────────

export interface Influencer {
  id: string;
  platform: "twitter" | "tiktok" | "instagram" | "covers" | "youtube";
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
  consensus_picks?: ConsensusPick[];
}

export interface Pick {
  id: string;
  influencer_id: string;
  platform: string;
  post_url?: string;
  raw_text: string;
  predicted_winner?: string;
  predicted_score?: string;
  confidence?: number;
  outcome: "pending" | "correct" | "incorrect" | "void";
  posted_at?: string;
  influencers?: Partial<Influencer>;
}

export interface ConsensusPick {
  id: string;
  match_id: string;
  predicted_winner: string;
  total_votes: number;
  weighted_score: number;
  confidence: number;
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
  history: any[];
}> {
  const { data } = await api.get(`/influencers/${id}`);
  return data;
}

export async function fetchMatches(params?: {
  upcoming_only?: boolean;
  stage?: string;
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

export async function fetchRecommendations(limit = 10): Promise<{
  recommendations: ConsensusPick[];
}> {
  const { data } = await api.get("/recommendations", { params: { limit } });
  return data;
}

export async function fetchOverview(): Promise<Overview> {
  const { data } = await api.get("/stats/overview");
  return data;
}
