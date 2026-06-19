"use client";
import { useQuery } from "@tanstack/react-query";
import { fetchRecommendations } from "@/lib/api";
import ConfidenceBar from "@/components/ConfidenceBar";
import { Zap, Calendar } from "lucide-react";

function fmtDate(s?: string) {
  if (!s) return "TBD";
  return new Date(s).toLocaleDateString("en-US", {
    weekday: "short", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

export default function Recommendations() {
  const { data, isLoading } = useQuery({
    queryKey: ["recommendations", 20],
    queryFn: () => fetchRecommendations(20),
    refetchInterval: 120_000,
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Top Consensus Picks</h1>
        <p className="text-gray-400 text-sm mt-1">
          AI-weighted picks from the most accurate influencers. Confidence = Elo-weighted agreement.
        </p>
      </div>

      {isLoading && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[...Array(6)].map((_, i) => (
            <div key={i} className="bg-gray-900 border border-gray-800 rounded-xl p-5 h-40 animate-pulse" />
          ))}
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {data?.recommendations.map((rec) => {
          const match = rec.matches;
          const pct = Math.round(rec.confidence * 100);
          const tier =
            pct >= 75 ? { label: "Strong Pick", cls: "text-emerald-400 bg-emerald-950 border-emerald-800" }
            : pct >= 55 ? { label: "Lean", cls: "text-yellow-400 bg-yellow-950 border-yellow-800" }
            : { label: "Uncertain", cls: "text-red-400 bg-red-950 border-red-800" };

          return (
            <div key={rec.id} className="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-4">
              {/* Tier badge */}
              <div className="flex items-center justify-between">
                <span className={`text-xs font-semibold px-2 py-0.5 rounded-full border ${tier.cls}`}>
                  <Zap className="w-3 h-3 inline mr-1" />{tier.label}
                </span>
                <span className="text-xs text-gray-500">{rec.total_votes} votes</span>
              </div>

              {/* Match */}
              <div>
                <p className="text-xs text-gray-400 mb-1">
                  {match?.stage && <span className="mr-1.5">{match.stage}</span>}
                  {match && (
                    <span className="flex items-center gap-1 text-gray-500">
                      <Calendar className="w-3 h-3" />{fmtDate(match.scheduled_at)}
                    </span>
                  )}
                </p>
                <p className="font-semibold">
                  {match?.home_team ?? "?"} <span className="text-gray-500">vs</span> {match?.away_team ?? "?"}
                </p>
              </div>

              {/* Prediction */}
              <div className="bg-gray-800 rounded-lg px-3 py-2.5">
                <p className="text-xs text-gray-400 mb-1">Consensus winner</p>
                <p className="text-lg font-bold text-indigo-300">{rec.predicted_winner}</p>
              </div>

              {/* Confidence */}
              <div>
                <div className="flex justify-between text-xs text-gray-400 mb-1">
                  <span>Confidence</span>
                  <span className="font-mono">{pct}%</span>
                </div>
                <ConfidenceBar value={rec.confidence} />
              </div>
            </div>
          );
        })}
      </div>

      {!isLoading && !data?.recommendations.length && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl py-16 text-center">
          <Zap className="w-10 h-10 text-gray-600 mx-auto mb-3" />
          <p className="text-gray-400 font-medium">No recommendations yet</p>
          <p className="text-gray-500 text-sm mt-1">
            Waiting for picks to be scraped. Check back after the first scrape cycle.
          </p>
        </div>
      )}
    </div>
  );
}
