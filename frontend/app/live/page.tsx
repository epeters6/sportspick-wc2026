"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { CheckCircle, Circle, Lock, Shield, RefreshCw } from "lucide-react";
import { fetchWeatherExperiment, weatherResearchArms, weatherResearchLabel, weatherReportNotice } from "@/lib/api";

const criterionLabels: Record<string, string> = {
  reads_complete: "Complete evidence reads",
  no_quarantined_evidence: "No quarantined trade or forecast evidence",
  minimum_independent_traded_events: "At least 250 distinct traded weather events",
  minimum_distinct_traded_dates: "At least 45 distinct traded target dates",
  positive_lower_95pct_day_cluster_net_roi: "Positive lower 95% bound for net ROI, clustered by date",
  positive_net_closing_clv: "Positive net closing-price advantage",
  closing_clv_event_coverage: "Closing prices cover at least 80% of events and 30 events",
  immutable_traded_decisions_verified: "Trades link to verified immutable decision snapshots",
};

function percent(value: number | null | undefined) {
  return value == null || !Number.isFinite(value) ? "â€”" : `${(value * 100).toFixed(2)}%`;
}

export default function WeatherReadinessPage() {
  const [selectedArm, setSelectedArm] = useState("");
  const { data, isLoading, isError, isFetching, refetch } = useQuery({
    queryKey: ["weather-experiment"], queryFn: fetchWeatherExperiment, refetchInterval: 120_000,
  });
  const arms = weatherResearchArms(data);
  const primaryId = data?.experiment?.id || arms[0]?.[0] || "";
  const selectedId = arms.some(([id]) => id === selectedArm) ? selectedArm : primaryId;
  const selected = arms.length ? arms.find(([id]) => id === selectedId)?.[1] : data;
  const forward = selected?.forward_evaluation;
  const reportNotice = weatherReportNotice(selected);
  const exploratoryForecast = selected?.experiment?.config?.execution_calibration_mode === "observe_only";
  return (
    <div className="space-y-7 pb-12">
      <div className="flex flex-wrap items-end justify-between gap-4 border-b border-white/10 pb-6">
        <div><h1 className="text-3xl font-bold flex items-center gap-3"><Shield className="w-8 h-8 text-sky-400" />Weather research gates</h1><p className="text-sm text-gray-400 mt-3 max-w-2xl">Evaluate the frozen paper experiment separately for Kalshi and Polymarket US. Research results do not authorize real orders.</p></div>
        <button type="button" onClick={() => refetch()} disabled={isFetching} className="text-sm text-gray-300 flex items-center gap-2 disabled:opacity-50"><RefreshCw className={`w-4 h-4 ${isFetching ? "animate-spin" : ""}`} />Refresh evidence</button>
      </div>

      <section className="rounded-2xl border border-amber-500/20 bg-amber-950/20 p-5">
        <h2 className="font-semibold text-amber-300 flex items-center gap-2"><Lock className="w-5 h-5" />Paper only â€” no automatic live promotion</h2>
        <p className="text-sm text-amber-100/80 mt-2">The weather cycle does not place exchange orders and keeps live readiness false. Passing every research gate means the evidence is ready for review. Funding and any future live execution require a separate decision and execution validation.</p>
      </section>

      {arms.length > 0 && <section className="glass-panel p-5 space-y-3">
        <label htmlFor="readiness-research-arm" className="block text-sm font-medium">Research arm</label>
        <select id="readiness-research-arm" value={selectedId} onChange={(event) => setSelectedArm(event.target.value)} className="w-full md:max-w-xl rounded-lg border border-white/20 bg-slate-950 px-3 py-2 text-sm">
          {arms.map(([id, arm]) => <option key={id} value={id}>{weatherResearchLabel(id, arm)}{id === primaryId ? " · Primary baseline" : ""}</option>)}
        </select>
        <p className="text-xs text-gray-400">Evidence and balances below belong only to this arm, with each venue evaluated separately.</p>
        {selected && <><p className="text-sm text-sky-300">{exploratoryForecast ? "Exploratory forecast" : "Historical calibration required"}</p><p className="text-xs text-gray-400">{exploratoryForecast ? "The historical calibration filter is recorded for comparison but is not required for entry in this paper arm." : "Entries in this arm must pass the historical calibration filter."}</p></>}
      </section>}

      {isError ? <div role="alert" className="glass-panel p-6 text-amber-300">Experiment evidence is unavailable. No readiness, balance, or risk status is assumed.</div>
        : isLoading ? <div aria-label="Loading evidence" className="glass-panel h-48 animate-pulse" />
        : !forward ? <div className="glass-panel p-6"><h2 className="font-semibold">No forward evaluation available</h2><p className="text-sm text-gray-400 mt-2">The deployed weather cycle must publish a report before these gates can be evaluated. Existing legacy profits or sample counts do not fill this gap.</p></div>
        : <>
          {reportNotice && <p role="alert" className="text-sm text-amber-300">{reportNotice}</p>}
          {selected?.status !== "healthy" && <p role="alert" className="text-sm text-amber-300">Selected arm cycle: {selected?.status || "unavailable"}.</p>}
          {data?.experiments && data.status !== "healthy" && <p role="alert" className="text-sm text-amber-300">Research suite: {data.status}. Check all arms before relying on current evidence.</p>}
          <p className="text-sm text-gray-400">Last evaluation: {new Date(forward.generated_at).toLocaleString(undefined, { timeZone: "UTC" })} UTC. A historical passing report is not current execution authorization.</p>
          {(forward.read_errors?.length ?? 0) > 0 && <div role="alert" className="glass-panel p-4 text-sm text-amber-300">Some evidence could not be read. Review the cycle report; these results cannot establish readiness.</div>}
          <div className="grid xl:grid-cols-2 gap-5">
            {(["kalshi", "polymarket"] as const).map((venue) => {
              const evidence = forward.venues[venue];
              const account = selected?.venues?.[venue];
              return <section key={venue} className="glass-panel p-5 space-y-5">
                <div className="flex flex-wrap justify-between gap-3"><h2 className="font-semibold text-xl">{venue === "kalshi" ? "Kalshi" : "Polymarket US"}</h2><span className="text-xs text-sky-300">{!evidence ? "Evaluation unavailable" : evidence.research_evidence_ready ? "Research evidence ready for review" : "Research requirements pending"}</span></div>
                {!evidence ? <p className="text-sm text-gray-400">Venue evaluation unavailable.</p> : <>
                  <dl className="grid grid-cols-2 gap-4 text-sm">
                    <div><dt className="text-gray-500 text-xs">Paper equity</dt><dd className="font-semibold mt-1">{typeof account?.equity === "number" && Number.isFinite(account.equity) ? `$${account.equity.toFixed(2)}` : "—"}</dd></div>
                    <div><dt className="text-gray-500 text-xs">Verified realized P&amp;L</dt><dd className="font-semibold mt-1">{Number.isFinite(evidence.verified_realized_pnl) ? `$${evidence.verified_realized_pnl.toFixed(2)}` : "—"}</dd></div>
                    <div><dt className="text-gray-500 text-xs">Traded weather events</dt><dd className="font-semibold mt-1">{evidence.independent_traded_events} / 250</dd></div>
                    <div><dt className="text-gray-500 text-xs">Traded target dates</dt><dd className="font-semibold mt-1">{evidence.distinct_traded_dates} / 45</dd></div>
                    <div><dt className="text-gray-500 text-xs">Verified paper ROI</dt><dd className="font-semibold mt-1">{percent(evidence.net_roi)}</dd></div>
                    <div><dt className="text-gray-500 text-xs">Closing-price coverage</dt><dd className="font-semibold mt-1">{percent(evidence.closing_clv_event_coverage)}</dd></div>
                  </dl>
                  <p className="text-xs text-gray-400">95% interval for net ROI: {evidence.net_roi_95pct_day_cluster_interval ? evidence.net_roi_95pct_day_cluster_interval.map(percent).join(" to ") : "Insufficient date clusters"}. Repeated monitoring and strategy selection are not adjusted in this exploratory interval.</p>
                  <ul className="space-y-3 border-t border-white/10 pt-4">{Object.entries(evidence.criteria).map(([name, passed]) => <li key={name} className="flex gap-2 text-sm">{passed ? <CheckCircle className="w-4 h-4 text-sky-400 shrink-0 mt-0.5" /> : <Circle className="w-4 h-4 text-gray-500 shrink-0 mt-0.5" />}<span className={passed ? "text-gray-200" : "text-gray-400"}>{criterionLabels[name] || name.replaceAll("_", " ")}</span></li>)}</ul>
                </>}
                {(account?.blocked_reasons?.length ?? 0) > 0 && <div className="rounded-lg bg-amber-950/30 border border-amber-500/20 p-3"><p className="font-medium text-sm text-amber-300">Paper account entry blocked</p><ul className="mt-2 text-xs text-amber-100/80 space-y-1">{account?.blocked_reasons?.map((reason) => <li key={reason}>{reason.replaceAll("_", " ")}</li>)}</ul></div>}
              </section>;
            })}
          </div>
        </>}

      <section className="glass-panel p-5 text-sm text-gray-400 space-y-3">
        <h2 className="font-semibold text-white">What the experiment can establish</h2>
        <p>Independent forecast snapshots, officially verified outcomes, realistic paper costs, and closing-price observations can support a research finding. They cannot prove that live fills will match the simulation.</p>
        <p>The 250-event and 45-date requirements are collection floors, not a guarantee of statistical certainty. Keep the configuration fixed and account for weather dependence across stations and dates.</p>
        <p><Link href="/weather" className="text-sky-300 hover:underline">Weather research</Link> shows experiment balances. <Link href="/trading" className="text-sky-300 hover:underline">Positions &amp; history</Link> preserves the legacy ledger and sports results separately.</p>
      </section>
    </div>
  );
}
