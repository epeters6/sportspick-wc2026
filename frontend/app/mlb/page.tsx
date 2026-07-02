"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { fetchMatches, fetchRecentPicks, fetchPropPicks } from "@/lib/api";
import ConfidenceBar from "@/components/ConfidenceBar";
import PlatformBadge from "@/components/PlatformBadge";
import BetTypeBadge from "@/components/BetTypeBadge";
import OutcomeBadge, { SportBadge, inferPickSport } from "@/components/OutcomeBadge";
import { formatPickDisplay } from "@/lib/pickDisplay";
import { CheckCircle, Clock, Tv2, ArrowRight, Zap, Layers } from "lucide-react";

function fmtDate(s: string) {
  return new Date(s).toLocaleDateString("en-US", {
    weekday: "short", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

export default function MLBPage() {
  const [upcoming, setUpcoming] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["mlb-matches", upcoming],
    queryFn: () => fetchMatches({ sport: "mlb", upcoming_only: upcoming }),
    refetchInterval: 60_000,
  });

  const { data: picksData } = useQuery({
    queryKey: ["mlb-recent-picks"],
    queryFn: () => fetchRecentPicks({ sport: "mlb", limit: 30 }),
    refetchInterval: 120_000,
  });

  const { data: propData } = useQuery({
    queryKey: ["mlb-prop-picks"],
    queryFn: () => fetchPropPicks({ sport: "mlb", limit: 12 }),
    refetchInterval: 120_000,
  });

  const allPicks = picksData?.picks ?? [];
  const mlbProps = propData?.picks ?? [];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Tv2 className="w-6 h-6 text-blue-400" />
            MLB Games
          </h1>
          <p className="text-gray-400 text-sm mt-1">
            Picks from Covers, Pickswise, ActionNetwork, YouTube, 𝕏 &amp; TikTok
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Link
            href="/recommendations?sport=mlb"
            className="flex items-center gap-1.5 text-xs text-indigo-400 hover:text-indigo-300 bg-indigo-950/50 border border-indigo-900/50 px-3 py-1.5 rounded-lg"
          >
            <Zap className="w-3.5 h-3.5" />
            MLB consensus picks
          </Link>
          <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1">
            <button
              onClick={() => setUpcoming(false)}
              className={`px-3 py-1.5 text-sm rounded font-medium transition-colors ${!upcoming ? "bg-indigo-600 text-white" : "text-gray-400 hover:text-white"}`}
            >
              All
            </button>
            <button
              onClick={() => setUpcoming(true)}
              className={`px-3 py-1.5 text-sm rounded font-medium transition-colors ${upcoming ? "bg-indigo-600 text-white" : "text-gray-400 hover:text-white"}`}
            >
              Upcoming
            </button>
          </div>
        </div>
      </div>

      {/* MLB alt/prop picks */}
      {mlbProps.length > 0 && (
        <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold flex items-center gap-2">
              <Layers className="w-4 h-4 text-cyan-400" />
              MLB Alt Bets
            </h2>
            <Link href="/props?sport=mlb" className="text-xs text-indigo-400 flex items-center gap-1 hover:text-indigo-300">
              All MLB props <ArrowRight className="w-3 h-3" />
            </Link>
          </div>
          <div className="space-y-2">
            {mlbProps.slice(0, 6).map((p) => (
              <div key={p.id} className="flex items-start gap-3 bg-gray-800/50 rounded-lg p-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap mb-0.5">
                    <BetTypeBadge betType={p.bet_type} betLine={p.bet_line} size="sm" />
                    <OutcomeBadge outcome={p.outcome} />
                    <PlatformBadge platform={p.platform ?? p.influencers?.platform ?? "covers"} />
                    <span className="text-sm font-medium">@{p.influencers?.handle}</span>
                  </div>
                  {p.matches ? (
                    <p className="text-xs text-gray-400">
                      {p.matches.home_team} vs {p.matches.away_team}
                    </p>
                  ) : (
                    <p className="text-xs text-gray-500 italic">Match not linked</p>
                  )}
                  <p className="text-sm text-indigo-300 font-medium mt-0.5">→ {formatPickDisplay(p)}</p>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Recent MLB picks (all bet types) */}
      {allPicks.length > 0 && (
        <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold">Recent MLB Picks</h2>
            <Link href="/props?sport=mlb" className="text-xs text-indigo-400 flex items-center gap-1 hover:text-indigo-300">
              Alt bets <ArrowRight className="w-3 h-3" />
            </Link>
          </div>
          <div className="space-y-2">
            {allPicks.slice(0, 8).map((p) => (
              <div key={p.id} className="flex items-start gap-3 bg-gray-800/50 rounded-lg p-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap mb-0.5">
                    <PlatformBadge platform={p.platform ?? p.influencers?.platform ?? "covers"} />
                    <span className="text-sm font-medium">@{p.influencers?.handle}</span>
                    {p.bet_type && p.bet_type !== "moneyline" && (
                      <BetTypeBadge betType={p.bet_type} betLine={p.bet_line} size="sm" />
                    )}
                    {p.outcome && p.outcome !== "pending" && <OutcomeBadge outcome={p.outcome} />}
                    <SportBadge sport={inferPickSport(p) ?? "mlb"} />
                  </div>
                  {p.matches && (
                    <p className="text-xs text-gray-400">
                      {p.matches.home_team} vs {p.matches.away_team}
                    </p>
                  )}
                  <p className="text-sm text-indigo-300 font-medium mt-0.5">
                    → {p.bet_type && p.bet_type !== "moneyline" ? formatPickDisplay(p) : p.predicted_winner}
                  </p>
                </div>
                {p.posted_at && (
                  <span className="text-[10px] text-gray-500 flex-shrink-0">
                    {fmtDate(p.posted_at)}
                  </span>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      <div className="space-y-3">
        {isLoading &&
          [...Array(8)].map((_, i) => (
            <div key={i} className="bg-gray-900 border border-gray-800 rounded-xl h-20 animate-pulse" />
          ))}

        {data?.matches.map((m) => {
          const cp = m.consensus_picks?.[0];
          return (
            <Link
              key={m.id}
              href={`/matches/${m.id}`}
              className="bg-gray-900 border border-gray-800 rounded-xl p-4 flex items-center gap-4 hover:border-gray-700 transition-colors block"
            >
              <div className="flex-shrink-0">
                {m.is_final ? (
                  <CheckCircle className="w-5 h-5 text-emerald-500" />
                ) : (
                  <Clock className="w-5 h-5 text-yellow-500" />
                )}
              </div>

              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-3">
                  <span className="font-semibold">{m.home_team}</span>
                  {m.is_final ? (
                    <span className="font-mono text-lg font-bold text-white">
                      {m.home_score} – {m.away_score}
                    </span>
                  ) : (
                    <span className="text-gray-500">vs</span>
                  )}
                  <span className="font-semibold">{m.away_team}</span>
                </div>
                <div className="flex items-center gap-3 text-xs text-gray-400 mt-0.5">
                  {m.stage && <><span>{m.stage}</span><span>·</span></>}
                  <span>{fmtDate(m.scheduled_at)}</span>
                  {m.venue && <><span>·</span><span>{m.venue}</span></>}
                </div>
              </div>

              {cp && (
                <div className="text-right flex-shrink-0 w-40">
                  <p className="text-xs text-gray-400 mb-1">
                    Consensus · {cp.pick_count ?? cp.total_votes} picks
                  </p>
                  <p className="font-semibold text-indigo-300 text-sm">{cp.predicted_winner}</p>
                  <div className="mt-1">
                    <ConfidenceBar value={cp.calibrated_confidence ?? cp.confidence} />
                  </div>
                  {cp.calibrated_confidence != null && (
                    <p className="text-[10px] text-amber-400/80 mt-0.5">
                      {Math.round((cp.calibrated_confidence ?? cp.confidence) * 100)}% calibrated
                    </p>
                  )}
                </div>
              )}
            </Link>
          );
        })}

        {!isLoading && !data?.matches.length && (
          <div className="bg-gray-900 border border-gray-800 rounded-xl py-12 text-center">
            <Tv2 className="w-10 h-10 text-gray-600 mx-auto mb-3" />
            <p className="text-gray-500">No MLB games synced yet.</p>
            <p className="text-gray-600 text-xs mt-1">Run the sync workflow to fetch the schedule.</p>
          </div>
        )}
      </div>
    </div>
  );
}
