"use client";
import { useQuery } from "@tanstack/react-query";
import {
  fetchOverview, fetchLeaderboard, fetchMatches,
  fetchCalibration, fetchAutobets, fetchPaperTrading, fetchPlatformStats,
  fetchPropPicks, fetchRecentPicks,
} from "@/lib/api";
import BetTypeBadge from "@/components/BetTypeBadge";
import OutcomeBadge from "@/components/OutcomeBadge";
import { formatPickDisplay } from "@/lib/pickDisplay";
import PlatformBadge from "@/components/PlatformBadge";
import Link from "next/link";
import { ArrowRight, Target, Radio, Layers, Activity, BrainCircuit, Users } from "lucide-react";
import { SYNC_SOURCES } from "@/lib/platforms";

function fmt(n: number) { return n.toLocaleString(); }
function pct(n: number) { return `${(n * 100).toFixed(1)}%`; }
function money(n: number | null | undefined) {
  if (n == null || isNaN(n)) return "—";
  return `$${n.toFixed(2)}`;
}
function roiPct(n: number | null | undefined, decimals = 1) {
  if (n == null || isNaN(n)) return "—";
  return `${n >= 0 ? "+" : ""}${n.toFixed(decimals)}%`;
}

// Custom Vibrant StatCard
function VibrantStatCard({ label, value, sub, icon: Icon, color }: { label: string, value: string, sub?: string, icon: any, color: string }) {
  return (
    <div className={`glass-card p-5 border-t-4 border-t-${color}-500 relative overflow-hidden group`}>
      <div className={`absolute -right-4 -top-4 opacity-10 group-hover:opacity-20 transition-opacity text-${color}-500`}>
        <Icon className="w-24 h-24" />
      </div>
      <p className="text-gray-400 text-sm font-medium tracking-wide uppercase mb-2">{label}</p>
      <p className={`text-3xl font-extrabold text-transparent bg-clip-text bg-gradient-to-br from-white to-gray-400 mb-1`}>{value}</p>
      {sub && <p className={`text-xs text-${color}-400 font-medium`}>{sub}</p>}
    </div>
  );
}

