"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  fetchCalibration, fetchAutobets, fetchPaperTrading, fetchTrackedPicks,
  triggerAutobetRun, type Sport, type Pick, type AutobetRow,
} from "@/lib/api";
import BetTypeBadge from "@/components/BetTypeBadge";
import OutcomeBadge, { SportBadge, inferPickSport } from "@/components/OutcomeBadge";
import { formatPickDisplay } from "@/lib/pickDisplay";
import {
  TrendingUp, Target, Activity, RefreshCw, CheckCircle, XCircle,
  Clock, AlertCircle, Banknote, Layers,
} from "lucide-react";

function pct(n: number | null | undefined, decimals = 1) {
  if (n == null || isNaN(n)) return "—";
  return `${(n * 100).toFixed(decimals)}%`;
}
function money(n: number | null | undefined) {
  if (n == null || isNaN(n)) return "—";
  return `$${n.toFixed(2)}`;
}
function roiPct(n: number | null | undefined, decimals = 2) {
  if (n == null || isNaN(n)) return "—";
  return `${n >= 0 ? "+" : ""}${n.toFixed(decimals)}%`;
}
function edge(n: number | null | undefined) {
  if (n == null || isNaN(n)) return "—";
  return `${n >= 0 ? "+" : ""}${(n * 100).toFixed(1)}%`;
}

function StatusBadge({ status }: { status: AutobetRow["status"] }) {
  const map = {
    open:     { label: "Open",     cls: "bg-blue-950 text-blue-300 border-blue-800" },
    won:      { label: "Won",      cls: "bg-emerald-950 text-emerald-300 border-emerald-800" },
    lost:     { label: "Lost",     cls: "bg-red-950 text-red-300 border-red-800" },
    void:     { label: "Void",     cls: "bg-gray-800 text-gray-400 border-gray-700" },
    rejected: { label: "Rejected", cls: "bg-yellow-950 text-yellow-400 border-yellow-800" },
  };
  const cfg = map[status] ?? map.void;
  return (
    <span className={`text-xs px-1.5 py-0.5 rounded border font-medium ${cfg.cls}`}>
      {cfg.label}
    </span>
  );
}

function BankrollSparkline({
  points,
  start,
}: {
  points: { bankroll: number; bet_n: number }[];
  start: number;
}) {
  const values = points.map((p) => p.bankroll);
  const min = Math.min(...values, start) * 0.98;
  const max = Math.max(...values, start) * 1.02;
  const range = max - min || 1;
  const w = 400;
  const h = 64;
  const coords = values.map((v, i) => {
    const x = (i / Math.max(values.length - 1, 1)) * w;
    const y = h - ((v - min) / range) * h;
    return `${x},${y}`;
  }).join(" ");
  const last = values[values.length - 1];
  const up = last >= start;
  return (
    <div className="relative">
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-16" preserveAspectRatio="none">
        <polyline
          fill="none"
          stroke={up ? "#34d399" : "#f87171"}
          strokeWidth="2"
          points={coords}
        />
      </svg>
      <div className="flex justify-between text-[10px] text-gray-500 mt-1">
        <span>Start {money(start)}</span>
        <span className={up ? "text-emerald-400" : "text-red-400"}>{money(last)}</span>
      </div>
    </div>
  );
}

type Tab = "bets" | "calibration" | "paper";

function formatAutobetPick(b: { outcome_name: string; bet_type?: string; bet_line?: string | null; bet_subject?: string | null }) {
  if (b.bet_type && b.bet_type !== "moneyline") {
    const parts = [b.bet_subject, b.outcome_name, b.bet_line].filter(Boolean);
    return parts.join(" · ") || b.outcome_name;
  }
  return b.outcome_name;
}

