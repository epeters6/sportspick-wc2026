"use client";
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { fetchPropPicks, type BetType, type Sport } from "@/lib/api";
import BetTypeBadge from "@/components/BetTypeBadge";
import PlatformBadge from "@/components/PlatformBadge";
import OutcomeBadge, { SportBadge, inferPickSport } from "@/components/OutcomeBadge";
import { Layers } from "lucide-react";
import { PLATFORM_FILTER_OPTIONS } from "@/lib/platforms";
import { formatPickDisplay } from "@/lib/pickDisplay";

const FILTERS: { value: string; label: string }[] = [
  { value: "all", label: "All props" },
  { value: "draw", label: "Draw" },
  { value: "total_goals", label: "Goals O/U" },
  { value: "total_runs", label: "Runs O/U" },
  { value: "btts", label: "BTTS" },
  { value: "corners", label: "Corners" },
  { value: "cards", label: "Cards" },
  { value: "team_shots", label: "Team shots" },
  { value: "team_tackles", label: "Tackles" },
  { value: "team_total_runs", label: "Team runs" },
  { value: "first_half_goals", label: "1H O/U" },
  { value: "first_five_runs", label: "F5 runs" },
  { value: "player_scorer", label: "Scorer" },
  { value: "player_shots", label: "P. Shots" },
  { value: "player_strikeouts", label: "Strikeouts" },
  { value: "player_hits", label: "Hits" },
  { value: "player_rbis", label: "RBIs" },
];

export default function PropsPageWrapper() {
  return (
    <Suspense fallback={<div className="h-48 bg-gray-900 border border-gray-800 rounded-xl animate-pulse" />}>
      <PropsPage />
    </Suspense>
  );
}

function PropsPage() {
  const searchParams = useSearchParams();
  const sportParam = searchParams.get("sport");
  const [filter, setFilter] = useState("all");
  const [sportOverride, setSportOverride] = useState<Sport | "all" | null>(null);
  const [platform, setPlatform] = useState("all");
  const sport: Sport | "all" =
    sportOverride ??
    (sportParam === "mlb" || sportParam === "football" ? sportParam : "all");

  const { data, isLoading } = useQuery({
    queryKey: ["prop-picks", filter, sport, platform],
    queryFn: () =>
      fetchPropPicks({
        limit: 100,
        bet_type: filter === "all" ? undefined : (filter as BetType),
        sport: sport === "all" ? undefined : sport,
      }),
    refetchInterval: 120_000,
  });

  const picks = data?.picks.filter((p) =>
    platform === "all" || p.influencers?.platform === platform || p.platform === platform
  ) ?? [];

  const settled = picks.filter((p) => p.outcome === "correct" || p.outcome === "incorrect").length;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <Layers className="w-6 h-6 text-cyan-400" />
          Alt Bets &amp; Props
        </h1>
        <p className="text-gray-400 text-sm mt-1">
          Draws, O/U, BTTS, corners, player props — World Cup &amp; MLB from all sources
        </p>
        {data && (
          <p className="text-xs text-gray-500 mt-1">
            {data.total} props shown · {settled} settled
          </p>
        )}
      </div>

      <div className="flex flex-wrap gap-2">
        <div className="flex flex-wrap gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 w-fit">
          {([
            { value: "all", label: "All sports" },
            { value: "football", label: "⚽ WC" },
            { value: "mlb", label: "⚾ MLB" },
          ] as const).map((s) => (
            <button
              key={s.value}
              onClick={() => setSportOverride(s.value)}
              className={`px-3 py-1.5 text-sm rounded font-medium transition-colors ${
                sport === s.value ? "bg-indigo-600 text-white" : "text-gray-400 hover:text-white"
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 w-fit">
          {PLATFORM_FILTER_OPTIONS.map((p) => (
            <button
              key={p.value}
              onClick={() => setPlatform(p.value)}
              className={`px-3 py-1.5 text-sm rounded font-medium transition-colors ${
                platform === p.value ? "bg-indigo-600 text-white" : "text-gray-400 hover:text-white"
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 w-fit">
        {FILTERS.map((f) => (
          <button
            key={f.value}
            onClick={() => setFilter(f.value)}
            className={`px-3 py-1.5 text-sm rounded font-medium transition-colors ${
              filter === f.value ? "bg-indigo-600 text-white" : "text-gray-400 hover:text-white"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      <div className="space-y-2">
        {isLoading &&
          [...Array(6)].map((_, i) => (
            <div key={i} className="bg-gray-900 border border-gray-800 rounded-xl h-20 animate-pulse" />
          ))}

        {picks.map((p) => {
          const m = p.matches;
          const pickSport = inferPickSport(p);
          return (
            <div
              key={p.id}
              className="bg-gray-900 border border-gray-800 rounded-xl p-4 flex flex-col sm:flex-row sm:items-center gap-3"
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap mb-1">
                  <BetTypeBadge betType={p.bet_type} betLine={p.bet_line} size="sm" />
                  <OutcomeBadge outcome={p.outcome} />
                  {pickSport && <SportBadge sport={pickSport} />}
                  {p.influencers?.platform && (
                    <PlatformBadge platform={p.influencers.platform} />
                  )}
                  <span className="text-sm font-medium">@{p.influencers?.handle}</span>
                </div>
                {m ? (
                  <p className="text-sm text-gray-300">
                    {m.home_team} vs {m.away_team}
                    {m.stage && <span className="text-gray-500"> · {m.stage}</span>}
                  </p>
                ) : pickSport && (
                  <p className="text-sm text-gray-500 italic">Match not linked yet</p>
                )}
                <p className="text-indigo-300 font-semibold mt-1">
                  → {formatPickDisplay(p)}
                </p>
                <p className="text-xs text-gray-500 mt-1 line-clamp-2">{p.raw_text}</p>
              </div>
              {p.post_url && (
                <a
                  href={p.post_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-indigo-400 hover:text-indigo-300 flex-shrink-0"
                >
                  Source →
                </a>
              )}
            </div>
          );
        })}

        {!isLoading && !picks.length && (
          <div className="bg-gray-900 border border-gray-800 rounded-xl py-16 text-center">
            <Layers className="w-10 h-10 text-gray-600 mx-auto mb-3" />
            <p className="text-gray-400">No prop picks for this filter.</p>
            <p className="text-gray-500 text-sm mt-1 max-w-md mx-auto">
              Try &quot;All sports&quot; or run a sync — props include O/U, BTTS, player K/hits, team shots, etc.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