export default function Dashboard() {
  const { data: overview, isLoading: ovLoading, isError: ovError } = useQuery({
    queryKey: ["overview"], queryFn: fetchOverview, refetchInterval: 60_000,
  });
  const { data: leaderData } = useQuery({
    queryKey: ["leaderboard", 5], queryFn: () => fetchLeaderboard({ limit: 5 }),
  });
  const { data: matchesData } = useQuery({
    queryKey: ["matches-upcoming"], queryFn: () => fetchMatches({ upcoming_only: true, limit: 12 }), refetchInterval: 120_000,
  });
  const { data: calData } = useQuery({
    queryKey: ["calibration"], queryFn: fetchCalibration, refetchInterval: 300_000,
  });
  const { data: autobetData } = useQuery({
    queryKey: ["autobets-summary"], queryFn: () => fetchAutobets(1), refetchInterval: 120_000,
  });
  const { data: platformStats } = useQuery({
    queryKey: ["platform-stats"], queryFn: fetchPlatformStats, refetchInterval: 120_000,
  });
  const { data: propData } = useQuery({
    queryKey: ["dashboard-props"], queryFn: () => fetchPropPicks({ limit: 4 }), refetchInterval: 120_000,
  });
  const { data: mlbPickData } = useQuery({
    queryKey: ["dashboard-mlb-picks"], queryFn: () => fetchRecentPicks({ sport: "mlb", limit: 4 }), refetchInterval: 120_000,
  });

  const ab = autobetData?.summary;

  return (
    <div className="space-y-10 pb-12">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-end justify-between gap-4 border-b border-gray-800/50 pb-6">
        <div>
          <h1 className="text-4xl font-extrabold text-transparent bg-clip-text bg-gradient-to-r from-indigo-400 via-purple-400 to-pink-400 tracking-tight">
            SportsPick Intelligence
          </h1>
          <p className="text-gray-400 text-sm mt-2 max-w-2xl">
            Live Quantitative Prediction Engine. Aggregating crowd consensus, calibrating influencer edges, and blending with MLB fatigue models.
          </p>
        </div>
        <Link
          href="/sources"
          className="group flex items-center gap-2 text-xs font-semibold text-indigo-300 hover:text-white bg-indigo-950/40 border border-indigo-500/30 px-4 py-2.5 rounded-xl transition-all hover:bg-indigo-600/40 hover:shadow-[0_0_20px_rgba(99,102,241,0.3)]"
        >
          <Activity className="w-4 h-4 animate-pulse text-indigo-400 group-hover:text-white" />
          {platformStats
            ? `${fmt(Object.values(platformStats.influencers_by_platform).reduce((a, b) => a + b, 0))} Verified Sources`
            : "System Active"}
        </Link>
      </div>

      {/* Primary Metrics */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-5">
        {ovLoading
          ? [...Array(4)].map((_, i) => <div key={i} className="glass-card h-32 animate-pulse" />)
          : ovError || !overview
          ? [...Array(4)].map((_, i) => (
              <div key={i} className="glass-card h-32 flex items-center justify-center text-gray-500 text-sm">
                {i === 0 ? "API unavailable — check NEXT_PUBLIC_API_URL" : ""}
              </div>
            ))
          : <>
              <VibrantStatCard label="Tracked Picks" value={fmt(overview.total_picks)} sub={`${fmt(overview.resolved_picks)} Graded`} icon={Layers} color="indigo" />
              <VibrantStatCard label="Crowd Accuracy" value={pct(overview.overall_accuracy)} sub={`${fmt(overview.correct_picks)} Correct Winners`} icon={Target} color="emerald" />
              {calData ? (
                 <VibrantStatCard label="Brier Score" value={(calData.brier_score ?? 0).toFixed(4)} sub={`Raw: ${(calData.raw_brier_score ?? 0).toFixed(3)}`} icon={BrainCircuit} color="cyan" />
              ) : (
                 <VibrantStatCard label="Live Matches" value={fmt(overview.total_matches)} sub={`${fmt(overview.finished_matches)} Completed`} icon={Radio} color="pink" />
              )}
              {ab ? (
                 <VibrantStatCard label="Trading P&L" value={`${ab.total_pnl >= 0 ? "+" : ""}${money(ab.total_pnl)}`} sub={`${money(ab.bankroll)} Bankroll (${ab.mode})`} icon={Activity} color={ab.total_pnl >= 0 ? "emerald" : "red"} />
              ) : (
                 <VibrantStatCard label="Verified Influencers" value={fmt(overview.total_influencers)} sub="Across 7 platforms" icon={Users} color="purple" />
              )}
            </>
        }
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        
        {/* Main Panel: Upcoming Matches & ML Model Interaction */}
        <section className="xl:col-span-2 glass-card p-6 flex flex-col">
          <div className="flex items-center justify-between mb-6">
            <div>
              <h2 className="text-lg font-bold flex items-center gap-2">
                <BrainCircuit className="w-5 h-5 text-indigo-400" />
                Live Model Consensus
              </h2>
              <p className="text-xs text-gray-400 mt-1">Comparing Crowd Intelligence vs Quantitative MLB Models</p>
            </div>
            <Link href="/matches" className="text-xs font-semibold text-indigo-400 flex items-center gap-1 hover:text-indigo-300 transition-colors">
              View All Pipeline Matches <ArrowRight className="w-4 h-4" />
            </Link>
          </div>
          
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-gray-500 border-b border-gray-800/50">
                  <th className="pb-3 font-semibold px-2">Matchup</th>
                  <th className="pb-3 font-semibold text-center">Crowd Consensus</th>
                  <th className="pb-3 font-semibold text-center">MLB Quant Model</th>
                  <th className="pb-3 font-semibold text-center">Final Blended Pick</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/30">
                {matchesData?.matches.slice(0, 10).map((m) => {
                  const cp = m.consensus_picks?.[0];
                  // Extract ML Prediction for the match winner (not outs)
                  const mlPreds = m.model_predictions?.filter(p => p.source === "sports_ml" && !p.outcome.includes(" ")) || [];
                  const ml = mlPreds.length > 0 ? mlPreds[0] : null;
                  
                  return (
                    <tr key={m.id} className="hover:bg-indigo-900/10 transition-colors group">
                      <td className="py-4 px-2">
                        <div className="font-semibold text-gray-200">{m.home_team}</div>
                        <div className="text-gray-500 text-xs mt-0.5">vs {m.away_team}</div>
                      </td>
                      
                      {/* Crowd */}
                      <td className="py-4 text-center border-l border-gray-800/30 px-2">
                        {cp ? (
                          <div>
                             <span className="text-gray-300 font-medium">{cp.predicted_winner}</span>
                             <div className="text-[10px] text-gray-500 mt-1">{Math.round((cp.raw_confidence || cp.confidence) * 100)}% Confidence ({cp.total_votes} votes)</div>
                          </div>
                        ) : <span className="text-gray-700">—</span>}
                      </td>

                      {/* ML Model */}
                      <td className="py-4 text-center border-l border-gray-800/30 px-2 bg-indigo-950/10">
                        {ml ? (
                          <div>
                            <span className="text-indigo-400 font-bold drop-shadow-[0_0_8px_rgba(99,102,241,0.5)]">{ml.outcome}</span>
                            <div className="text-[10px] text-indigo-300/70 mt-1">{Math.round(ml.prob * 100)}% Probability</div>
                          </div>
                        ) : <span className="text-gray-700 text-xs">Awaiting Simulation</span>}
                      </td>
                      
                      {/* Blended / Final */}
                      <td className="py-4 text-center border-l border-gray-800/30 px-2">
                        {cp ? (
                          <div>
                             <span className={`font-extrabold ${ml && cp.predicted_winner === ml.outcome ? 'text-emerald-400' : 'text-gray-100'}`}>
                               {cp.predicted_winner}
                             </span>
                             <div className="w-full bg-gray-800 h-1.5 rounded-full mt-2 overflow-hidden flex max-w-[80px] mx-auto">
                               <div className="bg-gradient-to-r from-indigo-500 to-purple-500 h-full" style={{ width: `${Math.round(cp.confidence * 100)}%` }} />
                             </div>
                             {ml && cp.predicted_winner !== ml.outcome && (
                               <div className="text-[9px] text-yellow-500 mt-1 uppercase tracking-wider font-bold">Model Conflict</div>
                             )}
                          </div>
                        ) : <span className="text-gray-700">—</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {!matchesData?.matches.length && (
              <div className="py-12 flex flex-col items-center justify-center text-gray-500">
                <Radio className="w-8 h-8 mb-3 opacity-20" />
                <p>No upcoming matches pipeline syncs detected.</p>
              </div>
            )}
          </div>
        </section>

        {/* Right Sidebar */}
        <div className="space-y-6 flex flex-col">
          {/* Top Influencers */}
          <section className="glass-panel p-5 flex-1">
            <div className="flex items-center justify-between mb-5">
              <h2 className="font-semibold text-gray-200">Alpha Extractors</h2>
              <Link href="/leaderboard" className="text-[10px] uppercase tracking-wider text-indigo-400 hover:text-indigo-300 transition-colors">
                Full Leaderboard
              </Link>
            </div>
            <div className="space-y-4">
              {leaderData?.influencers.slice(0, 5).map((inf, i) => (
                <Link key={inf.id} href={`/leaderboard/${inf.id}`} className="flex items-center gap-3 p-2 -mx-2 rounded-xl hover:bg-white/5 transition-all group">
                  <div className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold ${i === 0 ? 'bg-yellow-500/20 text-yellow-400 border border-yellow-500/30' : i === 1 ? 'bg-gray-300/20 text-gray-300 border border-gray-400/30' : i === 2 ? 'bg-amber-700/20 text-amber-500 border border-amber-700/30' : 'bg-gray-800 text-gray-500'}`}>
                    {i + 1}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-sm text-gray-200 truncate group-hover:text-white transition-colors">@{inf.handle}</span>
                      <PlatformBadge platform={inf.platform} />
                    </div>
                    <div className="flex justify-between items-center mt-1">
                       <span className="text-xs font-mono text-indigo-300 bg-indigo-950/50 px-1.5 py-0.5 rounded border border-indigo-500/20">Elo {Math.round(inf.elo_score ?? 1000)}</span>
                       {inf.avg_clv != null && (
                         <span className={`text-[10px] font-bold ${inf.avg_clv >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                           {inf.avg_clv >= 0 ? "+" : ""}{(inf.avg_clv * 100).toFixed(1)}% CLV
                         </span>
                       )}
                    </div>
                  </div>
                </Link>
              ))}
            </div>
          </section>

          {/* Active Polymarket Trading */}
          {ab && (
            <section className="glass-panel p-5">
              <div className="flex items-center justify-between mb-4">
                <h2 className="font-semibold text-gray-200 flex items-center gap-2">
                  <Target className="w-4 h-4 text-pink-500" /> Active Positions
                </h2>
              </div>
              <div className="space-y-3">
                {autobetData!.bets.filter(b => b.status === "open").slice(0, 3).map((b, i) => (
                  <div key={i} className="bg-black/30 border border-gray-800/50 rounded-lg p-3">
                    <div className="text-xs text-gray-400 truncate mb-1">{b.question}</div>
                    <div className="flex items-center justify-between">
                       <span className="font-semibold text-sm text-white">{b.outcome_name}</span>
                       <span className="font-mono text-sm text-emerald-400">+{(b.edge * 100).toFixed(1)}%</span>
                    </div>
                    <div className="flex items-center justify-between mt-2 pt-2 border-t border-gray-800/50">
                       <span className="text-[10px] text-gray-500">Stake: <span className="text-gray-300 font-mono">${b.stake.toFixed(2)}</span></span>
                       <span className="text-[10px] text-gray-500">Mkt: {Math.round(b.market_price * 100)}%</span>
                    </div>
                  </div>
                ))}
              </div>
            </section>
          )}
        </div>
      </div>
      
    </div>
  );
}