export default function TradingPage() {
  const [tab, setTab] = useState<Tab>("bets");
  const [sportFilter, setSportFilter] = useState<Sport | "all">("all");
  const qc = useQueryClient();

  const { data: abData, isLoading: abLoading } = useQuery({
    queryKey: ["autobets", 100], queryFn: () => fetchAutobets(100), refetchInterval: 60_000,
  });
  const { data: trackedData, isLoading: trackedLoading } = useQuery({
    queryKey: ["tracked-picks", sportFilter],
    queryFn: () => fetchTrackedPicks({
      limit: 40,
      sport: sportFilter === "all" ? undefined : sportFilter,
    }),
    refetchInterval: 120_000,
  });
  const { data: calData, isLoading: calLoading } = useQuery({
    queryKey: ["calibration"], queryFn: fetchCalibration, refetchInterval: 300_000,
  });
  const { data: paperData, isLoading: paperLoading } = useQuery({
    queryKey: ["paper-trading"], queryFn: fetchPaperTrading, refetchInterval: 120_000,
  });
  const { mutate: runAutobet, isPending: runPending } = useMutation({
    mutationFn: triggerAutobetRun,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["autobets"] });
      qc.invalidateQueries({ queryKey: ["paper-trading"] });
    },
  });

  const ab = abData?.summary;
  const bets = abData?.bets ?? [];
  const filteredAutobets = sportFilter === "all"
    ? bets
    : bets.filter((b) => (b.sport ?? "football") === sportFilter);
  const openBets = filteredAutobets.filter((b) => b.status === "open");
  const historyBets = filteredAutobets.filter((b) => b.status !== "open" && b.status !== "rejected");
  const trackedPicks = trackedData?.picks ?? [];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <TrendingUp className="w-6 h-6 text-violet-400" />
            Trading Hub
          </h1>
          <p className="text-gray-400 text-sm mt-1">
            Polymarket autobet + tracked alt/prop picks + model calibration
          </p>
        </div>
        {ab && (
          <div className="flex items-center gap-3 flex-wrap justify-end">
            {ab.live_readiness && (
              <span className={`text-xs px-2 py-1 rounded-full border ${
                ab.live_readiness.live_ready
                  ? "bg-emerald-950 text-emerald-300 border-emerald-800"
                  : "bg-amber-950 text-amber-300 border-amber-800"
              }`}>
                {ab.live_readiness.live_ready
                  ? "✓ Live-ready"
                  : `Paper only · ${ab.live_readiness.settled_bets}/${ab.live_readiness.min_settled_required} settled`}
              </span>
            )}
            <span className={`text-xs font-bold px-2 py-1 rounded-full ${
              ab.mode === "live"
                ? "bg-red-900 text-red-300 border border-red-700"
                : "bg-gray-800 text-gray-300 border border-gray-700"
            }`}>
              {ab.mode === "live" ? "🔴 LIVE" : "📝 PAPER"}
            </span>
            <button
              onClick={() => runAutobet()}
              disabled={runPending}
              className="flex items-center gap-1.5 text-sm bg-violet-700 hover:bg-violet-600 disabled:opacity-50 px-3 py-1.5 rounded-lg font-medium transition-colors"
            >
              <RefreshCw className={`w-4 h-4 ${runPending ? "animate-spin" : ""}`} />
              {runPending ? "Running…" : "Run Now"}
            </button>
          </div>
        )}
      </div>

      {/* Summary stat row */}
      {ab && (
        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
          {[
            { label: "Bankroll",    value: money(ab.bankroll),          sub: `from ${money(ab.starting_bankroll)}` },
            { label: "Total P&L",   value: money(ab.total_pnl),         sub: `${ab.total_pnl >= 0 ? "+" : ""}${((ab.total_pnl / (ab.starting_bankroll || 1)) * 100).toFixed(1)}% ROI`, green: ab.total_pnl >= 0 },
            { label: "Win rate",    value: pct(ab.win_rate),            sub: `${ab.settled_bets} settled` },
            { label: "Open bets",   value: String(ab.open_bets),        sub: money(ab.open_exposure) + " at risk" },
            { label: "Total staked",value: money(ab.total_staked),      sub: "" },
          ].map((c) => (
            <div key={c.label} className="bg-gray-900 border border-gray-800 rounded-xl p-4">
              <p className="text-xs text-gray-400">{c.label}</p>
              <p className={`text-xl font-bold mt-1 ${c.green === true ? "text-emerald-400" : c.green === false ? "text-red-400" : "text-white"}`}>
                {c.value}
              </p>
              {c.sub && <p className="text-xs text-gray-500 mt-0.5">{c.sub}</p>}
            </div>
          ))}
        </div>
      )}

      {/* Learning: price-tier performance + active gates */}
      {ab?.learning && (
        <section className="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-6">
          <div>
            <h2 className="font-semibold text-sm flex items-center gap-2">
              <Activity className="w-4 h-4 text-violet-400" />
              Autobet Learning
            </h2>
            <p className="text-xs text-gray-500 mt-1">
              Settled paper bets tighten gates by price tier, sport, and upset-trap profile
            </p>
          </div>

          {/* Bankroll curve */}
          {(ab.learning.bankroll_curve?.length ?? 0) > 1 && (
            <div>
              <h3 className="text-xs font-medium text-gray-400 mb-2">Paper bankroll curve</h3>
              <BankrollSparkline points={ab.learning.bankroll_curve!} start={ab.starting_bankroll} />
            </div>
          )}

          {/* Price tiers */}
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 border-b border-gray-800">
                  <th className="pb-2 pr-4 font-medium">Price tier</th>
                  <th className="pb-2 pr-4 font-medium text-right">Settled</th>
                  <th className="pb-2 pr-4 font-medium text-right">Win%</th>
                  <th className="pb-2 pr-4 font-medium text-right">ROI</th>
                  <th className="pb-2 pr-4 font-medium text-right">Sharpe</th>
                  <th className="pb-2 pr-4 font-medium text-right">Min edge</th>
                  <th className="pb-2 font-medium text-right">Min model</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/80">
                {Object.values(ab.learning.tier_stats).map((t) => {
                  const gates = ab.learning!.active_gates[t.tier];
                  return (
                    <tr key={t.tier}>
                      <td className="py-2 pr-4 text-gray-200">{t.label}</td>
                      <td className="py-2 pr-4 text-right font-mono text-xs">{t.settled}</td>
                      <td className="py-2 pr-4 text-right font-mono text-xs">
                        {t.settled ? pct(t.win_rate, 0) : "—"}
                      </td>
                      <td className={`py-2 pr-4 text-right font-mono text-xs ${t.roi_pct >= 0 ? "text-emerald-400" : t.settled ? "text-red-400" : "text-gray-500"}`}>
                        {t.settled ? roiPct(t.roi_pct, 1) : "—"}
                      </td>
                      <td className="py-2 pr-4 text-right font-mono text-xs text-gray-400">
                        {t.sharpe != null ? t.sharpe.toFixed(2) : "—"}
                      </td>
                      <td className="py-2 pr-4 text-right font-mono text-xs text-yellow-400">
                        {gates ? edge(gates.min_edge) : "—"}
                        {gates?.adjusted && <span className="text-gray-600 ml-1">*</span>}
                      </td>
                      <td className="py-2 text-right font-mono text-xs text-indigo-300">
                        {gates ? pct(gates.min_model_prob, 0) : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Sport + upset trap side by side */}
          <div className="grid md:grid-cols-2 gap-4">
            {ab.learning.sport_stats && Object.keys(ab.learning.sport_stats).length > 0 && (
              <div>
                <h3 className="text-xs font-medium text-gray-400 mb-2">ROI by sport</h3>
                <div className="space-y-2">
                  {Object.entries(ab.learning.sport_stats).map(([sport, s]) => (
                    <div key={sport} className="flex justify-between text-sm bg-gray-800/50 rounded-lg px-3 py-2">
                      <span className="capitalize text-gray-300">{sport}</span>
                      <span className="font-mono text-xs">
                        {s.settled ? (
                          <>
                            <span className={s.roi_pct >= 0 ? "text-emerald-400" : "text-red-400"}>
                              {roiPct(s.roi_pct, 1)}
                            </span>
                            <span className="text-gray-500 ml-2">({s.settled})</span>
                          </>
                        ) : "—"}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {ab.learning.upset_trap && (
              <div>
                <h3 className="text-xs font-medium text-gray-400 mb-2">Upset trap (high conf + low market)</h3>
                <div className="space-y-2">
                  {Object.entries(ab.learning.upset_trap).map(([key, s]) => (
                    <div key={key} className="flex justify-between text-sm bg-gray-800/50 rounded-lg px-3 py-2">
                      <span className="text-gray-300 text-xs">{s.label ?? key}</span>
                      <span className="font-mono text-xs">
                        {s.settled ? (
                          <>
                            <span className="text-gray-400">{pct(s.win_rate, 0)} hit · </span>
                            <span className={s.roi_pct >= 0 ? "text-emerald-400" : "text-red-400"}>
                              {roiPct(s.roi_pct, 1)}
                            </span>
                          </>
                        ) : "—"}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          {ab.live_readiness && !ab.live_readiness.live_ready && (
            <p className="text-xs text-amber-400/90 flex items-center gap-1.5">
              <AlertCircle className="w-3.5 h-3.5 shrink-0" />
              Live mode blocked: {ab.live_readiness.message}
            </p>
          )}
          <p className="text-[10px] text-gray-600">
            * Gates auto-tighten after {ab.learning.min_tier_samples}+ settled bets with tier ROI below −10%
          </p>
        </section>
      )}

      {/* Sport filter */}
      <div className="flex flex-wrap gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 w-fit">
        {([
          { value: "all", label: "All sports" },
          { value: "football", label: "⚽ WC" },
          { value: "mlb", label: "⚾ MLB" },
        ] as const).map((s) => (
          <button
            key={s.value}
            onClick={() => setSportFilter(s.value)}
            className={`px-3 py-1.5 text-sm rounded font-medium transition-colors ${
              sportFilter === s.value ? "bg-indigo-600 text-white" : "text-gray-400 hover:text-white"
            }`}
          >
            {s.label}
          </button>
        ))}
      </div>

      {/* Tabs */}
      <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 w-fit">
        {([["bets", "Bets", Target], ["calibration", "Calibration", Activity], ["paper", "Paper Trading", Banknote]] as const).map(([t, label, Icon]) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`flex items-center gap-1.5 px-3 py-2 rounded text-sm font-medium transition-colors ${
              tab === t ? "bg-indigo-600 text-white" : "text-gray-400 hover:text-white"
            }`}
          >
            <Icon className="w-3.5 h-3.5" />
            {label}
          </button>
        ))}
      </div>

      {/* ── BETS TAB ─────────────────────────────────────────────────────── */}
      {tab === "bets" && (
        <div className="space-y-5">
          {/* Tracked alt/prop picks (scraped, not Polymarket) */}
          <section className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
            <div className="px-5 py-3 border-b border-gray-800 flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <Layers className="w-4 h-4 text-cyan-400" />
                <h2 className="font-semibold">Tracked Alt Bets ({trackedPicks.length})</h2>
              </div>
              <span className="text-[10px] text-gray-500 uppercase tracking-wide">Scraped picks · auto-settled</span>
            </div>
            {trackedLoading ? (
              <div className="p-5 space-y-2">
                {[...Array(3)].map((_, i) => (
                  <div key={i} className="h-12 bg-gray-800 rounded animate-pulse" />
                ))}
              </div>
            ) : trackedPicks.length > 0 ? (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-gray-500 border-b border-gray-800">
                    <th className="px-5 py-3 font-medium">Pick</th>
                    <th className="px-3 py-3 font-medium">Match</th>
                    <th className="px-3 py-3 font-medium">Source</th>
                    <th className="px-3 py-3 font-medium text-right">Result</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800">
                  {trackedPicks.slice(0, 30).map((p: Pick) => (
                    <tr key={p.id} className="hover:bg-gray-800/50">
                      <td className="px-5 py-3">
                        <div className="flex items-center gap-2 flex-wrap">
                          <BetTypeBadge betType={p.bet_type} betLine={p.bet_line} size="sm" />
                          <SportBadge sport={inferPickSport(p)} />
                        </div>
                        <p className="text-indigo-300 font-medium mt-1">{formatPickDisplay(p)}</p>
                      </td>
                      <td className="px-3 py-3 text-xs text-gray-400 max-w-[180px]">
                        {p.matches
                          ? `${p.matches.home_team} vs ${p.matches.away_team}`
                          : <span className="italic text-gray-600">Unlinked</span>}
                      </td>
                      <td className="px-3 py-3 text-xs text-gray-400">
                        @{p.influencers?.handle ?? "—"}
                      </td>
                      <td className="px-3 py-3 text-right">
                        <OutcomeBadge outcome={p.outcome} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="py-10 text-center text-gray-500">
                <Layers className="w-8 h-8 mx-auto mb-2 opacity-40" />
                <p>No alt/prop picks for this sport filter.</p>
              </div>
            )}
          </section>

          {/* Polymarket open bets */}
          <section className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
            <div className="px-5 py-3 border-b border-gray-800 flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <Clock className="w-4 h-4 text-blue-400" />
                <h2 className="font-semibold">Polymarket Open ({openBets.length})</h2>
              </div>
              <span className="text-[10px] text-gray-500 uppercase tracking-wide">Autobet engine</span>
            </div>
            {abLoading ? (
              <div className="p-5 space-y-2">
                {[...Array(3)].map((_, i) => (
                  <div key={i} className="h-12 bg-gray-800 rounded animate-pulse" />
                ))}
              </div>
            ) : openBets.length > 0 ? (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-gray-500 border-b border-gray-800">
                    <th className="px-5 py-3 font-medium">Market</th>
                    <th className="px-3 py-3 font-medium">Pick</th>
                    <th className="px-3 py-3 font-medium text-right">Model</th>
                    <th className="px-3 py-3 font-medium text-right">Mkt Price</th>
                    <th className="px-3 py-3 font-medium text-right">Edge</th>
                    <th className="px-3 py-3 font-medium text-right">Stake</th>
                    <th className="px-3 py-3 font-medium text-right">At</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800">
                  {openBets.map((b, i) => (
                    <tr key={i} className="hover:bg-gray-800/50">
                      <td className="px-5 py-3 max-w-[250px]">
                        <div className="flex items-center gap-1.5 mb-0.5">
                          <SportBadge sport={b.sport ?? "football"} />
                          {b.bet_type && b.bet_type !== "moneyline" && (
                            <BetTypeBadge betType={b.bet_type} betLine={b.bet_line} size="sm" />
                          )}
                        </div>
                        <p className="truncate text-gray-200">{b.question}</p>
                      </td>
                      <td className="px-3 py-3 font-medium text-indigo-300 text-sm">
                        {formatAutobetPick(b)}
                      </td>
                      <td className="px-3 py-3 text-right font-mono text-xs text-gray-300">{Math.round(b.model_prob * 100)}%</td>
                      <td className="px-3 py-3 text-right font-mono text-xs text-gray-300">{Math.round(b.market_price * 100)}%</td>
                      <td className="px-3 py-3 text-right">
                        <span className={b.edge >= 0.07 ? "text-emerald-400 font-semibold" : "text-yellow-400"}>
                          {edge(b.edge)}
                        </span>
                      </td>
                      <td className="px-3 py-3 text-right font-mono text-gray-200">{money(b.stake)}</td>
                      <td className="px-3 py-3 text-right text-xs text-gray-500">
                        {new Date(b.created_at).toLocaleDateString("en-US", { month: "short", day: "numeric" })}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="py-12 text-center text-gray-500">
                <Target className="w-8 h-8 mx-auto mb-2 opacity-40" />
                <p>No open Polymarket bets for this sport — WC moneylines only until MLB/prop markets match.</p>
              </div>
            )}
          </section>

          {/* Polymarket history */}
          {historyBets.length > 0 && (
            <section className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
              <div className="px-5 py-3 border-b border-gray-800 flex items-center gap-2">
                <CheckCircle className="w-4 h-4 text-emerald-400" />
                <h2 className="font-semibold">Polymarket Settled ({historyBets.length})</h2>
              </div>
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-gray-500 border-b border-gray-800">
                    <th className="px-5 py-3 font-medium">Market</th>
                    <th className="px-3 py-3 font-medium">Outcome</th>
                    <th className="px-3 py-3 font-medium text-right">Edge</th>
                    <th className="px-3 py-3 font-medium text-right">Stake</th>
                    <th className="px-3 py-3 font-medium text-right">P&L</th>
                    <th className="px-3 py-3 font-medium text-right">Result</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800">
                  {historyBets.slice(0, 50).map((b, i) => (
                    <tr key={i} className="hover:bg-gray-800/50">
                      <td className="px-5 py-2.5 max-w-[250px]">
                        <p className="truncate text-gray-300 text-xs">{b.question}</p>
                      </td>
                      <td className="px-3 py-2.5 text-sm text-indigo-300">
                        {formatAutobetPick(b)}
                      </td>
                      <td className="px-3 py-2.5 text-right text-xs text-gray-400">{edge(b.edge)}</td>
                      <td className="px-3 py-2.5 text-right font-mono text-xs text-gray-400">{money(b.stake)}</td>
                      <td className="px-3 py-2.5 text-right font-mono text-xs">
                        <span className={(b.pnl ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}>
                          {b.pnl != null ? `${b.pnl >= 0 ? "+" : ""}${money(b.pnl)}` : "—"}
                        </span>
                      </td>
                      <td className="px-3 py-2.5 text-right">
                        <StatusBadge status={b.status} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}
        </div>
      )}

      {/* ── CALIBRATION TAB ──────────────────────────────────────────────── */}
      {tab === "calibration" && (
        <div className="space-y-5">
          {calLoading ? (
            <div className="bg-gray-900 border border-gray-800 rounded-xl p-8 animate-pulse h-48" />
          ) : calData && (calData.total_resolved ?? 0) > 0 ? (
            <>
              {/* Top metrics */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                  <p className="text-xs text-gray-400">Brier Score (calibrated)</p>
                  <p className="text-2xl font-bold mt-1 text-emerald-400">{calData.brier_score.toFixed(4)}</p>
                  <p className="text-xs text-gray-500 mt-1">Moneyline · lower is better · random = 0.25</p>
                </div>
                <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                  <p className="text-xs text-gray-400">Raw Brier (legacy)</p>
                  <p className="text-2xl font-bold mt-1 text-gray-400">
                    {(calData.raw_brier_score ?? calData.brier_score).toFixed(4)}
                  </p>
                  <p className="text-xs text-gray-500 mt-1">Unadjusted scraper confidence</p>
                </div>
                <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                  <p className="text-xs text-gray-400">Simulated ROI</p>
                  <p className={`text-2xl font-bold mt-1 ${calData.simulated_roi_pct >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                    {calData.simulated_roi_pct >= 0 ? "+" : ""}{calData.simulated_roi_pct.toFixed(2)}%
                  </p>
                  <p className="text-xs text-gray-500 mt-1">At calibrated implied odds</p>
                </div>
                <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                  <p className="text-xs text-gray-400">Moneyline hit rate</p>
                  <p className="text-2xl font-bold mt-1">
                    {calData.moneyline?.hit_rate != null
                      ? `${(calData.moneyline.hit_rate * 100).toFixed(1)}%`
                      : "—"}
                  </p>
                  <p className="text-xs text-gray-500 mt-1">
                    {calData.moneyline?.total_resolved ?? calData.total_resolved} resolved ML picks
                  </p>
                </div>
              </div>

              {/* Calibration curve */}
              {calData.calibration_curve && Object.keys(calData.calibration_curve).length > 0 && (
                <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                  <h3 className="font-semibold mb-1">Empirical Hit Rate by Raw Confidence</h3>
                  <p className="text-xs text-gray-500 mb-4">
                    What actually happens when pickers claim each confidence band ({calData.ml_history_size ?? 0} moneyline picks in curve)
                  </p>
                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                    {Object.entries(calData.calibration_curve).map(([bucket, rate]) => (
                      <div key={bucket} className="bg-gray-800 rounded-lg p-3">
                        <p className="text-xs text-gray-400 capitalize">{bucket.replace("-", " ")}</p>
                        <p className="text-lg font-bold mt-1">{(rate * 100).toFixed(0)}%</p>
                        <p className="text-xs text-gray-500">actual win rate</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 2D calibration matrix */}
              {calData.hit_rates_2d && Object.keys(calData.hit_rates_2d).length > 0 && (
                <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 overflow-x-auto">
                  <h3 className="font-semibold mb-1">2D Calibration (confidence × market price)</h3>
                  <p className="text-xs text-gray-500 mb-4">
                    Empirical hit rate when both consensus confidence and market line at pick time are known
                    ({calData.picks_with_market_line ?? 0} picks)
                  </p>
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-gray-500">
                        <th className="pb-2 pr-2 text-left font-medium">Conf ↓ / Mkt →</th>
                        {["longshot", "underdog", "coinflip", "favorite"].map((m) => (
                          <th key={m} className="pb-2 px-2 text-center font-medium capitalize">{m}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {["low", "medium-low", "medium-high", "high"].map((conf) => (
                        <tr key={conf} className="border-t border-gray-800/60">
                          <td className="py-2 pr-2 text-gray-400 capitalize">{conf}</td>
                          {["longshot", "underdog", "coinflip", "favorite"].map((mkt) => {
                            const cell = calData.hit_rates_2d?.[conf]?.[mkt];
                            const hr = cell?.hit_rate ?? null;
                            return (
                              <td key={mkt} className="py-2 px-2 text-center font-mono">
                                {cell && cell.total > 0 ? (
                                  <span className={
                                    hr! >= 0.55 ? "text-emerald-400" :
                                    hr! >= 0.4 ? "text-yellow-400" : "text-red-400"
                                  }>
                                    {(hr! * 100).toFixed(0)}%
                                    <span className="text-gray-600 block text-[10px]">n={cell.total}</span>
                                  </span>
                                ) : (
                                  <span className="text-gray-700">—</span>
                                )}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {/* Upset trap on picks */}
              {calData.upset_trap && (calData.upset_trap.upset_trap?.total ?? 0) > 0 && (
                <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                  <h3 className="font-semibold mb-4">Upset Trap (picks calibration)</h3>
                  <div className="grid sm:grid-cols-2 gap-3">
                    {Object.entries(calData.upset_trap).map(([key, stats]) => (
                      <div key={key} className="bg-gray-800 rounded-lg p-4">
                        <p className="text-xs text-gray-400">{stats.label ?? key}</p>
                        <p className="text-lg font-bold mt-1">{(stats.hit_rate * 100).toFixed(0)}%</p>
                        <p className="text-xs text-gray-500">{stats.correct}/{stats.total} picks</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Hit rates by confidence bucket */}
              <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                <h3 className="font-semibold mb-4">Hit Rate by Calibrated Confidence</h3>
                <div className="space-y-3">
                  {Object.entries(calData.hit_rates_by_bucket).map(([bucket, stats]) => (
                    <div key={bucket} className="space-y-1">
                      <div className="flex justify-between text-xs text-gray-400">
                        <span>{bucket} confidence</span>
                        <span className="font-mono">
                          {(stats.hit_rate * 100).toFixed(0)}% ({stats.correct}/{stats.total})
                        </span>
                      </div>
                      <div className="h-2 bg-gray-800 rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full transition-all ${
                            stats.hit_rate >= 0.6 ? "bg-emerald-500" :
                            stats.hit_rate >= 0.45 ? "bg-yellow-500" : "bg-red-500"
                          }`}
                          style={{ width: `${stats.hit_rate * 100}%` }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Hit rates by bet type */}
              {Object.keys(calData.hit_rates_by_bet_type).length > 0 && (
                <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                  <h3 className="font-semibold mb-4">Hit Rate by Bet Type</h3>
                  <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                    {Object.entries(calData.hit_rates_by_bet_type).map(([bt, stats]) => (
                      <div key={bt} className="bg-gray-800 rounded-lg p-3">
                        <p className="text-xs text-gray-400 capitalize">{bt.replace(/_/g, " ")}</p>
                        <p className="text-lg font-bold mt-1">
                          {(stats.hit_rate * 100).toFixed(0)}%
                        </p>
                        <p className="text-xs text-gray-500">{stats.correct}/{stats.total} picks</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </>
          ) : (
            <div className="bg-gray-900 border border-gray-800 rounded-xl py-16 text-center">
              <Activity className="w-10 h-10 text-gray-600 mx-auto mb-3" />
              <p className="text-gray-400">No calibration data yet — picks need to resolve first.</p>
            </div>
          )}
        </div>
      )}

      {/* ── PAPER TRADING TAB ─────────────────────────────────────────────── */}
      {tab === "paper" && (
        <div className="space-y-5">
          {paperLoading ? (
            <div className="bg-gray-900 border border-gray-800 rounded-xl p-8 animate-pulse h-48" />
          ) : paperData ? (
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
              {[
                { label: "Virtual bankroll",  value: money(paperData.bankroll),              green: true },
                { label: "Total P&L",         value: money(paperData.total_pnl),           green: (paperData.total_pnl ?? 0) >= 0 },
                { label: "ROI",               value: roiPct(paperData.roi_pct),            green: (paperData.roi_pct ?? 0) >= 0 },
                { label: "Win rate",          value: pct(paperData.win_rate),              green: true },
                { label: "Total bets",        value: String(paperData.total_bets ?? 0),    green: null },
                { label: "Pending bets",      value: String(paperData.pending_bets ?? 0),  green: null },
                { label: "Total wagered",     value: money(paperData.total_wagered),       green: null },
              ].map((c) => (
                <div key={c.label} className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                  <p className="text-xs text-gray-400">{c.label}</p>
                  <p className={`text-2xl font-bold mt-1 ${
                    c.green === true ? "text-emerald-400" :
                    c.green === false ? "text-red-400" : "text-white"
                  }`}>
                    {c.value}
                  </p>
                </div>
              ))}
            </div>
          ) : (
            <div className="bg-gray-900 border border-gray-800 rounded-xl py-16 text-center">
              <Banknote className="w-10 h-10 text-gray-600 mx-auto mb-3" />
              <p className="text-gray-400">No paper trading data yet.</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
