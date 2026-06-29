"use client";
import { useState } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import {
  fetchPlatformStats, fetchLeaderboard,
} from "@/lib/api";
import PlatformBadge from "@/components/PlatformBadge";
import { Users, ArrowRight, Radio } from "lucide-react";
import { SYNC_SOURCES, fmtFollowers } from "@/lib/platforms";

export default function SourcesPage() {
  const searchParams = useSearchParams();
  const platformParam = searchParams.get("platform");
  const [activeOverride, setActiveOverride] = useState<string | null>(null);
  const activePlatform = activeOverride ?? platformParam;

  const { data: stats } = useQuery({
    queryKey: ["platform-stats"],
    queryFn: fetchPlatformStats,
    refetchInterval: 120_000,
  });

  const { data: topByPlatform } = useQuery({
    queryKey: ["sources-leaderboard", activePlatform],
    queryFn: () =>
      fetchLeaderboard({
        limit: 10,
        sort_by: "total_picks",
        platform: activePlatform ?? undefined,
      }),
    enabled: !!activePlatform,
  });

  const totalInfluencers = stats
    ? Object.values(stats.influencers_by_platform).reduce((a, b) => a + b, 0)
    : 0;
  const totalPicks = stats
    ? Object.values(stats.picks_by_platform).reduce((a, b) => a + b, 0)
    : 0;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <Users className="w-6 h-6 text-indigo-400" />
          Pick Sources
        </h1>
        <p className="text-gray-400 text-sm mt-1">
          {totalInfluencers} influencers · {totalPicks.toLocaleString()} picks scraped across all platforms
        </p>
      </div>

      {/* Platform overview cards */}
      {stats && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {SYNC_SOURCES.map((s) => {
            const inf = stats.influencers_by_platform[s.id] ?? 0;
            const picks = stats.picks_by_platform[s.id] ?? 0;
            const sourceMeta = stats.active_sources.find((a) => a.id === s.id);
            const isActive = activePlatform === s.id;

            return (
              <button
                key={s.id}
                onClick={() => setActiveOverride(isActive ? null : s.id)}
                className={`text-left bg-gray-900 border rounded-xl p-5 transition-colors ${
                  isActive ? "border-indigo-500 ring-1 ring-indigo-500/30" : "border-gray-800 hover:border-gray-700"
                }`}
              >
                <div className="flex items-center justify-between mb-2">
                  <PlatformBadge platform={s.id} />
                  {!sourceMeta?.always_on && (
                    <span className="text-[10px] text-gray-500 flex items-center gap-1">
                      <Radio className="w-3 h-3" /> cookie auth
                    </span>
                  )}
                </div>
                <p className="text-2xl font-bold">{inf}</p>
                <p className="text-xs text-gray-400">influencers tracked</p>
                <p className="text-sm text-gray-500 mt-2">{picks.toLocaleString()} picks scraped</p>
                {sourceMeta?.note && (
                  <p className="text-[10px] text-gray-600 mt-1">{sourceMeta.note}</p>
                )}
              </button>
            );
          })}
        </div>
      )}

      {/* Top influencers for selected platform */}
      {activePlatform && (
        <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold">
              Top <PlatformBadge platform={activePlatform} /> cappers
            </h2>
            <Link
              href={`/leaderboard?platform=${activePlatform}`}
              className="text-xs text-indigo-400 flex items-center gap-1 hover:text-indigo-300"
            >
              Full leaderboard <ArrowRight className="w-3 h-3" />
            </Link>
          </div>
          <div className="space-y-2">
            {topByPlatform?.influencers.map((inf, i) => (
              <Link
                key={inf.id}
                href={`/leaderboard/${inf.id}`}
                className="flex items-center gap-3 hover:bg-gray-800 rounded-lg p-2 -mx-2 transition-colors"
              >
                <span className="w-5 text-center text-xs text-gray-500 font-mono">{i + 1}</span>
                <div className="flex-1 min-w-0">
                  <span className="font-medium text-sm">@{inf.handle}</span>
                  {inf.display_name && (
                    <span className="text-gray-400 text-sm ml-1.5">{inf.display_name}</span>
                  )}
                </div>
                <span className="text-xs text-gray-400">
                  {inf.follower_count > 0 ? fmtFollowers(inf.follower_count) : "—"}
                </span>
                <span className="text-sm font-mono text-indigo-300">{inf.total_picks} picks</span>
              </Link>
            ))}
            {!topByPlatform?.influencers.length && (
              <p className="text-sm text-gray-500 py-4 text-center">No influencers on this platform yet.</p>
            )}
          </div>
        </section>
      )}

      {/* Sync info */}
      <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
        <h2 className="font-semibold text-sm mb-2">Sync Pipeline</h2>
        <p className="text-sm text-gray-400 leading-relaxed">
          Every 30 minutes the sync job runs Covers.com, YouTube, and ActionNetwork scrapers,
          plus World Cup and MLB schedule updates. 𝕏/Twitter (~60+ seeded cappers with keyword discovery)
          and TikTok run when session cookies are configured in the environment.
        </p>
        <div className="flex flex-wrap gap-2 mt-3">
          {SYNC_SOURCES.map((s) => (
            <span key={s.id} className={`text-xs px-2 py-1 rounded-full bg-gray-800 ${s.color}`}>
              {s.label}
            </span>
          ))}
        </div>
      </section>
    </div>
  );
}
