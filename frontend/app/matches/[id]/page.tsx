"use client";
import { useQuery } from "@tanstack/react-query";
import { use } from "react";
import { fetchMatchPicks } from "@/lib/api";
import PlatformBadge from "@/components/PlatformBadge";
import ConfidenceBar from "@/components/ConfidenceBar";
import { ArrowLeft, CheckCircle, XCircle, Clock } from "lucide-react";
import Link from "next/link";
import BetTypeBadge from "@/components/BetTypeBadge";
import ProbBar from "@/components/ProbBar";

const outcomeIcon = {
  correct: <CheckCircle className="w-4 h-4 text-emerald-400" />,
  incorrect: <XCircle className="w-4 h-4 text-red-400" />,
  pending: <Clock className="w-4 h-4 text-yellow-400" />,
  void: <span className="text-gray-500 text-xs">void</span>,
};

export default function MatchDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data, isLoading } = useQuery({
    queryKey: ["match-picks", id],
    queryFn: () => fetchMatchPicks(id),
    refetchInterval: 60_000,
  });

  if (isLoading) {
    return <div className="animate-pulse space-y-4">
      {[...Array(4)].map((_, i) => <div key={i} className="h-16 bg-gray-900 rounded-xl border border-gray-800" />)}
    </div>;
  }

  const { match, picks, consensus } = data ?? {};
  if (!match) return <p className="text-gray-400">Match not found.</p>;

  return (
    <div className="space-y-6">
      <Link href="/matches" className="flex items-center gap-1.5 text-sm text-gray-400 hover:text-white transition-colors">
        <ArrowLeft className="w-4 h-4" /> Back to matches
      </Link>

      {/* Match header */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <p className="text-xs text-gray-500 mb-2">{match.stage} · {match.tournament}</p>
        <div className="flex items-center gap-6">
          <div className="text-center flex-1">
            <p className="text-2xl font-bold">{match.home_team}</p>
            <p className="text-xs text-gray-400 mt-1">Home</p>
          </div>
          <div className="text-center">
            {match.is_final ? (
              <p className="text-4xl font-black font-mono">
                {match.home_score} – {match.away_score}
              </p>
            ) : (
              <p className="text-xl font-bold text-gray-500">vs</p>
            )}
            {match.is_final && match.winner && (
              <p className="text-xs text-emerald-400 mt-1 font-medium">
                Winner: {match.winner === "draw" ? "Draw" : match.winner}
              </p>
            )}
          </div>
          <div className="text-center flex-1">
            <p className="text-2xl font-bold">{match.away_team}</p>
            <p className="text-xs text-gray-400 mt-1">Away</p>
          </div>
        </div>
      </div>

      {/* Consensus */}
      {consensus?.length ? (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <h2 className="font-semibold mb-4">Consensus Breakdown</h2>
          <div className="space-y-4">
            {consensus.map((c) => (
              <div key={c.id} className="space-y-2">
                <div className="flex justify-between text-sm mb-1">
                  <span className="font-medium text-indigo-300">{c.predicted_winner}</span>
                  <span className="text-gray-400">{c.pick_count ?? c.total_votes} picks</span>
                </div>
                {(c.home_probability || c.draw_probability || c.away_probability) ? (
                  <ProbBar
                    homeProb={c.home_probability}
                    drawProb={c.draw_probability}
                    awayProb={c.away_probability}
                    homeLabel={match.home_team}
                    awayLabel={match.away_team}
                  />
                ) : (
                  <ConfidenceBar value={c.confidence} />
                )}
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {/* All picks */}
      <div>
        <h2 className="font-semibold mb-4">All Influencer Picks ({picks?.length ?? 0})</h2>
        <div className="space-y-2">
          {picks?.map((p) => (
            <div key={p.id} className="bg-gray-900 border border-gray-800 rounded-lg p-3 flex items-start gap-3">
              <div className="mt-0.5">{outcomeIcon[p.outcome]}</div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-medium text-sm">@{p.influencers?.handle}</span>
                  {p.influencers?.platform && <PlatformBadge platform={p.influencers.platform} />}
                  <BetTypeBadge betType={p.bet_type} betLine={p.bet_line} />
                  {p.predicted_winner && (
                    <span className="text-indigo-300 text-sm font-medium">→ {p.predicted_winner}</span>
                  )}
                  {p.predicted_score && (
                    <span className="text-gray-400 text-xs font-mono">{p.predicted_score}</span>
                  )}
                  {p.confidence && (
                    <span className="text-gray-500 text-xs">{Math.round(p.confidence * 100)}% conf.</span>
                  )}
                  {p.market_prob_at_pick != null && (
                    <span className="text-xs text-amber-400 font-mono">
                      mkt {Math.round(p.market_prob_at_pick * 100)}%
                    </span>
                  )}
                </div>
                <p className="text-xs text-gray-400 mt-1 line-clamp-2">{p.raw_text}</p>
              </div>
              {p.post_url && (
                <a href={p.post_url} target="_blank" rel="noopener noreferrer"
                  className="text-xs text-indigo-400 hover:text-indigo-300 flex-shrink-0">
                  View →
                </a>
              )}
            </div>
          ))}
          {!picks?.length && (
            <p className="text-gray-500 text-sm text-center py-8">No picks scraped for this match yet.</p>
          )}
        </div>
      </div>
    </div>
  );
}
