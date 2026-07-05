"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  fetchCalibration, fetchAutobets, fetchPaperTrading, fetchTrackedPicks, fetchWeatherPredictions,
  triggerAutobetRun, type Sport, type Pick, type AutobetRow,
} from "@/lib/api";
import BetTypeBadge from "@/components/BetTypeBadge";
import OutcomeBadge, { SportBadge, inferPickSport } from "@/components/OutcomeBadge";
import { formatPickDisplay } from "@/lib/pickDisplay";
import VibrantStatCard from "@/components/VibrantStatCard";
import {
  TrendingUp, Target, Activity, RefreshCw, CheckCircle,
  Clock, AlertCircle, Banknote, Layers, CloudRain, Wind, Thermometer, BrainCircuit, ShieldAlert
} from "lucide-react";

function pct(n: number | null | undefined, decimals = 1) {
  if (n == null || isNaN(n)) return "—";
  return `${(n * 100).toFixed(decimals)}%`;
}
function money(n: number | null | undefined) {
  if (n == null || isNaN(n)) return "—";
  return `$${n.toFixed(2)}`;
}
function roiPct(n: number | null | undefined, decimals = 2) {
  if (n == null || isNaN(n)) return "—";
  return `${n >= 0 ? "+" : ""}${n.toFixed(decimals)}%`;
}
function edge(n: number | null | undefined) {
  if (n == null || isNaN(n)) return "—";
  return `${n >= 0 ? "+" : ""}${(n * 100).toFixed(1)}%`;
}

type Tab = "bets" | "calibration" | "paper" | "weather";

function formatAutobetPick(b: { outcome_name: string; bet_type?: string; bet_line?: string | null; bet_subject?: string | null }) {
  if (b.bet_type && b.bet_type !== "moneyline") {
    const parts = [b.bet_subject, b.outcome_name, b.bet_line].filter(Boolean);
    return parts.join(" · ") || b.outcome_name;
  }
  return b.outcome_name;
}

