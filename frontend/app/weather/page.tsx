"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { CloudRain, Thermometer, Shield, RefreshCw, ArrowRight } from "lucide-react";
import { fetchWeatherPredictions, fetchWeatherExperiment, weatherResearchArms, weatherResearchLabel, weatherReportNotice, type WeatherPrediction } from "@/lib/api";

type Venue = "all" | "kalshi" | "polymarket";

function venueOf(row: WeatherPrediction) {
  return (row.metadata?.platform || row.event_key.split(":")[1] || "unknown").toLowerCase();
}

function venueLabel(venue: string) {
  if (venue === "kalshi") return "Kalshi";
  if (venue === "polymarket" || venue === "polymarket_us") return "Polymarket US";
  return "Unverified venue";
}

function percentage(value: number | null | undefined) {
  return value == null || !Number.isFinite(value) ? "—" : `${(value * 100).toFixed(1)}%`;
}

function signedEdge(value: number | null) {
  return value == null || !Number.isFinite(value) ? "—" : `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)} pp`;
}

function timestamp(value?: string) {
  if (!value || Number.isNaN(Date.parse(value))) return "Not available";
  return new Date(value).toLocaleString(undefined, { timeZone: "UTC" }) + " UTC";
}

export default function WeatherPage() {
  const [selectedArm, setSelectedArm] = useState("");
  const [venue, setVenue] = useState<Venue>("all");
  const experimentQuery = useQuery({
    queryKey: ["weather-experiment"], queryFn: fetchWeatherExperiment, refetchInterval: 120_000,
  });
  const report = experimentQuery.data;
  const arms = weatherResearchArms(report);
  const primaryId = report?.experiment?.id || arms[0]?.[0] || "";
  const selectedId = arms.some(([id]) => id === selectedArm) ? selectedArm : primaryId;
  const experiment = arms.length ? arms.find(([id]) => id === selectedId)?.[1] : report;
  const reportNotice = weatherReportNotice(experiment);
  const exploratoryForecast = experiment?.experiment?.config?.execution_calibration_mode === "observe_only";
  const { data, isLoading, isError, isFetching, refetch } = useQuery({
    queryKey: ["weather-predictions", 100],
    queryFn: () => fetchWeatherPredictions(100),
    refetchInterval: 120_000,
  });
  const rows = data?.predictions ?? [];
  const visible = rows.filter((row) => venue === "all" || venueOf(row) === venue || (venue === "polymarket" && venueOf(row) === "polymarket_us"));
  const events = new Set(visible.map((row) => row.event_key)).size;
  const official = visible.filter((row) => row.metadata?.label_source === "venue_official" && typeof row.is_correct === "boolean").length;
  const kalshi = rows.filter((row) => venueOf(row) === "kalshi").length;
  const polymarket = rows.filter((row) => ["polymarket", "polymarket_us"].includes(venueOf(row))).length;

  return (
    <div className="space-y-7 pb-12">
      <div className="flex flex-col xl:flex-row xl:items-end justify-between gap-4 border-b border-white/10 pb-6">
        <div>
          <p className="text-xs uppercase tracking-widest text-sky-400 mb-3">Kalshi + Polymarket US</p>
          <h1 className="text-3xl lg:text-4xl font-bold flex items-center gap-3"><CloudRain className="w-9 h-9 text-sky-400" />Weather research</h1>
          <p className="text-gray-400 text-sm mt-3 max-w-2xl">Daily temperature markets, station forecasts, and official outcomes. Build a repeatable paper record before risking capital.</p>
        </div>
        <div className="flex flex-wrap gap-3">
          <Link href="/trading" className="inline-flex items-center gap-2 rounded-xl bg-sky-600 px-4 py-2.5 text-sm font-medium hover:bg-sky-500">Paper positions <ArrowRight className="w-4 h-4" /></Link>
          <Link href="/live" className="inline-flex items-center gap-2 rounded-xl border border-white/10 px-4 py-2.5 text-sm text-gray-300 hover:bg-white/5"><Shield className="w-4 h-4" />Readiness gates</Link>
        </div>
      </div>

      <div className="rounded-2xl border border-amber-500/20 bg-amber-950/20 p-4 text-sm text-amber-100/80">
        <p className="font-semibold text-amber-300">Research estimates, not demonstrated trading profits</p>
        <p className="mt-1">An estimated edge is not an execution signal. Official settlement, available liquidity, fees, and forward results determine whether a strategy is ready. This screen does not enable trading.</p>
      </div>

      <section className="glass-panel p-5 space-y-4" aria-label="Forward paper experiment">
        <div className="flex flex-wrap justify-between gap-3"><h2 className="font-semibold text-lg">Forward paper experiment</h2><span className="text-xs text-amber-300">Paper only · live trading not authorized</span></div>
        {arms.length > 0 && <div className="space-y-2">
          <label htmlFor="weather-research-arm" className="block text-xs text-gray-400">Research arm</label>
          <select id="weather-research-arm" value={selectedId} onChange={(event) => setSelectedArm(event.target.value)} className="w-full md:max-w-xl rounded-lg border border-white/20 bg-slate-950 px-3 py-2 text-sm">
            {arms.map(([id, arm]) => <option key={id} value={id}>{weatherResearchLabel(id, arm)}{id === primaryId ? " � Primary baseline" : ""}</option>)}
          </select>
          <p className="text-xs text-gray-400">Each arm and venue has its own paper balance and P&amp;L. Their overlapping markets are not independent samples.</p>
        </div>}
        {experimentQuery.isError ? <p role="alert" className="text-sm text-amber-300">Experiment status unavailable. The weather API may need to be deployed or restored; no fresh status is assumed.</p>
          : experimentQuery.isLoading ? <p className="text-sm text-gray-400">Loading the durable experiment report…</p>
          : !experiment || experiment.status === "never_run" ? <p className="text-sm text-gray-400">No completed experiment report yet. The weather cycle will publish its first report after deployment and execution.</p>
          : <>
            {reportNotice && <p role="alert" className="text-sm text-amber-300">{reportNotice}</p>}
            {report?.experiments && report.status !== "healthy" && <p role="alert" className="text-sm text-amber-300">Research suite: {report.status}. Another arm may be affected even if the selected arm is healthy.</p>}
            <p className="text-sm text-sky-300">{exploratoryForecast ? "Exploratory forecast" : "Historical calibration required"}</p>
            <p className="text-xs text-gray-400">{exploratoryForecast ? "The historical calibration filter is recorded for comparison but is not required for entry in this paper arm." : "Entries in this arm must pass the historical calibration filter."}</p>
            <p className="text-sm text-gray-400">Last recorded cycle: <span className={experiment.status === "healthy" ? "text-sky-300" : "text-amber-300"}>{experiment.status}</span> · Recorded {timestamp(experiment.completed_at || experiment.timestamp)}. Cycle health describes operations, not profitability.</p>
            <p className="text-xs text-gray-500 break-all">Experiment {experiment.experiment?.id || "unavailable"} · {experiment.experiment?.config?.stations?.join(", ") || "Stations unavailable"}</p>
            {Object.keys(experiment.venues ?? {}).length === 0 && <p className="text-sm text-amber-300">Venue balances are unavailable for this arm.</p>}
            <div className="grid md:grid-cols-2 gap-4">
              {Object.entries(experiment.venues ?? {}).map(([name, balance]) => <div key={name} className="rounded-xl border border-white/10 p-4"><h3 className="font-semibold mb-3">{venueLabel(name)}</h3><dl className="grid grid-cols-2 gap-3 text-sm">{[["Paper equity", balance.equity], ["Available cash", balance.available_cash], ["Reserved", balance.reserved], ["Realized P&L", balance.realized_pnl]].map(([label, value]) => <div key={label}><dt className="text-xs text-gray-500">{label}</dt><dd className="font-mono mt-1">{typeof value === "number" && Number.isFinite(value) ? `$${value.toFixed(2)}` : "—"}</dd></div>)}</dl><p className="text-xs text-gray-400 mt-3">{balance.settled} settled · {balance.open} open · {balance.quarantined} quarantined</p></div>)}
            </div>
            <p className="text-xs text-gray-500">This arm has a separately declared paper seed. Legacy losses remain in Positions &amp; history and are not included as new profits.</p>
            {(report?.retired_experiments?.length ?? 0) > 0 && <div className="border-t border-white/10 pt-3 space-y-2"><p className="text-xs font-medium text-gray-400">Preserved historical experiments</p>{report?.retired_experiments?.map((retired) => <p key={retired.id} className="text-xs text-gray-500">{retired.id}: {retired.reason || "Retired; evidence preserved separately."}</p>)}</div>}
          </>}
      </section>

      <div className="grid md:grid-cols-2 gap-4">
        {[["Kalshi", kalshi, "Daily high and low temperature contracts"], ["Polymarket US", polymarket, "Weather contracts through the existing US integration"]].map(([name, count, description]) => (
          <div key={name} className="glass-panel p-5">
            <div className="flex items-center justify-between gap-3"><h2 className="font-semibold text-lg">{name}</h2><span className="text-xs text-sky-300">{isLoading ? "Loading…" : isError ? "Data unavailable" : `${count} legacy bucket records`}</span></div>
            <p className="text-sm text-gray-400 mt-2">{description}</p>
          </div>
        ))}
      </div>

      <section className="space-y-4" aria-label="Recent weather predictions">
        <div className="flex flex-wrap justify-between gap-4 items-center">
          <div><h2 className="font-semibold text-lg">Legacy weather model records</h2><p className="text-xs text-gray-400 mt-1">Latest available record: {timestamp(rows[0]?.created_at)}</p></div>
          <button type="button" onClick={() => refetch()} disabled={isFetching} className="flex items-center gap-2 text-sm text-gray-300 disabled:opacity-50"><RefreshCw className={`w-4 h-4 ${isFetching ? "animate-spin" : ""}`} />Refresh data</button>
        </div>
        <div className="flex flex-wrap gap-2" aria-label="Filter by venue">
          {([ ["all", "Both venues"], ["kalshi", "Kalshi"], ["polymarket", "Polymarket US"] ] as const).map(([value, label]) => (
            <button type="button" key={value} onClick={() => setVenue(value)} aria-pressed={venue === value} className={`rounded-lg px-4 py-2 text-sm ${venue === value ? "bg-sky-600 text-white" : "bg-white/5 text-gray-400 hover:bg-white/10"}`}>{label}</button>
          ))}
        </div>
        <p className="text-xs text-gray-400">This legacy model feed is separate from the selected research arm above. Showing up to 100 bucket records, including settled events. {events} event groups and {official} official outcome labels in this view. These are not independent trades or a full performance sample.</p>

        {isError ? (
          <div role="alert" className="glass-panel p-8 text-center text-amber-300">Weather data could not be loaded. Check the API connection and refresh; no current trading status can be inferred.</div>
        ) : isLoading ? (
          <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-4" aria-label="Loading weather records">{Array.from({ length: 6 }, (_, i) => <div key={i} className="glass-card h-56 animate-pulse" />)}</div>
        ) : visible.length === 0 ? (
          <div className="glass-panel p-10 text-center"><CloudRain className="mx-auto w-9 h-9 text-gray-500 mb-3" /><p className="text-gray-300">No records for this venue in the latest sample.</p><p className="text-sm text-gray-500 mt-2">This does not confirm that discovery is running or that the venue has no markets.</p></div>
        ) : (
          <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-4">
            {visible.map((row) => {
              const meta = row.metadata;
              const parts = row.event_key.split(":");
              const officialOutcome = meta?.label_source === "venue_official" && typeof row.is_correct === "boolean";
              return (
                <article key={row.id} className="glass-panel p-5 space-y-4">
                  <div className="flex justify-between gap-2 text-xs"><span className="text-sky-300 font-medium">{venueLabel(venueOf(row))}</span><span className="text-gray-400">{meta?.target_date || parts[3] || "Date unavailable"}</span></div>
                  <div><h3 className="font-semibold flex items-center gap-2"><Thermometer className="w-4 h-4 text-sky-400" />{meta?.station || parts[2] || "Station unavailable"} · {(meta?.metric || parts[4] || "temperature").replaceAll("_", " ")}</h3><p className="text-gray-300 text-sm mt-2 break-words">{meta?.bucket_label || row.outcome}</p></div>
                  <dl className="grid grid-cols-3 gap-2 border-y border-white/5 py-3 text-sm">
                    <div><dt className="text-xs text-gray-500">Model</dt><dd className="font-semibold text-sky-300 mt-1">{percentage(row.prob)}</dd></div>
                    <div><dt className="text-xs text-gray-500">Market</dt><dd className="font-semibold text-gray-300 mt-1">{percentage(row.market_price)}</dd></div>
                    <div><dt className="text-xs text-gray-500">Est. edge</dt><dd className={`font-semibold mt-1 ${(row.edge ?? 0) > 0 ? "text-sky-300" : "text-gray-300"}`}>{signedEdge(row.edge)}</dd></div>
                  </dl>
                  <div className="flex flex-wrap gap-2 text-xs"><span className="rounded bg-white/5 px-2 py-1 text-gray-300">{officialOutcome ? `Official outcome: ${row.is_correct ? "YES" : "NO"}` : "Outcome not officially verified"}</span><span className="rounded bg-white/5 px-2 py-1 text-gray-400">{meta?.selected === true ? "Model selected · execution unconfirmed" : "Research record"}</span></div>
                  <p className="text-xs text-gray-500">Recorded {timestamp(row.created_at)}</p>
                </article>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
