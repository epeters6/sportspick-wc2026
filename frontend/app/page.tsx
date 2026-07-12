"use client";
import { useQuery } from "@tanstack/react-query";
import {
  fetchOverview, fetchLeaderboard,
  fetchCalibration, fetchAutobets, fetchPlatformStats,
  fetchPropPicks, fetchRecentPicks, fetchWeatherPredictions,
  fetchTreasury, fetchGuardian, fetchArbScan,
} from "@/lib/api";
import PlatformBadge from "@/components/PlatformBadge";
import VibrantStatCard from "@/components/VibrantStatCard";
import Link from "next/link";
import { ArrowRight, Target, Radio, Layers, Activity, BrainCircuit, Users, CloudRain, AlertTriangle, Wallet, ArrowLeftRight, ShieldAlert, ShieldCheck, Shield } from "lucide-react";

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

export default function Dashboard() {
  const { data: overview, isLoading: ovLoading } = useQuery({
    queryKey: ["overview"], queryFn: fetchOverview, refetchInterval: 60_000,
  });
  const { data: leaderData } = useQuery({
    queryKey: ["leaderboard", 5], queryFn: () => fetchLeaderboard({ limit: 5 }),
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
  const { data: weatherData } = useQuery({
    queryKey: ["weather-predictions"], queryFn: () => fetchWeatherPredictions(4), refetchInterval: 120_000,
  });
  const { data: treasuryData } = useQuery({
    queryKey: ["treasury"], queryFn: fetchTreasury, refetchInterval: 60_000,
  });
  const { data: guardianData } = useQuery({
    queryKey: ["guardian"], queryFn: fetchGuardian, refetchInterval: 60_000,
  });
  const { data: arbData } = useQuery({
    queryKey: ["arb-scan"], queryFn: fetchArbScan, refetchInterval: 30_000,
  });

  const ab = autobetData?.summary;

  return (
    <div className="space-y-10 pb-12">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-end justify-between gap-4 border-b border-gray-800/50 pb-6">
        <div>
          <h1 className="text-4xl font-extrabold text-transparent bg-clip-text bg-gradient-to-r from-emerald-400 via-cyan-400 to-indigo-400 tracking-tight">
            Quant Betting Command Center
          </h1>
          <p className="text-gray-400 text-sm mt-2 max-w-2xl">
            Shadow trading on Polymarket & Kalshi — MLB moneylines, weather buckets (high/low temp), and calibrated crowd edges.
          </p>
        </div>
        <Link
          href="/live"
          className="group flex items-center gap-2 text-xs font-semibold text-emerald-300 hover:text-white bg-emerald-950/40 border border-emerald-500/30 px-4 py-2.5 rounded-xl transition-all hover:bg-emerald-600/30"
        >
          <Shield className="w-4 h-4" />
          {ab?.mode === "live" ? "Live Active" : "Shadow Mode"} — View Gates
        </Link>
      </div>

      {/* Primary Metrics */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-5">
        {ovLoading
          ? [...Array(4)].map((_, i) => <div key={i} className="glass-card h-32 animate-pulse" />)
          : overview && <>
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
        
        {/* Main Panel: Command Center */}
        <section className="xl:col-span-2 flex flex-col gap-6">
          
          {/* Guardian & Treasury Row */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            
            {/* Guardian Circuit Breaker */}
            <div className="glass-panel p-5 relative overflow-hidden group">
              <div className="absolute inset-0 bg-gradient-to-br from-indigo-500/10 to-transparent opacity-0 group-hover:opacity-100 transition-opacity"></div>
              <div className="flex items-center justify-between mb-4 relative z-10">
                <h3 className="font-semibold flex items-center gap-2">
                  <ShieldCheck className={`w-5 h-5 ${guardianData?.halted ? 'text-red-400' : 'text-emerald-400'}`} />
                  Guardian Circuit Status
                </h3>
              </div>
              <div className="relative z-10 flex flex-col gap-3">
                <div className="flex items-center justify-between p-3 rounded-xl bg-black/40 border border-white/5">
                  <span className="text-sm font-medium text-gray-300">Global Execution</span>
                  <span className={`px-2 py-1 rounded text-xs font-bold ${guardianData?.halted ? 'bg-red-500/20 text-red-400 border border-red-500/30' : 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'}`}>
                    {guardianData?.halted ? "HALTED" : "LIVE"}
                  </span>
                </div>
                {guardianData?.halted && guardianData?.reasons?.map((r: string, i: number) => (
                  <div key={i} className="text-xs text-red-300 bg-red-950/30 p-2 rounded border border-red-900/50 flex items-start gap-2">
                    <ShieldAlert className="w-4 h-4 shrink-0" />
                    <span>{r}</span>
                  </div>
                ))}
              </div>
            </div>

            {/* Treasury Watcher */}
            <div className="glass-panel p-5 relative overflow-hidden group">
              <div className="absolute inset-0 bg-gradient-to-br from-purple-500/10 to-transparent opacity-0 group-hover:opacity-100 transition-opacity"></div>
              <div className="flex items-center justify-between mb-4 relative z-10">
                <h3 className="font-semibold flex items-center gap-2">
                  <Wallet className="w-5 h-5 text-purple-400" />
                  Treasury Watcher
                </h3>
              </div>
              <div className="relative z-10 flex flex-col gap-3">
                <div className="flex justify-between items-center bg-black/30 p-2 rounded-lg border border-white/5">
                  <span className="text-sm text-gray-400">Kalshi USD</span>
                  <span className="font-mono text-sm text-white">${treasuryData?.kalshi_usd?.toFixed(2) || "0.00"}</span>
                </div>
                <div className="flex justify-between items-center bg-black/30 p-2 rounded-lg border border-white/5">
                  <span className="text-sm text-gray-400">Polymarket USDC</span>
                  <span className="font-mono text-sm text-white">${treasuryData?.polymarket_usdc?.toFixed(2) || "0.00"}</span>
                </div>
                {/* Visual warning if treasury is low */}
                {treasuryData && (treasuryData.kalshi_usd < 50 || treasuryData.polymarket_usdc < 50) && (
                   <div className="text-[10px] text-amber-400 flex items-center gap-1 mt-1">
                     <AlertTriangle className="w-3 h-3" /> Treasury low. Arb executions may fail.
                   </div>
                )}
              </div>
            </div>
            
          </div>

          {/* Arb Scanner Feed */}
          <div className="glass-panel p-5">
             <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold flex items-center gap-2">
                  <ArrowLeftRight className="w-5 h-5 text-pink-400" />
                  Live Arbitrage Scanner
                </h3>
                <span className="text-xs text-indigo-400 bg-indigo-950/50 px-2 py-1 rounded-full animate-pulse border border-indigo-500/30">Scanning...</span>
             </div>
             
             {arbData?.opportunities?.length > 0 ? (
               <div className="space-y-3">
                 {arbData.opportunities.map((arb: any, i: number) => (
                   <div key={i} className="flex flex-col md:flex-row md:items-center justify-between p-4 bg-black/40 rounded-xl border border-pink-500/20 hover:border-pink-500/50 transition-colors">
                     <div>
                       <div className="text-sm font-medium text-white mb-1">{arb.market}</div>
                       <div className="text-xs text-gray-400 flex items-center gap-2">
                         <span className="bg-blue-950/50 text-blue-300 px-1.5 rounded border border-blue-500/30">Kalshi: {arb.kalshi_side}</span>
                         <span>+</span>
                         <span className="bg-purple-950/50 text-purple-300 px-1.5 rounded border border-purple-500/30">Poly: {arb.poly_side}</span>
                       </div>
                     </div>
                     <div className="mt-3 md:mt-0 flex flex-col md:items-end">
                       <span className="text-sm font-mono text-emerald-400 font-bold">+{arb.margin.toFixed(2)}¢ Margin</span>
                       <span className="text-[10px] text-gray-500 mt-1">Max Size: {arb.available_size} | {arb.timestamp}</span>
                     </div>
                   </div>
                 ))}
               </div>
             ) : (
               <div className="flex flex-col items-center justify-center py-8 text-gray-500">
                 <Radio className="w-8 h-8 mb-3 opacity-20" />
                 <p className="text-sm">No cross-exchange arbitrage opportunities found.</p>
               </div>
             )}
          </div>

          {/* Parametric Weather Nowcasting */}
          <div className="glass-panel p-5 mt-4 border-t-2 border-t-sky-500">
             <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold flex items-center gap-2">
                  <CloudRain className="w-5 h-5 text-sky-400" />
                  Parametric Weather Nowcasting
                </h3>
             </div>
             
             {weatherData?.predictions?.length > 0 ? (
               <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                 {weatherData.predictions.slice(0, 2).map((wp: any, i: number) => (
                   <div key={i} className="flex flex-col p-4 bg-black/40 rounded-xl border border-sky-500/20">
                     <div className="flex items-center justify-between mb-3">
                       <div className="text-sm font-bold text-gray-200">{wp.outcome}</div>
                       <span className="text-[10px] text-sky-400 font-mono bg-sky-950/40 px-2 py-0.5 rounded uppercase">{wp.event_key}</span>
                     </div>
                     <div className="flex justify-between items-end mb-2">
                       <div>
                         <div className="text-xs text-gray-500 uppercase">Model Probability</div>
                         <div className="text-lg font-bold text-sky-300">{Math.round(wp.prob * 100)}%</div>
                       </div>
                       <div className="text-right">
                         <div className="text-xs text-gray-500 uppercase">Market Price</div>
                         <div className="text-lg font-bold text-gray-300">{Math.round((wp.market_price || 0) * 100)}¢</div>
                       </div>
                     </div>
                     <div className="mt-2 pt-2 border-t border-sky-900/30 flex justify-between items-center">
                        <span className="text-[10px] text-gray-500 uppercase font-semibold">Calculated Edge</span>
                        <span className={`text-sm font-bold font-mono ${wp.edge && wp.edge >= 0.05 ? "text-emerald-400" : "text-gray-400"}`}>
                          {wp.edge != null ? `+${(wp.edge * 100).toFixed(1)}%` : "—"}
                        </span>
                     </div>
                   </div>
                 ))}
               </div>
             ) : (
               <div className="flex flex-col items-center justify-center py-6 text-gray-500">
                 <Radio className="w-8 h-8 mb-2 opacity-20" />
                 <p className="text-sm">Awaiting METAR/Ensemble updates...</p>
               </div>
             )}
          </div>
          
          {/* MLB & Weather focus (WC excluded) */}
          <div className="flex items-center justify-between mb-2 mt-4">
            <div>
              <h2 className="text-lg font-bold flex items-center gap-2">
                <BrainCircuit className="w-5 h-5 text-indigo-400" />
                Active Markets
              </h2>
            </div>
            <Link href="/trading" className="text-xs font-semibold text-indigo-400 flex items-center gap-1 hover:text-indigo-300 transition-colors">
              Trading Hub <ArrowRight className="w-4 h-4" />
            </Link>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {mlbPickData?.picks?.slice(0, 2).map((p: any) => (
              <div key={p.id} className="glass-card p-4 border-t-2 border-t-emerald-500">
                <span className="text-xs text-emerald-400 uppercase font-semibold">MLB</span>
                <p className="text-sm text-gray-200 mt-1 truncate">{p.raw_text?.slice(0, 80)}</p>
              </div>
            ))}
            {weatherData?.predictions?.slice(0, 2).map((wp: any, i: number) => (
              <div key={i} className="glass-card p-4 border-t-2 border-t-sky-500">
                <span className="text-xs text-sky-400 uppercase font-semibold">Weather</span>
                <p className="text-sm text-gray-200 mt-1">{wp.outcome}</p>
              </div>
            ))}
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
                {autobetData?.bets?.filter(b => b?.status === "open").slice(0, 3).map((b, i) => (
                  <div key={i} className="bg-black/30 border border-gray-800/50 rounded-lg p-3">
                    <div className="text-xs text-gray-400 truncate mb-1">{b?.question}</div>
                    <div className="flex items-center justify-between">
                       <span className="font-semibold text-sm text-white">{b?.outcome_name}</span>
                       <span className="font-mono text-sm text-emerald-400">+{(Number(b?.edge || 0) * 100).toFixed(1)}%</span>
                    </div>
                    <div className="flex items-center justify-between mt-2 pt-2 border-t border-gray-800/50">
                       <span className="text-[10px] text-gray-500">Stake: <span className="text-gray-300 font-mono">${Number(b?.stake || 0).toFixed(2)}</span></span>
                       <span className="text-[10px] text-gray-500">Mkt: {Math.round(Number(b?.market_price || 0) * 100)}%</span>
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
