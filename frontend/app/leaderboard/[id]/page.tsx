"use client";
import { useQuery } from "@tanstack/react-query";
import { use } from "react";
import { fetchInfluencer } from "@/lib/api";
import PlatformBadge from "@/components/PlatformBadge";
import BetTypeBadge from "@/components/BetTypeBadge";
import { ArrowLeft, CheckCircle, XCircle, Clock, TrendingUp } from "lucide-react";
import Link from "next/link";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import { fmtFollowers } from "@/lib/platforms";

const outcomeIcon: Record<string, React.ReactNode> = {
  correct: <CheckCircle className="w-4 h-4 text-emerald-400 flex-shrink-0" />,
  incorrect: <XCircle className="w-4 h-4 text-red-400 flex-shrink-0" />,
  pending: <Clock className="w-4 h-4 text-yellow-400 flex-shrink-0" />,
};

function pct(n: number) { return `${(n * 100).toFixed(1)}%`; }

export default function InfluencerDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data, isLoading } = useQuery({
    queryKey: ["influencer", id],
    queryFn: () => fetchInfluencer(id),
  });

  if (isLoading) return <div className="space-y-4">{[...Array(3)].map((_, i) => <div key={i} className="h-24 bg-gray-900 rounded-xl border border-gray-800 animate-pulse" />)}</div>;

  const { influencer: inf, recent_picks, history } = data ?? {};
  if (!inf) return <p className="text-gray-400">Influencer not found.</p>;

  const chartData = [...(history ?? [])].reverse().map((h: any) => ({
    date: h.snapshot_date,
    elo: Math.round(h.elo_score ?? 1000),
    accuracy: +(h.accuracy_rate * 100).toFixed(1),
  }));

  return (
    <div className="space-y-6">
      <Link href="/leaderboard" className="flex items-center gap-1.5 text-sm text-gray-400 hover:text-white">
        <ArrowLeft className="w-4 h-4" /> Back to leaderboard
      </Link>

      {/* Profile header */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <div className="flex items-start gap-4">
          {inf.avatar_url && (
            <img src={inf.avatar_url} alt={inf.handle} className="w-16 h-16 rounded-full bg-gray-800" />
          )}
          <div className="flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <h1 className="text-xl font-bold">@{inf.handle}</h1>
              <PlatformBadge platform={inf.platform} />
            </div>
            {inf.display_name && <p className="text-gray-400 text-sm mt-0.5">{inf.display_name}</p>}
            {inf.follower_count > 0 && (
              <p className="text-xs text-gray-500 mt-1">{fmtFollowers(inf.follower_count)} followers</p>
            )}
            {inf.bio && <p className="text-gray-500 text-sm mt-2 max-w-lg">{inf.bio}</p>}
          </div>
          {inf.profile_url && (
            <a href={inf.profile_url} target="_blank" rel="noopener noreferrer"
              className="text-sm text-indigo-400 hover:text-indigo-300">
              View profile →
            </a>
          )}
        </div>

        {/* Stats row */}
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-4 mt-6">
          {[
            { label: "Elo Score",   value: Math.round(inf.elo_score ?? 1000),     accent: "text-indigo-300" },
            { label: "Accuracy",    value: pct(inf.accuracy_rate),                accent: "text-emerald-400" },
            { label: "Total Picks", value: String(inf.total_picks),               accent: "text-white" },
            { label: "Streak",      value: `${(inf.pick_streak ?? 0) > 0 ? "+" : ""}${inf.pick_streak ?? 0}`, accent: (inf.pick_streak ?? 0) >= 0 ? "text-emerald-400" : "text-red-400" },
            {
              label: "Avg CLV",
              value: inf.avg_clv != null ? `${inf.avg_clv >= 0 ? "+" : ""}${(inf.avg_clv * 100).toFixed(1)}%` : "—",
              accent: inf.avg_clv == null ? "text-gray-500" : inf.avg_clv >= 0 ? "text-emerald-400" : "text-red-400",
            },
          ].map((s) => (
            <div key={s.label} className="bg-gray-800 rounded-lg p-3">
              <p className="text-xs text-gray-400">{s.label}</p>
              <p className={`text-2xl font-bold ${s.accent}`}>{s.value}</p>
            </div>
          ))}
        </div>

        {inf.elo_by_sport && Object.keys(inf.elo_by_sport).length > 0 && (
          <div className="flex flex-wrap gap-2 mt-4 pt-4 border-t border-gray-800">
            <span className="text-xs text-gray-500 w-full">Per-sport Elo</span>
            {Object.entries(inf.elo_by_sport).map(([sport, elo]) => (
              <span
                key={sport}
                className="text-xs px-2.5 py-1 rounded-full bg-gray-800 text-indigo-300 font-mono"
              >
                {sport === "mlb" ? "⚾" : sport === "football" ? "⚽" : sport} {Math.round(elo)}
              </span>
            ))}
            {inf.avg_clv_by_sport && Object.keys(inf.avg_clv_by_sport).length > 0 && (
              <>
                <span className="text-xs text-gray-500 w-full mt-2">Per-sport CLV</span>
                {Object.entries(inf.avg_clv_by_sport).map(([sport, clv]) => (
                  <span
                    key={`clv-${sport}`}
                    className={`text-xs px-2.5 py-1 rounded-full bg-gray-800 font-mono ${
                      clv >= 0 ? "text-emerald-400" : "text-red-400"
                    }`}
                  >
                    {sport === "mlb" ? "⚾" : sport === "football" ? "⚽" : sport}{" "}
                    {clv >= 0 ? "+" : ""}{(clv * 100).toFixed(1)}%
                  </span>
                ))}
              </>
            )}
          </div>
        )}
      </div>

      {/* Elo history chart */}
      {chartData.length > 1 && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <h2 className="font-semibold mb-4 flex items-center gap-2">
            <TrendingUp className="w-4 h-4 text-indigo-400" /> Elo History
          </h2>
          <ResponsiveContainer width="100%" height={160}>
            <LineChart data={chartData}>
              <XAxis dataKey="date" tick={{ fill: "#6b7280", fontSize: 10 }} tickLine={false} />
              <YAxis domain={["auto", "auto"]} tick={{ fill: "#6b7280", fontSize: 10 }} tickLine={false} width={40} />
              <Tooltip
                contentStyle={{ background: "#111827", border: "1px solid #374151", borderRadius: 8 }}
                labelStyle={{ color: "#9ca3af" }}
              />
              <Line type="monotone" dataKey="elo" stroke="#818cf8" dot={false} strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Recent picks */}
      <div>
        <h2 className="font-semibold mb-4">Recent Picks ({recent_picks?.length ?? 0})</h2>
        <div className="space-y-2">
          {recent_picks?.map((p: any) => (
            <div key={p.id} className="bg-gray-900 border border-gray-800 rounded-lg p-3 flex items-start gap-3">
              {outcomeIcon[p.outcome] ?? <Clock className="w-4 h-4 text-gray-600 flex-shrink-0" />}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap text-sm">
                  <BetTypeBadge betType={p.bet_type} betLine={p.bet_line} />
                  {p.predicted_winner && (
                    <span className="font-medium text-indigo-300">→ {p.predicted_winner}</span>
                  )}
                  {p.predicted_score && (
                    <span className="font-mono text-gray-400 text-xs">{p.predicted_score}</span>
                  )}
                  {p.market_prob_at_pick != null && (
                    <span className="text-xs text-amber-400 font-mono">mkt {Math.round(p.market_prob_at_pick * 100)}%</span>
                  )}
                  <span className={`text-xs font-medium capitalize ${p.outcome === "correct" ? "text-emerald-400" : p.outcome === "incorrect" ? "text-red-400" : "text-yellow-400"}`}>
                    {p.outcome}
                  </span>
                </div>
                <p className="text-xs text-gray-500 mt-1 line-clamp-2">{p.raw_text}</p>
              </div>
              {p.post_url && (
                <a href={p.post_url} target="_blank" rel="noopener noreferrer"
                  className="text-xs text-indigo-400 hover:text-indigo-300 flex-shrink-0">
                  View →
                </a>
              )}
            </div>
          ))}
          {!recent_picks?.length && <p className="text-gray-500 text-sm py-4 text-center">No picks yet.</p>}
        </div>
      </div>
    </div>
  );
}
