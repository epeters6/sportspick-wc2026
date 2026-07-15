"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import {
  fetchReadiness, fetchLiveToggle, setLiveToggle, fetchGuardian,
} from "@/lib/api";
import ReadinessWidget from "@/components/ReadinessWidget";
import VibrantStatCard from "@/components/VibrantStatCard";
import {
  Shield, ShieldAlert, ShieldCheck, ToggleLeft, ToggleRight,
  TrendingUp, Target, AlertTriangle, Zap, Lock, Unlock,
} from "lucide-react";

function pct(n: number, d = 1) {
  return `${(n * 100).toFixed(d)}%`;
}

export default function LiveReadinessPage() {
  const qc = useQueryClient();

  const { data: readiness, isLoading } = useQuery({
    queryKey: ["readiness"],
    queryFn: fetchReadiness,
    refetchInterval: 60_000,
  });
  const { data: toggleState } = useQuery({
    queryKey: ["live-toggle"],
    queryFn: fetchLiveToggle,
    refetchInterval: 30_000,
  });
  const { data: guardian } = useQuery({
    queryKey: ["guardian"],
    queryFn: fetchGuardian,
    refetchInterval: 30_000,
  });

  const { mutate: flipToggle, isPending: toggling } = useMutation({
    mutationFn: (enabled: boolean) => setLiveToggle(enabled),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["live-toggle"] });
      qc.invalidateQueries({ queryKey: ["readiness"] });
      qc.invalidateQueries({ queryKey: ["autobets"] });
    },
  });

  const global = readiness?.global;
  const canEnable = readiness?.can_enable_live ?? false;
  const effectiveLive = readiness?.effective_live ?? false;
  const toggleOn = toggleState?.toggle?.enabled ?? false;
  const blockers = readiness?.blockers ?? [];
  const guardianHalted = guardian?.halted ?? false;

  return (
    <div className="space-y-8 pb-12">
      <div className="flex flex-col md:flex-row md:items-end justify-between gap-4 border-b border-gray-800/50 pb-6">
        <div>
          <h1 className="text-4xl font-extrabold flex items-center gap-3 text-transparent bg-clip-text bg-gradient-to-r from-emerald-400 to-cyan-400">
            <Zap className="w-8 h-8 text-emerald-400" />
            Live Trading Readiness
          </h1>
          <p className="text-gray-400 text-sm mt-2 max-w-2xl">
            Shadow trading runs on every sync cycle. Live mode only activates when the paper track record,
            guardian, and this toggle all agree.
          </p>
        </div>
        <div className={`text-xs font-bold px-4 py-2 rounded-full uppercase tracking-wider border ${
          effectiveLive
            ? "bg-red-500/20 text-red-400 border-red-500/30 animate-pulse"
            : "bg-indigo-500/20 text-indigo-300 border-indigo-500/30"
        }`}>
          {effectiveLive ? "LIVE EXECUTION ACTIVE" : "SHADOW MODE"}
        </div>
      </div>

      {isLoading ? (
        <div className="glass-panel h-48 animate-pulse" />
      ) : (
        <>
          {/* Global gate + toggle */}
          <section className="glass-panel p-6 space-y-6">
            <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-6">
              <div className="flex items-start gap-4">
                {global?.live_ready && !guardianHalted ? (
                  <ShieldCheck className="w-10 h-10 text-emerald-400 shrink-0" />
                ) : (
                  <ShieldAlert className="w-10 h-10 text-amber-400 shrink-0" />
                )}
                <div>
                  <h2 className="text-xl font-bold text-white">
                    {global?.live_ready && !guardianHalted
                      ? "Paper track record passes promotion gate"
                      : "Not ready for live betting yet"}
                  </h2>
                  <p className="text-sm text-gray-400 mt-1">
                    {global?.message ?? "Collecting shadow data…"}
                  </p>
                  <p className="text-xs text-gray-500 mt-2">
                    Requires ≥{readiness?.min_settled_required ?? 50} settled bets and{" "}
                    {readiness?.min_roi_required_pct ?? 0}%+ paper ROI.
                  </p>
                </div>
              </div>

              <button
                onClick={() => flipToggle(!toggleOn)}
                disabled={toggling || (!toggleOn && !canEnable)}
                className={`flex items-center gap-3 px-6 py-3 rounded-xl font-bold text-sm transition-all ${
                  toggleOn
                    ? "bg-red-600 hover:bg-red-500 text-white shadow-[0_0_20px_rgba(239,68,68,0.3)]"
                    : canEnable
                      ? "bg-emerald-600 hover:bg-emerald-500 text-white shadow-[0_0_20px_rgba(16,185,129,0.3)]"
                      : "bg-gray-800 text-gray-500 cursor-not-allowed"
                }`}
              >
                {toggleOn ? <ToggleRight className="w-5 h-5" /> : <ToggleLeft className="w-5 h-5" />}
                {toggling ? "Updating…" : toggleOn ? "Live Toggle ON — click to disable" : "Enable Live Trading"}
              </button>
            </div>

            {blockers.length > 0 && (
              <div className="bg-amber-950/30 border border-amber-900/50 rounded-xl p-4 space-y-2">
                <h3 className="text-sm font-semibold text-amber-400 flex items-center gap-2">
                  <AlertTriangle className="w-4 h-4" /> Active blockers
                </h3>
                <ul className="text-xs text-amber-300/90 space-y-1 list-disc list-inside">
                  {blockers.map((b: string, i: number) => <li key={i}>{b}</li>)}
                </ul>
              </div>
            )}

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <VibrantStatCard
                label="Settled Paper Bets"
                value={`${global?.settled_bets ?? 0} / ${global?.min_settled_required ?? 50}`}
                sub="Core bets (excl. longshots)"
                icon={Target}
                color="indigo"
              />
              <VibrantStatCard
                label="Paper ROI"
                value={`${(global?.paper_roi_pct ?? 0) >= 0 ? "+" : ""}${(global?.paper_roi_pct ?? 0).toFixed(1)}%`}
                sub={`Min required: ${global?.min_roi_required_pct ?? 0}%`}
                icon={TrendingUp}
                color={(global?.paper_roi_pct ?? 0) > 0 ? "emerald" : "red"}
              />
              <VibrantStatCard
                label="Total P&L"
                value={`${(global?.total_pnl ?? 0) >= 0 ? "+" : ""}$${(global?.total_pnl ?? 0).toFixed(2)}`}
                sub={toggleOn ? "Toggle armed" : "Toggle off"}
                icon={global?.live_ready ? Unlock : Lock}
                color={global?.live_ready ? "emerald" : "pink"}
              />
            </div>
          </section>

          {/* Guardian */}
          <section className="glass-panel p-6">
            <h2 className="font-bold text-lg mb-4 flex items-center gap-2">
              <Shield className="w-5 h-5 text-violet-400" />
              Guardian Circuit Breaker
            </h2>
            <div className={`p-4 rounded-xl border ${
              guardianHalted ? "bg-red-950/30 border-red-500/30" : "bg-emerald-950/20 border-emerald-500/30"
            }`}>
              <span className={`text-sm font-bold ${guardianHalted ? "text-red-400" : "text-emerald-400"}`}>
                {guardianHalted ? "HALTED — live orders blocked" : "CLEAR — no active halts"}
              </span>
              {guardianHalted && guardian?.reasons?.map((r: string, i: number) => (
                <p key={i} className="text-xs text-red-300 mt-2">{r}</p>
              ))}
            </div>
          </section>

          {/* Per-domain */}
          <section className="space-y-4">
            <h2 className="font-bold text-lg">Domain Readiness (MLB · Weather · Football)</h2>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              {(readiness?.domains ?? []).map((d: Parameters<typeof ReadinessWidget>[0]["domain"]) => (
                <ReadinessWidget key={d.domain} domain={d} />
              ))}
            </div>
          </section>

          {/* How it works */}
          <section className="glass-panel p-6 text-sm text-gray-400 space-y-3">
            <h2 className="font-bold text-white text-base">How shadow → live works</h2>
            <ol className="list-decimal list-inside space-y-2">
              <li>GitHub Actions runs <code className="text-indigo-300">sync_ml.yml</code> 3×/day in shadow mode — paper fills only.</li>
              <li>Each bet is logged with model prob, market price, edge, and CLV at close.</li>
              <li>When settled bets ≥ {readiness?.min_settled_required ?? 50} and ROI is positive, the global gate opens.</li>
              <li>Flip the toggle above to arm live execution. Guardian and per-domain gates still apply.</li>
              <li>On CI, also set <code className="text-indigo-300">ALLOW_LIVE_ON_GITHUB_ACTIONS=true</code> as a second opt-in.</li>
            </ol>
            <p className="pt-2 border-t border-white/5">
              Paper positions are not listed here — open{" "}
              <Link href="/trading" className="text-emerald-400 hover:underline">Trading → Open Positions</Link>
              {" "}(filter Weather) to see shadow weather bets.
            </p>
          </section>
        </>
      )}
    </div>
  );
}
