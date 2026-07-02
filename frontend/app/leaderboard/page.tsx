"use client";
import { useState } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { fetchLeaderboard } from "@/lib/api";
import PlatformBadge from "@/components/PlatformBadge";
import { TrendingUp, TrendingDown } from "lucide-react";
import { PLATFORM_FILTER_OPTIONS, fmtFollowers } from "@/lib/platforms";

const SORT_OPTIONS = [
  { value: "elo_score",     label: "Elo Score" },
  { value: "accuracy_rate", label: "Accuracy" },
  { value: "avg_clv",       label: "CLV" },
  { value: "total_picks",   label: "Most Picks" },
  { value: "follower_count",label: "Followers" },
];

function pct(n: number) { return `${(n * 100).toFixed(1)}%`; }

export default function Leaderboard() {
  const searchParams = useSearchParams();
  const platformFromUrl = searchParams.get("platform");
  const [platformOverride, setPlatformOverride] = useState<string | null>(null);
  const [sortBy, setSortBy] = useState("total_picks");
  const platform = platformOverride ?? platformFromUrl ?? "all";

  const { data, isLoading } = useQuery({
    queryKey: ["leaderboard", sortBy, platform],
    queryFn: () =>
      fetchLeaderboard({
        limit: 100,
        sort_by: sortBy,
        platform: platform === "all" ? undefined : platform,
      }),
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Influencer Leaderboard</h1>
        <p className="text-gray-400 text-sm mt-1">
          Ranked across 𝕏, TikTok, Covers, Pickswise, YouTube, ActionNetwork &amp; more
          <span className="text-gray-500"> · per-sport Elo when enough picks</span>
        </p>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3">
        <div className="flex flex-wrap gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1">
          {SORT_OPTIONS.map((o) => (
            <button
              key={o.value}
              onClick={() => setSortBy(o.value)}
              className={`px-3 py-1.5 rounded text-sm font-medium transition-colors ${
                sortBy === o.value ? "bg-indigo-600 text-white" : "text-gray-400 hover:text-white"
              }`}
            >
              {o.label}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1">
          {PLATFORM_FILTER_OPTIONS.map((p) => (
            <button
              key={p.value}
              onClick={() => setPlatformOverride(p.value === "all" ? null : p.value)}
              className={`px-3 py-1.5 rounded text-sm font-medium transition-colors ${
                platform === p.value ? "bg-indigo-600 text-white" : "text-gray-400 hover:text-white"
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      {/* Table */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-gray-500 bg-gray-900 border-b border-gray-800">
                <th className="px-4 py-3 font-medium">#</th>
                <th className="px-4 py-3 font-medium">Influencer</th>
                <th className="px-4 py-3 font-medium">Platform</th>
                <th className="px-4 py-3 font-medium text-right">Followers</th>
                <th className="px-4 py-3 font-medium text-right">Elo</th>
                <th className="px-4 py-3 font-medium text-right">Accuracy</th>
                <th className="px-4 py-3 font-medium text-right">Picks</th>
                <th className="px-4 py-3 font-medium text-right">Streak</th>
                <th className="px-4 py-3 font-medium text-right">Avg CLV</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {isLoading &&
                [...Array(10)].map((_, i) => (
                  <tr key={i}>
                    {[...Array(9)].map((_, j) => (
                      <td key={j} className="px-4 py-3">
                        <div className="h-4 bg-gray-800 rounded animate-pulse" />
                      </td>
                    ))}
                  </tr>
                ))}
              {data?.influencers.map((inf, i) => (
                <tr
                  key={inf.id}
                  className="hover:bg-gray-800/50 transition-colors cursor-pointer"
                >
                  <td className="px-4 py-3 text-gray-500 font-mono text-xs">{i + 1}</td>
                  <td className="px-4 py-3">
                    <Link
                      href={`/leaderboard/${inf.id}`}
                      className="font-medium hover:text-indigo-300 transition-colors"
                    >
                      @{inf.handle}
                      {inf.display_name && (
                        <span className="text-gray-400 font-normal ml-1.5">{inf.display_name}</span>
                      )}
                    </Link>
                  </td>
                  <td className="px-4 py-3">
                    <PlatformBadge platform={inf.platform} />
                  </td>
                  <td className="px-4 py-3 text-right font-mono text-xs text-gray-400">
                    {inf.follower_count > 0 ? fmtFollowers(inf.follower_count) : "—"}
                  </td>
                  <td className="px-4 py-3 text-right font-mono text-indigo-300">
                    <div>{Math.round(inf.elo_score)}</div>
                    {inf.elo_by_sport && Object.keys(inf.elo_by_sport).length > 0 && (
                      <div className="text-[10px] text-gray-500 font-sans mt-0.5">
                        {inf.elo_by_sport.mlb != null && (
                          <span className="mr-1.5">⚾ {Math.round(inf.elo_by_sport.mlb)}</span>
                        )}
                        {inf.elo_by_sport.football != null && (
                          <span>⚽ {Math.round(inf.elo_by_sport.football)}</span>
                        )}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right font-mono text-emerald-400">
                    {pct(inf.accuracy_rate)}
                  </td>
                  <td className="px-4 py-3 text-right text-gray-300">{inf.total_picks}</td>
                  <td className="px-4 py-3 text-right">
                    <span className={`flex items-center justify-end gap-1 font-medium ${inf.pick_streak >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                      {inf.pick_streak >= 0 ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
                      {Math.abs(inf.pick_streak)}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right font-mono text-xs">
                    {inf.avg_clv != null ? (
                      <span className={inf.avg_clv >= 0 ? "text-emerald-400" : "text-red-400"}>
                        {inf.avg_clv >= 0 ? "+" : ""}{(inf.avg_clv * 100).toFixed(1)}%
                      </span>
                    ) : (
                      <span className="text-gray-600">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!isLoading && !data?.influencers.length && (
          <div className="py-12 text-center text-gray-500">
            No influencers with picks yet. Run <code className="bg-gray-800 px-1 rounded">/seed</code> and wait for the first scrape.
          </div>
        )}
      </div>
    </div>
  );
}
