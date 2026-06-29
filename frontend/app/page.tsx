"use client";
import { useQuery } from "@tanstack/react-query";
import {
  fetchOverview, fetchRecommendations, fetchLeaderboard, fetchMatches,
  fetchCalibration, fetchAutobets, fetchPaperTrading, fetchPlatformStats,
  fetchPropPicks, fetchRecentPicks,
} from "@/lib/api";
import BetTypeBadge from "@/components/BetTypeBadge";
import OutcomeBadge from "@/components/OutcomeBadge";
import { formatPickDisplay } from "@/lib/pickDisplay";
import StatCard from "@/components/StatCard";
import ConfidenceBar from "@/components/ConfidenceBar";
import PlatformBadge from "@/components/PlatformBadge";
import ProbBar from "@/components/ProbBar";
import Link from "next/link";
import { ArrowRight, Target, Radio, Layers } from "lucide-react";
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

export default function Dashboard() {
  const { data: overview, isLoading: ovLoading } = useQuery({
    queryKey: ["overview"], queryFn: fetchOverview, refetchInterval: 60_000,
  });
  const { data: recsData } = useQuery({
    queryKey: ["recommendations", 5], queryFn: () => fetchRecommendations(5), refetchInterval: 120_000,
  });
  const { data: leaderData } = useQuery({
    queryKey: ["leaderboard", 5], queryFn: () => fetchLeaderboard({ limit: 5 }),
  });
  const { data: matchesData } = useQuery({
    queryKey: ["matches-upcoming"], queryFn: () => fetchMatches({ upcoming_only: true, limit: 8 }), refetchInterval: 120_000,
  });
  const { data: calData } = useQuery({
    queryKey: ["calibration"], queryFn: fetchCalibration, refetchInterval: 300_000,
  });
  const { data: autobetData } = useQuery({
    queryKey: ["autobets-summary"], queryFn: () => fetchAutobets(1), refetchInterval: 120_000,
  });
  const { data: paperData } = useQuery({
    queryKey: ["paper-trading"], queryFn: fetchPaperTrading, refetchInterval: 120_000,
  });
  const { data: platformStats } = useQuery({
    queryKey: ["platform-stats"], queryFn: fetchPlatformStats, refetchInterval: 120_000,
  });
  const { data: propData } = useQuery({
    queryKey: ["dashboard-props"], queryFn: () => fetchPropPicks({ limit: 6 }), refetchInterval: 120_000,
  });
  const { data: mlbPickData } = useQuery({
    queryKey: ["dashboard-mlb-picks"], queryFn: () => fetchRecentPicks({ sport: "mlb", limit: 5 }), refetchInterval: 120_000,
  });

  const ab = autobetData?.summary;

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Dashboard</h1>
          <p className="text-gray-400 text-sm mt-1">World Cup 2026 + MLB — Pick Tracker</p>
        </div>
        <Link
          href="/sources"
          className="flex items-center gap-1.5 text-xs text-indigo-400 hover:text-indigo-300 bg-indigo-950/50 border border-indigo-900/50 px-3 py-1.5 rounded-lg"
        >
          <Radio className="w-3.5 h-3.5" />
          {platformStats
            ? `${Object.values(platformStats.influencers_by_platform).reduce((a, b) => a + b, 0)} sources tracked`
            : "View sources"}
        </Link>
      </div>

      {/* Active sources + sport breakdown */}
      {platformStats && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
            <h2 className="font-semibold text-sm mb-3">Data Sources</h2>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
              {SYNC_SOURCES.map((s) => {
                const inf = platformStats.influencers_by_platform[s.id] ?? 0;
                const picks = platformStats.picks_by_platform[s.id] ?? 0;
                return (
                  <div key={s.id} className="bg-gray-800/60 rounded-lg p-3">
                    <p className={`text-xs font-medium ${s.color}`}>{s.label}</p>
                    <p className="text-lg font-bold mt-0.5">{fmt(inf)}</p>
                    <p className="text-[10px] text-gray-500">{fmt(picks)} picks</p>
                  </div>
                );
              })}
            </div>
          </section>
          <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
            <h2 className="font-semibold text-sm mb-3">By Sport</h2>
            <div className="grid grid-cols-2 gap-3">
              <div className="bg-gray-800/60 rounded-lg p-4">
                <p className="text-xs text-gray-400">⚽ World Cup</p>
                <p className="text-2xl font-bold mt-1">{fmt(platformStats.matches_by_sport.football ?? 0)}</p>
                <p className="text-[10px] text-gray-500">matches tracked</p>
              </div>
              <div className="bg-gray-800/60 rounded-lg p-4">
                <p className="text-xs text-gray-400">⚾ MLB</p>
                <p className="text-2xl font-bold mt-1">{fmt(platformStats.matches_by_sport.mlb ?? 0)}</p>
                <p className="text-[10px] text-gray-500">games tracked</p>
                {(platformStats.mlb_prop_picks_total ?? 0) > 0 && (
                  <p className="text-[10px] text-cyan-400 mt-1">{fmt(platformStats.mlb_prop_picks_total!)} MLB props</p>
                )}
                <Link href="/mlb" className="text-[10px] text-indigo-400 hover:text-indigo-300 mt-1 inline-block">
                  View MLB →
                </Link>
              </div>
            </div>
            {(platformStats.prop_picks_total ?? 0) > 0 && (
              <p className="text-[10px] text-gray-500 mt-3">
                {fmt(platformStats.prop_picks_total!)} alt/prop picks tracked ·{" "}
                <Link href="/props" className="text-indigo-400 hover:text-indigo-300">View all →</Link>
              </p>
            )}
            <p className="text-[10px] text-gray-600 mt-3">
              X/TikTok scrape when session cookies are configured · sync every 30 min
            </p>
          </section>
        </div>
      )}

      {/* Stat cards — overview */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {ovLoading
          ? [...Array(4)].map((_, i) => (
              <div key={i} className="bg-gray-900 border border-gray-800 rounded-xl p-5 animate-pulse h-24" />
            ))
          : overview && <>
              <StatCard label="Influencers tracked" value={fmt(overview.total_influencers)} accent="blue" />
              <StatCard
                label="Total picks"
                value={fmt(overview.total_picks)}
                sub={`${fmt(overview.resolved_picks)} resolved`}
                accent="purple"
              />
              <StatCard
                label="Overall accuracy"
                value={pct(overview.overall_accuracy)}
                sub={`${fmt(overview.correct_picks)} correct`}
                accent="green"
              />
              <StatCard
                label="Matches"
                value={`${overview.finished_matches} / ${overview.total_matches}`}
                sub="finished"
                accent="yellow"
              />
            </>
        }
      </div>

      {/* ML + Trading stat cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {calData && (calData.total_resolved ?? 0) > 0 && (
          <>
            <StatCard
              label="Brier score"
              value={(calData.brier_score ?? 0).toFixed(4)}
              sub={`${calData.total_resolved} resolved picks`}
              accent="blue"
            />
            <StatCard
              label="Simulated ROI"
              value={`${(calData.simulated_roi_pct ?? 0) > 0 ? "+" : ""}${(calData.simulated_roi_pct ?? 0).toFixed(1)}%`}
              sub="vs implied odds"
              accent={(calData.simulated_roi_pct ?? 0) >= 0 ? "green" : "red"}
            />
          </>
        )}
        {ab && (
          <>
            <StatCard
              label={`Polymarket ${ab.mode === "live" ? "🔴 LIVE" : "📝 Paper"}`}
              value={money(ab.bankroll)}
              sub={`${ab.total_pnl >= 0 ? "+" : ""}${money(ab.total_pnl)} P&L`}
              accent={ab.total_pnl >= 0 ? "green" : "red"}
            />
            <StatCard
              label="Open bets"
              value={String(ab.open_bets)}
              sub={`${money(ab.open_exposure)} at risk`}
              accent="purple"
            />
          </>
        )}
        {paperData && !calData?.total_resolved && (
          <StatCard
            label="Paper trading"
            value={money(paperData.bankroll)}
            sub={`ROI ${roiPct(paperData.roi_pct, 1)}`}
            accent={(paperData.roi_pct ?? 0) >= 0 ? "green" : "red"}
          />
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Top Picks */}
        <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold">{"Today's Top Consensus Picks"}</h2>
            <Link href="/recommendations" className="text-xs text-indigo-400 flex items-center gap-1 hover:text-indigo-300">
              View all <ArrowRight className="w-3 h-3" />
            </Link>
          </div>
          <div className="space-y-5">
            {recsData?.recommendations.slice(0, 5).map((rec) => (
              <div key={rec.match_id} className="space-y-1.5">
                <div className="flex items-center justify-between text-sm">
                  <span className="font-medium text-xs text-gray-400">
                    {rec.matches?.home_team} vs {rec.matches?.away_team}
                  </span>
                  <span className="text-indigo-300 font-semibold text-sm">{rec.predicted_winner}</span>
                </div>
                <ProbBar
                  homeProb={rec.home_probability}
                  drawProb={rec.draw_probability}
                  awayProb={rec.away_probability}
                  homeLabel={rec.matches?.home_team ?? "Home"}
                  awayLabel={rec.matches?.away_team ?? "Away"}
                />
                <p className="text-xs text-gray-500">{rec.pick_count ?? rec.total_votes} pickers · {Math.round(rec.confidence * 100)}% confidence</p>
              </div>
            ))}
            {!recsData?.recommendations.length && (
              <p className="text-sm text-gray-500">No recommendations yet.</p>
            )}
          </div>
        </section>

        {/* Top Influencers */}
        <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold">Top Influencers</h2>
            <Link href="/leaderboard" className="text-xs text-indigo-400 flex items-center gap-1 hover:text-indigo-300">
              Full leaderboard <ArrowRight className="w-3 h-3" />
            </Link>
          </div>
          <div className="space-y-3">
            {leaderData?.influencers.slice(0, 5).map((inf, i) => (
              <Link
                key={inf.id}
                href={`/leaderboard/${inf.id}`}
                className="flex items-center gap-3 hover:bg-gray-800 rounded-lg p-1.5 -mx-1.5 transition-colors"
              >
                <span className="w-5 text-center text-xs text-gray-500 font-mono">{i + 1}</span>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-sm truncate">@{inf.handle}</span>
                    <PlatformBadge platform={inf.platform} />
                  </div>
                  <div className="flex gap-3 text-xs text-gray-400 mt-0.5">
                    <span>Elo {Math.round(inf.elo_score ?? 1000)}</span>
                    <span>{pct(inf.accuracy_rate)} acc.</span>
                    {inf.avg_clv != null && (
                      <span className={inf.avg_clv >= 0 ? "text-emerald-400" : "text-red-400"}>
                        CLV {inf.avg_clv >= 0 ? "+" : ""}{(inf.avg_clv * 100).toFixed(1)}%
                      </span>
                    )}
                  </div>
                </div>
                <div className={`text-sm font-bold ${(inf.pick_streak ?? 0) > 0 ? "text-emerald-400" : "text-red-400"}`}>
                  {(inf.pick_streak ?? 0) > 0 ? "+" : ""}{inf.pick_streak}
                </div>
              </Link>
            ))}
          </div>
        </section>
      </div>

      {/* Alt bets + MLB picks */}
      {((propData?.picks.length ?? 0) > 0 || (mlbPickData?.picks.length ?? 0) > 0) && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {(propData?.picks.length ?? 0) > 0 && (
            <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
              <div className="flex items-center justify-between mb-4">
                <h2 className="font-semibold flex items-center gap-2">
                  <Layers className="w-4 h-4 text-cyan-400" />
                  Recent Alt Bets
                </h2>
                <Link href="/props" className="text-xs text-indigo-400 flex items-center gap-1 hover:text-indigo-300">
                  All props <ArrowRight className="w-3 h-3" />
                </Link>
              </div>
              <div className="space-y-2">
                {propData!.picks.slice(0, 5).map((p) => (
                  <div key={p.id} className="flex items-center gap-2 bg-gray-800/50 rounded-lg p-2.5">
                    <BetTypeBadge betType={p.bet_type} betLine={p.bet_line} size="sm" />
                    <OutcomeBadge outcome={p.outcome} />
                    <span className="text-sm text-indigo-300 truncate flex-1">{formatPickDisplay(p)}</span>
                  </div>
                ))}
              </div>
            </section>
          )}
          {(mlbPickData?.picks.length ?? 0) > 0 && (
            <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
              <div className="flex items-center justify-between mb-4">
                <h2 className="font-semibold">Recent MLB Picks</h2>
                <Link href="/mlb" className="text-xs text-indigo-400 flex items-center gap-1 hover:text-indigo-300">
                  MLB hub <ArrowRight className="w-3 h-3" />
                </Link>
              </div>
              <div className="space-y-2">
                {mlbPickData!.picks.slice(0, 5).map((p) => (
                  <div key={p.id} className="flex items-center gap-2 bg-gray-800/50 rounded-lg p-2.5">
                    {p.bet_type && p.bet_type !== "moneyline" && (
                      <BetTypeBadge betType={p.bet_type} betLine={p.bet_line} size="sm" />
                    )}
                    <span className="text-sm text-indigo-300 truncate flex-1">
                      {p.bet_type && p.bet_type !== "moneyline" ? formatPickDisplay(p) : p.predicted_winner}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          )}
        </div>
      )}

      {/* Polymarket open bets mini-table */}
      {(autobetData?.bets?.filter(b => b.status === "open").length ?? 0) > 0 && (
        <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold flex items-center gap-2">
              <Target className="w-4 h-4 text-violet-400" />
              Active Polymarket Bets
              {ab?.mode === "paper" && (
                <span className="text-xs bg-gray-800 text-gray-400 px-2 py-0.5 rounded-full">paper</span>
              )}
            </h2>
            <Link href="/trading" className="text-xs text-indigo-400 flex items-center gap-1 hover:text-indigo-300">
              Full trading hub <ArrowRight className="w-3 h-3" />
            </Link>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 border-b border-gray-800">
                  <th className="pb-2 font-medium">Market</th>
                  <th className="pb-2 font-medium">Pick</th>
                  <th className="pb-2 font-medium text-right">Edge</th>
                  <th className="pb-2 font-medium text-right">Stake</th>
                  <th className="pb-2 font-medium text-right">Model</th>
                  <th className="pb-2 font-medium text-right">Mkt</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800">
                {autobetData!.bets.filter(b => b.status === "open").slice(0, 5).map((b, i) => (
                  <tr key={i} className="hover:bg-gray-800/50">
                    <td className="py-2.5 max-w-[200px] truncate text-gray-300 text-xs">{b.question}</td>
                    <td className="py-2.5 font-medium text-indigo-300">{b.outcome_name}</td>
                    <td className="py-2.5 text-right">
                      <span className={b.edge >= 0.07 ? "text-emerald-400" : "text-yellow-400"}>
                        +{(b.edge * 100).toFixed(1)}%
                      </span>
                    </td>
                    <td className="py-2.5 text-right font-mono text-gray-300">${b.stake.toFixed(2)}</td>
                    <td className="py-2.5 text-right font-mono text-xs text-gray-400">{Math.round(b.model_prob * 100)}%</td>
                    <td className="py-2.5 text-right font-mono text-xs text-gray-400">{Math.round(b.market_price * 100)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {/* Upcoming matches */}
      <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-semibold">Upcoming WC Matches</h2>
          <Link href="/matches" className="text-xs text-indigo-400 flex items-center gap-1 hover:text-indigo-300">
            All matches <ArrowRight className="w-3 h-3" />
          </Link>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-gray-500 border-b border-gray-800">
                <th className="pb-2 font-medium">Match</th>
                <th className="pb-2 font-medium">Date</th>
                <th className="pb-2 font-medium">Consensus</th>
                <th className="pb-2 font-medium">Confidence</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {matchesData?.matches.slice(0, 8).map((m) => {
                const cp = m.consensus_picks?.[0];
                return (
                  <tr key={m.id} className="hover:bg-gray-800/50 transition-colors">
                    <td className="py-2.5 font-medium">{m.home_team} vs {m.away_team}</td>
                    <td className="py-2.5 text-gray-400 text-xs">
                      {new Date(m.scheduled_at).toLocaleDateString("en-US", {
                        month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
                      })}
                    </td>
                    <td className="py-2.5">
                      {cp ? <span className="text-indigo-300 font-medium">{cp.predicted_winner}</span>
                           : <span className="text-gray-600">—</span>}
                    </td>
                    <td className="py-2.5 w-32">
                      {cp ? <ConfidenceBar value={cp.confidence} /> : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!matchesData?.matches.length && (
            <p className="text-sm text-gray-500 py-4 text-center">No upcoming matches synced yet.</p>
          )}
        </div>
      </section>
    </div>
  );
}