export default function TradingPage() {
  const [tab, setTab] = useState<Tab>("bets");
  const [sportFilter, setSportFilter] = useState<Sport | "all">("all");
  const qc = useQueryClient();

  const { data: abData, isLoading: abLoading } = useQuery({
    queryKey: ["autobets", 100], queryFn: () => fetchAutobets(100), refetchInterval: 60_000,
  });
  const { data: trackedData, isLoading: trackedLoading } = useQuery({
    queryKey: ["tracked-picks", sportFilter],
    queryFn: () => fetchTrackedPicks({ limit: 40, sport: sportFilter === "all" ? undefined : sportFilter }),
    refetchInterval: 120_000,
  });
  const { data: calData, isLoading: calLoading } = useQuery({
    queryKey: ["calibration"], queryFn: fetchCalibration, refetchInterval: 300_000,
  });
  const { data: paperData, isLoading: paperLoading } = useQuery({
    queryKey: ["paper-trading"], queryFn: fetchPaperTrading, refetchInterval: 120_000,
  });
  const { data: weatherData, isLoading: weatherLoading } = useQuery({
    queryKey: ["weather-predictions", 100], queryFn: () => fetchWeatherPredictions(100), refetchInterval: 120_000,
  });
  const { mutate: runAutobet, isPending: runPending } = useMutation({
    mutationFn: triggerAutobetRun,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["autobets"] });
      qc.invalidateQueries({ queryKey: ["paper-trading"] });
    },
  });

  const ab = abData?.summary;
  const bets = abData?.bets ?? [];
  const filteredAutobets = sportFilter === "all" ? bets : bets.filter((b) => (b.sport ?? "football") === sportFilter);
  const openBets = filteredAutobets.filter((b) => b.status === "open");
  const historyBets = filteredAutobets.filter((b) => b.status !== "open" && b.status !== "rejected");
  const trackedPicks = trackedData?.picks ?? [];

  return (
    <div className="space-y-8 pb-12">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-end justify-between gap-4 border-b border-gray-800/50 pb-6">
        <div>
          <h1 className="text-4xl font-extrabold flex items-center gap-3 text-transparent bg-clip-text bg-gradient-to-r from-violet-400 to-indigo-400">
            <TrendingUp className="w-8 h-8 text-violet-400" />
            Trading Hub
          </h1>
          <p className="text-gray-400 text-sm mt-2 max-w-2xl">
            Polymarket Autobet Execution, Model Calibration, and Paper Trading Analytics.
          </p>
        </div>
        {ab && (
          <div className="flex items-center gap-3 flex-wrap justify-end">
            <span className={`text-xs font-bold px-3 py-1.5 rounded-full uppercase tracking-wider ${
              ab.mode === "live" ? "bg-red-500/20 text-red-400 border border-red-500/30 shadow-[0_0_15px_rgba(239,68,68,0.3)] animate-pulse" : "bg-gray-800 text-gray-300 border border-gray-700"
            }`}>
              {ab.mode === "live" ? "Live Trading" : "Paper Mode"}
            </span>
            <button onClick={() => runAutobet()} disabled={runPending} className="flex items-center gap-2 text-sm bg-violet-600 hover:bg-violet-500 disabled:opacity-50 px-4 py-2 rounded-xl font-medium transition-all hover:shadow-[0_0_20px_rgba(139,92,246,0.3)]">
              <RefreshCw className={`w-4 h-4 ${runPending ? "animate-spin" : ""}`} />
              {runPending ? "Running…" : "Force Evaluation"}
            </button>
          </div>
        )}
      </div>

      {/* Summary stat row using VibrantStatCard */}
      {ab && (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-5">
          <VibrantStatCard label="Live Bankroll" value={money(ab.bankroll)} sub={`Started at ${money(ab.starting_bankroll)}`} icon={Banknote} color="emerald" />
          <VibrantStatCard label="Total P&L" value={`${ab.total_pnl >= 0 ? "+" : ""}${money(ab.total_pnl)}`} sub={`${ab.total_pnl >= 0 ? "+" : ""}${((ab.total_pnl / (ab.starting_bankroll || 1)) * 100).toFixed(1)}% ROI`} icon={TrendingUp} color={ab.total_pnl >= 0 ? "emerald" : "red"} />
          <VibrantStatCard label="Win Rate" value={pct(ab.win_rate)} sub={`Across ${ab.settled_bets} settled bets`} icon={Target} color="indigo" />
          <VibrantStatCard label="Open Exposure" value={money(ab.open_exposure)} sub={`${ab.open_bets} active positions`} icon={Activity} color="pink" />
        </div>
      )}

      {/* Autobet Learning (Redesigned) */}
      {ab?.learning && (
        <section className="glass-panel p-6 space-y-6">
          <div className="flex items-center gap-2">
            <BrainCircuit className="w-5 h-5 text-violet-400" />
            <h2 className="font-bold text-lg">Autobet Dynamic Learning</h2>
          </div>
          
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Price Tiers */}
            <div className="space-y-4">
              <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider">Performance by Odds Tier</h3>
              <div className="space-y-3">
                {Object.values(ab.learning.tier_stats).map((t) => {
                  const gates = ab.learning!.active_gates[t.tier];
                  const winRate = t.settled ? t.win_rate : 0;
                  return (
                    <div key={t.tier} className="bg-black/30 p-3 rounded-lg border border-white/5 relative overflow-hidden group">
                      <div className="absolute inset-0 bg-gradient-to-r from-violet-500/10 to-transparent opacity-0 group-hover:opacity-100 transition-opacity" />
                      <div className="flex justify-between items-end relative z-10">
                        <div>
                          <p className="text-xs text-gray-400">{t.label}</p>
                          <div className="flex items-baseline gap-2 mt-1">
                            <span className="text-lg font-bold text-white">{t.settled ? pct(winRate, 0) : "—"}</span>
                            <span className="text-[10px] text-gray-500">{t.settled} settled</span>
                          </div>
                        </div>
                        <div className="text-right">
                          <p className="text-[10px] text-gray-500 uppercase">ROI</p>
                          <p className={`text-sm font-bold ${t.roi_pct >= 0 ? "text-emerald-400" : t.settled ? "text-red-400" : "text-gray-500"}`}>
                            {t.settled ? roiPct(t.roi_pct, 1) : "—"}
                          </p>
                        </div>
                        <div className="text-right">
                          <p className="text-[10px] text-gray-500 uppercase">Min Edge Req</p>
                          <p className="text-sm font-bold text-yellow-400">
                            {gates ? edge(gates.min_edge) : "—"}
                            {gates?.adjusted && <span className="text-amber-500 ml-1" title="Adjusted by learning engine">⚠️</span>}
                          </p>
                        </div>
                      </div>
                      {/* Visual Bar */}
                      <div className="h-1 bg-gray-800 rounded-full mt-3 overflow-hidden">
                        <div className={`h-full ${winRate >= 0.5 ? "bg-emerald-500" : "bg-red-500"}`} style={{ width: `${winRate * 100}%` }} />
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Sport Stats & Traps */}
            <div className="space-y-6">
              {ab.learning.sport_stats && Object.keys(ab.learning.sport_stats).length > 0 && (
                <div>
                  <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">ROI By Domain</h3>
                  <div className="grid grid-cols-2 gap-3">
                    {Object.entries(ab.learning.sport_stats).map(([sport, s]) => (
                      <div key={sport} className="glass-card p-3 border-t-2 border-t-indigo-500">
                        <span className="capitalize text-gray-300 text-xs font-medium">{sport}</span>
                        <div className="mt-1 flex items-end gap-2">
                          <span className={`text-lg font-bold ${s.roi_pct >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                            {s.settled ? roiPct(s.roi_pct, 1) : "—"}
                          </span>
                          <span className="text-[10px] text-gray-500 mb-1">({s.settled})</span>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {ab.live_readiness && !ab.live_readiness.live_ready && (
                <div className="bg-amber-950/30 border border-amber-900/50 p-4 rounded-xl flex items-start gap-3">
                  <ShieldAlert className="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
                  <div>
                    <h4 className="text-sm font-semibold text-amber-500">Live Execution Suspended</h4>
                    <p className="text-xs text-amber-400/80 mt-1">{ab.live_readiness.message}</p>
                  </div>
                </div>
              )}
            </div>
          </div>
        </section>
      )}

      {/* Sport filter */}
      <div className="flex flex-wrap gap-2">
        {([
          { value: "all", label: "All domains" },
          { value: "football", label: "⚽ Football" },
          { value: "mlb", label: "⚾ MLB" },
          { value: "weather", label: "🌤️ Weather" },
          { value: "politics", label: "🏛️ Politics" },
        ] as const).map((s) => (
          <button
            key={s.value}
            onClick={() => setSportFilter(s.value)}
            className={`px-4 py-2 text-sm rounded-xl font-medium transition-all ${
              sportFilter === s.value ? "bg-indigo-600 text-white shadow-[0_0_15px_rgba(79,70,229,0.3)]" : "bg-white/5 text-gray-400 hover:text-white hover:bg-white/10 border border-white/5"
            }`}
          >
            {s.label}
          </button>
        ))}
      </div>

      {/* Tabs Menu */}
      <div className="flex gap-2 p-1 bg-black/40 border border-white/5 rounded-xl w-fit backdrop-blur-md">
        {([
          ["bets", "Active Trading", Target],
          ["calibration", "Model Calibration", Activity],
          ["paper", "Paper Analytics", Banknote],
          ["weather", "Weather Markets", CloudRain]
        ] as const).map(([t, label, Icon]) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`flex items-center gap-2 px-4 py-2.5 rounded-lg text-sm font-bold transition-all ${
              tab === t ? "bg-indigo-500/20 text-indigo-300 border border-indigo-500/30" : "text-gray-400 hover:text-white hover:bg-white/5 border border-transparent"
            }`}
          >
            <Icon className="w-4 h-4" /> {label}
          </button>
        ))}
      </div>

      {/* ── TAB CONTENT ──────────────────────────────────────────────────────── */}
      
      {/* 1. BETS TAB */}
      {tab === "bets" && (
        <div className="space-y-6">
          <section className="glass-panel p-6">
            <h2 className="font-bold text-lg mb-4 flex items-center gap-2">
              <Clock className="w-5 h-5 text-sky-400" /> Active Polymarket Positions ({openBets.length})
            </h2>
            {abLoading ? (
              <div className="h-32 animate-pulse bg-white/5 rounded-xl" />
            ) : openBets.length > 0 ? (
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {openBets.map((b, i) => (
                  <div key={i} className="bg-black/30 border border-white/5 hover:border-indigo-500/30 p-4 rounded-xl transition-colors group">
                    <div className="flex items-start justify-between gap-4 mb-3">
                      <div>
                        <div className="flex items-center gap-2 mb-1.5">
                          <SportBadge sport={b.sport ?? "football"} />
                          {b.bet_type && b.bet_type !== "moneyline" && <BetTypeBadge betType={b.bet_type} betLine={b.bet_line} size="sm" />}
                        </div>
                        <h3 className="text-gray-200 text-sm font-medium line-clamp-2 leading-snug">{b.question}</h3>
                      </div>
                      <div className="text-right shrink-0">
                        <p className="text-[10px] text-gray-500 uppercase font-semibold">Stake</p>
                        <p className="font-mono text-gray-200 font-bold">{money(b.stake)}</p>
                      </div>
                    </div>
                    <div className="grid grid-cols-3 gap-2 py-2 border-t border-white/5">
                      <div>
                        <p className="text-[10px] text-gray-500 uppercase">Outcome</p>
                        <p className="text-xs font-bold text-indigo-300 truncate">{formatAutobetPick(b)}</p>
                      </div>
                      <div className="text-center border-x border-white/5">
                        <p className="text-[10px] text-gray-500 uppercase">Edge</p>
                        <p className={`text-xs font-bold ${b.edge >= 0.05 ? "text-emerald-400" : "text-yellow-400"}`}>{edge(b.edge)}</p>
                      </div>
                      <div className="text-right">
                        <p className="text-[10px] text-gray-500 uppercase">Prob vs Mkt</p>
                        <p className="text-xs font-mono text-gray-400">{Math.round(b.model_prob * 100)}% / {Math.round(b.market_price * 100)}%</p>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="py-12 text-center text-gray-500 border border-dashed border-white/10 rounded-xl">
                <Target className="w-8 h-8 mx-auto mb-2 opacity-20" />
                <p>No open bets currently executing.</p>
              </div>
            )}
          </section>

          {historyBets.length > 0 && (
            <section className="glass-panel p-6">
              <h2 className="font-bold text-lg mb-4 flex items-center gap-2">
                <CheckCircle className="w-5 h-5 text-emerald-400" /> Settled History ({historyBets.length})
              </h2>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs uppercase tracking-wider text-gray-500 border-b border-gray-800">
                      <th className="pb-3 px-2 font-semibold">Market</th>
                      <th className="pb-3 px-2 font-semibold">Pick</th>
                      <th className="pb-3 px-2 font-semibold text-right">Edge</th>
                      <th className="pb-3 px-2 font-semibold text-right">Stake</th>
                      <th className="pb-3 px-2 font-semibold text-right">CLV</th>
                      <th className="pb-3 px-2 font-semibold text-right">P&L</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-800/50">
                    {historyBets.slice(0, 50).map((b, i) => (
                      <tr key={i} className="hover:bg-white/[0.02] transition-colors">
                        <td className="py-3 px-2 max-w-[250px]">
                          <p className="truncate text-gray-300 text-xs">{b.question}</p>
                        </td>
                        <td className="py-3 px-2 text-xs font-medium text-indigo-300">{formatAutobetPick(b)}</td>
                        <td className="py-3 px-2 text-right text-xs text-gray-500 font-mono">{edge(b.edge)}</td>
                        <td className="py-3 px-2 text-right text-xs font-mono text-gray-400">{money(b.stake)}</td>
                        <td className="py-3 px-2 text-right text-xs font-mono">
                          {b.clv != null ? (
                            <span className={b.clv > 0 ? "text-emerald-400" : b.clv < 0 ? "text-red-400" : "text-gray-500"}>
                              {b.clv > 0 ? "+" : ""}{(b.clv * 100).toFixed(1)}%
                            </span>
                          ) : (
                            <span className="text-gray-700">-</span>
                          )}
                        </td>
                        <td className="py-3 px-2 text-right">
                          <span className={`text-xs font-bold px-2 py-1 rounded ${
                            b.status === "won" ? "bg-emerald-500/20 text-emerald-400" :
                            b.status === "lost" ? "bg-red-500/20 text-red-400" : "bg-gray-800 text-gray-400"
                          }`}>
                            {b.pnl != null ? `${b.pnl >= 0 ? "+" : ""}${money(b.pnl)}` : b.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </div>
      )}

      {/* 2. CALIBRATION TAB */}
      {tab === "calibration" && (
        <div className="space-y-6">
          {calLoading ? (
            <div className="glass-panel p-8 animate-pulse h-48" />
          ) : calData && (calData.total_resolved ?? 0) > 0 ? (
            <>
              {calData.hit_rates_2d && Object.keys(calData.hit_rates_2d).length > 0 && (
                <section className="glass-panel p-6">
                  <h3 className="font-bold text-lg mb-1 text-gray-100">2D Calibration Heatmap</h3>
                  <p className="text-xs text-gray-400 mb-6">Empirical win rate across confidence levels and market implied odds.</p>
                  
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr>
                          <th className="pb-3 pr-4 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Conf / Market</th>
                          {["longshot", "underdog", "coinflip", "favorite"].map((m) => (
                            <th key={m} className="pb-3 px-3 text-center text-xs font-semibold text-gray-400 capitalize">{m}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-white/5">
                        {["low", "medium-low", "medium-high", "high"].map((conf) => (
                          <tr key={conf}>
                            <td className="py-3 pr-4 text-xs font-medium text-gray-400 capitalize">{conf}</td>
                            {["longshot", "underdog", "coinflip", "favorite"].map((mkt) => {
                              const cell = calData.hit_rates_2d?.[conf]?.[mkt];
                              const hr = cell?.hit_rate ?? null;
                              
                              let bg = "bg-white/5";
                              let text = "text-gray-500";
                              if (hr != null && cell!.total > 0) {
                                if (hr >= 0.55) { bg = "bg-emerald-500/20"; text = "text-emerald-400"; }
                                else if (hr >= 0.4) { bg = "bg-yellow-500/20"; text = "text-yellow-400"; }
                                else { bg = "bg-red-500/20"; text = "text-red-400"; }
                              }

                              return (
                                <td key={mkt} className="py-2 px-1">
                                  <div className={`rounded-lg flex flex-col items-center justify-center p-2 h-14 ${bg}`}>
                                    {hr != null && cell!.total > 0 ? (
                                      <>
                                        <span className={`font-bold ${text}`}>{(hr * 100).toFixed(0)}%</span>
                                        <span className="text-[9px] text-gray-500">n={cell!.total}</span>
                                      </>
                                    ) : <span className="text-gray-700">—</span>}
                                  </div>
                                </td>
                              );
                            })}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              )}

              <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                {/* Hit rates by confidence bucket */}
                <div className="glass-panel p-6">
                  <h3 className="font-bold text-lg mb-6">Hit Rate by Confidence</h3>
                  <div className="space-y-5">
                    {Object.entries(calData.hit_rates_by_bucket).map(([bucket, stats]) => (
                      <div key={bucket}>
                        <div className="flex justify-between text-xs mb-1.5">
                          <span className="text-gray-300 font-medium capitalize">{bucket}</span>
                          <span className="font-mono text-gray-400">
                            {(stats.hit_rate * 100).toFixed(0)}% <span className="text-gray-600">({stats.correct}/{stats.total})</span>
                          </span>
                        </div>
                        <div className="h-2 bg-gray-900 rounded-full overflow-hidden border border-white/5">
                          <div
                            className={`h-full rounded-full ${
                              stats.hit_rate >= 0.6 ? "bg-gradient-to-r from-emerald-600 to-emerald-400" :
                              stats.hit_rate >= 0.45 ? "bg-gradient-to-r from-yellow-600 to-yellow-400" : "bg-gradient-to-r from-red-600 to-red-400"
                            }`}
                            style={{ width: `${stats.hit_rate * 100}%` }}
                          />
                        </div>
                      </div>
                    ))}
                  </div>
                </div>

                {/* Upset traps */}
                {calData.upset_trap && Object.keys(calData.upset_trap).length > 0 && (
                  <div className="glass-panel p-6">
                    <h3 className="font-bold text-lg mb-6">Upset Traps</h3>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      {Object.entries(calData.upset_trap).map(([key, stats]) => (
                        <div key={key} className="bg-black/40 border border-white/5 rounded-xl p-4">
                          <p className="text-xs text-gray-400 mb-1">{stats.label ?? key}</p>
                          <p className="text-2xl font-black text-white">{(stats.hit_rate * 100).toFixed(0)}%</p>
                          <p className="text-[10px] text-gray-500 uppercase mt-1">{stats.correct}/{stats.total} correct</p>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </>
          ) : (
            <div className="glass-panel py-16 text-center">
              <Activity className="w-10 h-10 text-gray-600 mx-auto mb-3" />
              <p className="text-gray-400">No calibration data yet.</p>
            </div>
          )}
        </div>
      )}

      {/* 3. PAPER TAB */}
      {tab === "paper" && (
        <div className="space-y-6">
          {paperLoading ? (
            <div className="glass-panel p-8 animate-pulse h-48" />
          ) : paperData ? (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-5">
              <VibrantStatCard label="Paper Bankroll" value={money(paperData.bankroll)} icon={Banknote} color="cyan" />
              <VibrantStatCard label="Paper P&L" value={`${(paperData.total_pnl ?? 0) >= 0 ? "+" : ""}${money(paperData.total_pnl)}`} sub={`${roiPct(paperData.roi_pct)} ROI`} icon={TrendingUp} color={(paperData.total_pnl ?? 0) >= 0 ? "emerald" : "red"} />
              <VibrantStatCard label="Win Rate" value={pct(paperData.win_rate)} sub={`${paperData.total_bets} simulated bets`} icon={Target} color="purple" />
              <VibrantStatCard label="Total Wagered" value={money(paperData.total_wagered)} sub={`${paperData.pending_bets} pending`} icon={Layers} color="indigo" />
            </div>
          ) : (
            <div className="glass-panel py-16 text-center">
              <Banknote className="w-10 h-10 text-gray-600 mx-auto mb-3" />
              <p className="text-gray-400">No paper trading data.</p>
            </div>
          )}
        </div>
      )}

      {/* 4. WEATHER TAB */}
      {tab === "weather" && (
        <div className="glass-panel p-6">
          <div className="flex items-center justify-between mb-6">
            <h2 className="font-bold text-lg flex items-center gap-2">
              <CloudRain className="w-5 h-5 text-sky-400" />
              Pavlov Weather Engine
            </h2>
          </div>
          
          {weatherLoading ? (
            <div className="h-48 animate-pulse bg-white/5 rounded-xl" />
          ) : (weatherData?.predictions?.length ?? 0) > 0 ? (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {weatherData!.predictions.map((wp: any) => (
                <div key={wp.id} className="bg-black/30 border border-white/5 hover:border-sky-500/30 p-4 rounded-xl transition-all group">
                  <div className="flex items-center gap-2 mb-2">
                    <Thermometer className="w-4 h-4 text-sky-500 opacity-70" />
                    <span className="text-xs font-semibold text-sky-400 tracking-wide uppercase truncate">{wp.event_key}</span>
                  </div>
                  <div className="text-gray-100 font-bold mb-4">{wp.outcome}</div>
                  
                  <div className="grid grid-cols-3 gap-2 py-3 border-t border-white/5">
                    <div>
                      <p className="text-[10px] text-gray-500 uppercase">Model</p>
                      <p className="text-sm font-bold text-sky-300">{Math.round(wp.prob * 100)}%</p>
                    </div>
                    <div className="border-l border-white/5 pl-2">
                      <p className="text-[10px] text-gray-500 uppercase">Market</p>
                      <p className="text-sm font-mono text-gray-400">{Math.round((wp.market_price || 0) * 100)}%</p>
                    </div>
                    <div className="border-l border-white/5 pl-2">
                      <p className="text-[10px] text-gray-500 uppercase">Edge</p>
                      <p className={`text-sm font-bold ${wp.edge >= 0.05 ? "text-emerald-400" : "text-yellow-400"}`}>
                        {wp.edge != null ? `+${(wp.edge * 100).toFixed(1)}%` : "—"}
                      </p>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="py-12 text-center text-gray-500 border border-dashed border-white/10 rounded-xl">
              <CloudRain className="w-8 h-8 mx-auto mb-2 opacity-20" />
              <p>No weather predictions available.</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
