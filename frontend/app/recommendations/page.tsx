"use client";
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { fetchRecommendations, type Sport } from "@/lib/api";
import ConfidenceBar from "@/components/ConfidenceBar";
import ProbBar from "@/components/ProbBar";
import { Zap, Calendar } from "lucide-react";

function fmtDate(s?: string) {
  if (!s) return "TBD";
  return new Date(s).toLocaleDateString("en-US", {
    weekday: "short", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

export default function RecommendationsPage() {
  return (
    <Suspense fallback={<div className="h-48 bg-gray-900 border border-gray-800 rounded-xl animate-pulse" />}>
      <Recommendations />
    </Suspense>
  );
}

function Recommendations() {
  const searchParams = useSearchParams();
  const sportParam = searchParams.get("sport");
  const [sportOverride, setSportOverride] = useState<Sport | "all" | null>(null);
  const sport: Sport | "all" =
    sportOverride ??
    (sportParam === "mlb" || sportParam === "football" ? sportParam : "all");

  const { data, isLoading } = useQuery({
    queryKey: ["recommendations", 30, sport],
    queryFn: () => fetchRecommendations(30, sport === "all" ? undefined : sport),
    refetchInterval: 120_000,
  });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Top Consensus Picks</h1>
          <p className="text-gray-400 text-sm mt-1">
            Elo-weighted picks from Covers, YouTube, ActionNetwork, 𝕏 &amp; TikTok cappers
          </p>
        </div>
        <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1">
          {([
            { value: "all", label: "All sports" },
            { value: "football", label: "⚽ World Cup" },
            { value: "mlb", label: "⚾ MLB" },
          ] as const).map((o) => (
            <button
              key={o.value}
              onClick={() => setSportOverride(o.value)}
              className={`px-3 py-1.5 text-sm rounded font-medium transition-colors ${
                sport === o.value ? "bg-indigo-600 text-white" : "text-gray-400 hover:text-white"
              }`}
            >
              {o.label}
            </button>
          ))}
        </div>
      </div>

      {isLoading && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[...Array(6)].map((_, i) => (
            <div key={i} className="bg-gray-900 border border-gray-800 rounded-xl p-5 h-52 animate-pulse" />
          ))}
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {data?.recommendations.map((rec) => {
          const match = rec.matches;
          const pct = Math.round(rec.confidence * 100);
          const tier =
            pct >= 75 ? { label: "Strong Pick", cls: "text-emerald-400 bg-emerald-950 border-emerald-800" }
            : pct >= 55 ? { label: "Lean",       cls: "text-yellow-400 bg-yellow-950 border-yellow-800" }
            : { label: "Uncertain",              cls: "text-red-400 bg-red-950 border-red-800" };

          const hasProbs = rec.home_probability != null || rec.draw_probability != null || rec.away_probability != null;
          const matchSport = (match as { sport?: string })?.sport;

          return (
            <div key={rec.id} className="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-3">
              <div className="flex items-center justify-between">
                <span className={`text-xs font-semibold px-2 py-0.5 rounded-full border ${tier.cls}`}>
                  <Zap className="w-3 h-3 inline mr-1" />{tier.label}
                </span>
                <span className="text-xs text-gray-500">{rec.pick_count ?? rec.total_votes} pickers</span>
              </div>

              <div>
                <p className="text-xs text-gray-400 mb-0.5 flex items-center gap-2">
                  {matchSport === "mlb" && (
                    <span className="text-blue-400 font-medium">⚾ MLB</span>
                  )}
                  {match?.stage && <span>{match.stage}</span>}
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

              <div className="bg-gray-800 rounded-lg px-3 py-2.5">
                <p className="text-xs text-gray-400 mb-1">Consensus pick</p>
                <p className="text-lg font-bold text-indigo-300">{rec.predicted_winner}</p>
              </div>

              {hasProbs && (
                <ProbBar
                  homeProb={rec.home_probability}
                  drawProb={rec.draw_probability}
                  awayProb={rec.away_probability}
                  homeLabel={match?.home_team ?? "Home"}
                  awayLabel={match?.away_team ?? "Away"}
                />
              )}

              {!hasProbs && (
                <div>
                  <div className="flex justify-between text-xs text-gray-400 mb-1">
                    <span>Confidence</span>
                    <span className="font-mono">{pct}%</span>
                  </div>
                  <ConfidenceBar value={rec.confidence} />
                </div>
              )}
            </div>
          );
        })}
      </div>

      {!isLoading && !data?.recommendations.length && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl py-16 text-center">
          <Zap className="w-10 h-10 text-gray-600 mx-auto mb-3" />
          <p className="text-gray-400 font-medium">No recommendations yet</p>
          <p className="text-gray-500 text-sm mt-1">
            Waiting for picks from Covers, YouTube, ActionNetwork, 𝕏 &amp; TikTok. Check back after the next sync.
          </p>
        </div>
      )}
    </div>
  );
}
