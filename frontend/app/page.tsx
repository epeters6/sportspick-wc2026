"use client";
import { useQuery } from "@tanstack/react-query";
import { fetchOverview, fetchRecommendations, fetchLeaderboard, fetchMatches } from "@/lib/api";
import StatCard from "@/components/StatCard";
import ConfidenceBar from "@/components/ConfidenceBar";
import PlatformBadge from "@/components/PlatformBadge";
import Link from "next/link";
import { ArrowRight, RefreshCw } from "lucide-react";

function fmt(n: number) { return n.toLocaleString(); }
function pct(n: number) { return `${(n * 100).toFixed(1)}%`; }

export default function Dashboard() {
  const { data: overview, isLoading: ovLoading } = useQuery({
    queryKey: ["overview"],
    queryFn: fetchOverview,
    refetchInterval: 60_000,
  });
  const { data: recsData } = useQuery({
    queryKey: ["recommendations", 5],
    queryFn: () => fetchRecommendations(5),
    refetchInterval: 120_000,
  });
  const { data: leaderData } = useQuery({
    queryKey: ["leaderboard", 5],
    queryFn: () => fetchLeaderboard({ limit: 5 }),
  });
  const { data: matchesData } = useQuery({
    queryKey: ["matches-upcoming"],
    queryFn: () => fetchMatches({ upcoming_only: true }),
    refetchInterval: 120_000,
  });

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Dashboard</h1>
          <p className="text-gray-400 text-sm mt-1">FIFA World Cup 2026 — Pick Tracker</p>
        </div>
        <span className="flex items-center gap-1.5 text-xs text-gray-500">
          <RefreshCw className="w-3 h-3" /> Live — refreshes every 30 min
        </span>
      </div>

      {/* Stat cards */}
      {ovLoading ? (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="bg-gray-900 border border-gray-800 rounded-xl p-5 animate-pulse h-24" />
          ))}
        </div>
      ) : overview ? (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
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
            label="WC Matches"
            value={`${overview.finished_matches} / ${overview.total_matches}`}
            sub="finished"
            accent="yellow"
          />
        </div>
      ) : null}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Top Picks */}
        <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold">Today's Top Consensus Picks</h2>
            <Link href="/recommendations" className="text-xs text-indigo-400 flex items-center gap-1 hover:text-indigo-300">
              View all <ArrowRight className="w-3 h-3" />
            </Link>
          </div>
          <div className="space-y-4">
            {recsData?.recommendations.slice(0, 5).map((rec) => (
              <div key={rec.id} className="flex flex-col gap-1.5">
                <div className="flex items-center justify-between text-sm">
                  <span className="font-medium">
                    {rec.matches?.home_team} vs {rec.matches?.away_team}
                  </span>
                  <span className="text-indigo-300 font-semibold">{rec.predicted_winner}</span>
                </div>
                <ConfidenceBar value={rec.confidence} />
                <p className="text-xs text-gray-500">{rec.total_votes} influencers agree</p>
              </div>
            ))}
            {!recsData?.recommendations.length && (
              <p className="text-sm text-gray-500">No recommendations yet — waiting for picks.</p>
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
                    <span>Elo {Math.round(inf.elo_score)}</span>
                    <span>{pct(inf.accuracy_rate)} acc.</span>
                    <span>{inf.total_picks} picks</span>
                  </div>
                </div>
                <div className={`text-sm font-bold ${inf.pick_streak > 0 ? "text-emerald-400" : "text-red-400"}`}>
                  {inf.pick_streak > 0 ? "+" : ""}{inf.pick_streak}
                </div>
              </Link>
            ))}
          </div>
        </section>
      </div>

      {/* Upcoming matches */}
      <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-semibold">Upcoming Matches</h2>
          <Link href="/matches" className="text-xs text-indigo-400 flex items-center gap-1 hover:text-indigo-300">
            All matches <ArrowRight className="w-3 h-3" />
          </Link>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-gray-500 border-b border-gray-800">
                <th className="pb-2 font-medium">Match</th>
                <th className="pb-2 font-medium">Stage</th>
                <th className="pb-2 font-medium">Date</th>
                <th className="pb-2 font-medium">Consensus Pick</th>
                <th className="pb-2 font-medium">Confidence</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {matchesData?.matches.slice(0, 8).map((m) => {
                const cp = m.consensus_picks?.[0];
                return (
                  <tr key={m.id} className="hover:bg-gray-800/50 transition-colors">
                    <td className="py-2.5 font-medium">{m.home_team} vs {m.away_team}</td>
                    <td className="py-2.5 text-gray-400">{m.stage || "—"}</td>
                    <td className="py-2.5 text-gray-400">
                      {new Date(m.scheduled_at).toLocaleDateString("en-US", {
                        month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
                      })}
                    </td>
                    <td className="py-2.5">
                      {cp ? (
                        <span className="text-indigo-300 font-medium">{cp.predicted_winner}</span>
                      ) : (
                        <span className="text-gray-600">No data</span>
                      )}
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
