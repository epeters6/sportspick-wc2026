"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { fetchMatches, Match } from "@/lib/api";
import ConfidenceBar from "@/components/ConfidenceBar";
import { CheckCircle, Clock } from "lucide-react";

function fmtDate(s: string) {
  return new Date(s).toLocaleDateString("en-US", {
    weekday: "short", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

export default function MatchesPage() {
  const [upcoming, setUpcoming] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["matches", upcoming],
    queryFn: () => fetchMatches({ upcoming_only: upcoming }),
    refetchInterval: 60_000,
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">World Cup 2026 Matches</h1>
          <p className="text-gray-400 text-sm mt-1">Live scores and consensus pick data</p>
        </div>
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
              {/* Status */}
              <div className="flex-shrink-0">
                {m.is_final ? (
                  <CheckCircle className="w-5 h-5 text-emerald-500" />
                ) : (
                  <Clock className="w-5 h-5 text-yellow-500" />
                )}
              </div>

              {/* Teams + Score */}
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
                  <span>{m.stage}</span>
                  <span>·</span>
                  <span>{fmtDate(m.scheduled_at)}</span>
                  {m.venue && <><span>·</span><span>{m.venue}</span></>}
                </div>
              </div>

              {/* Consensus */}
              {cp && (
                <div className="text-right flex-shrink-0 w-40">
                  <p className="text-xs text-gray-400 mb-1">Consensus</p>
                  <p className="font-semibold text-indigo-300 text-sm">{cp.predicted_winner}</p>
                  <div className="mt-1">
                    <ConfidenceBar value={cp.confidence} />
                  </div>
                </div>
              )}
            </Link>
          );
        })}
        {!isLoading && !data?.matches.length && (
          <div className="bg-gray-900 border border-gray-800 rounded-xl py-12 text-center text-gray-500">
            No matches found. Sync World Cup data via POST /sync.
          </div>
        )}
      </div>
    </div>
  );
}
