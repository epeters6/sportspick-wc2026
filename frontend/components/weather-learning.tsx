"use client";

import { useQuery } from "@tanstack/react-query";
import { fetchWeatherLearning } from "@/lib/api";

const labels: Record<string, string> = {
  regularized_logistic: "Simple learning model", boosted_trees: "Boosted trees",
  market_baseline: "Market prices", original_forecast: "Original weather forecast",
  current_probability: "Current blended forecast",
};

function recorded(value?: string) {
  return value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : "Unavailable";
}

export default function WeatherLearning() {
  const query = useQuery({ queryKey: ["weather-learning"], queryFn: fetchWeatherLearning, refetchInterval: 120_000 });
  const report = query.data;
  const training = report?.stages.training;
  const forward = report?.stages.forward_evaluation;
  const stamp = Date.parse(report?.completed_at || "");
  const stale = !Number.isFinite(stamp) || Date.now() - stamp > 3 * 60 * 60_000 || stamp > Date.now() + 60_000;
  return <section className="glass-panel p-5 space-y-4" aria-label="Weather model learning">
    <div className="flex flex-wrap justify-between gap-2"><h2 className="font-semibold text-lg">Weather model learning</h2><span className="text-xs text-sky-300">Forecast research</span></div>
    {query.isError ? <p role="alert" className="text-sm text-amber-300">Learning status is unavailable. Current training and collection health cannot be confirmed.</p>
      : query.isLoading ? <p className="text-sm text-gray-400">Loading model progress…</p>
      : !report || report.status === "never_run" ? <p className="text-sm text-gray-400">Waiting for the first learning report.</p>
      : <>
        {(stale || report.status !== "healthy") && <p role="alert" className="text-sm text-amber-300">{stale ? "This learning report is stale or has an invalid timestamp." : "Some learning or collection work needs attention. Successful stages are shown below."}</p>}
        <p className="text-sm text-gray-400">Last report: {recorded(report.completed_at)}. Models learn from official outcomes and are compared on later dates.</p>
        <dl className="grid sm:grid-cols-3 gap-4">
          <div><dt className="text-xs text-gray-400">Verified history available</dt><dd className="mt-1 font-semibold">{training?.quality.station_days ?? "—"} station-days</dd><p className="text-xs text-gray-500">Across {training?.quality.distinct_dates ?? "—"} dates</p></div>
          <div><dt className="text-xs text-gray-400">Latest collection</dt><dd className="mt-1 font-semibold">{report.stages.collection?.snapshots ?? "—"} predictions</dd><p className="text-xs text-gray-500">{report.stages.collection?.stations?.length ?? "—"} stations in this run</p></div>
          <div><dt className="text-xs text-gray-400">New forward evidence</dt><dd className="mt-1 font-semibold">{forward?.quality.station_days ?? "—"} settled station-days</dd><p className="text-xs text-gray-500">Across {forward?.quality.distinct_dates ?? "—"} dates</p></div>
        </dl>
        <p className="text-xs text-gray-400">Trained {recorded(training?.trained_at)}. Selected on calibration data: {labels[training?.selected_model || ""] || "Still collecting"}.</p>
        {training?.enriched_rows === 0 && <p className="text-sm text-amber-200/80">The first models use existing forecast history. New station observations and additional weather models are being collected; they need official outcomes before joining training.</p>}
        {training?.evaluation && <div className="overflow-x-auto"><table className="w-full text-sm text-left">
          <caption className="text-xs text-gray-400 text-left pb-3">Latest historical test: {training.split?.test?.dates.length ?? "—"} dates. Lower scores indicate better probabilities. These are not trading returns.</caption>
          <thead><tr className="border-b border-white/10 text-gray-400"><th className="py-2">Model</th><th className="py-2 text-right">Brier score</th><th className="py-2 text-right">Log loss</th></tr></thead>
          <tbody>{Object.entries(training.evaluation).map(([name, result]) => <tr key={name} className="border-b border-white/5"><td className="py-2">{labels[name] || name}</td><td className="text-right font-mono">{result.test.brier.toFixed(3)}</td><td className="text-right font-mono">{result.test.log_loss.toFixed(3)}</td></tr>)}</tbody>
        </table></div>}
        <p className="text-xs text-gray-500">Repeated predictions of the same station-day are related evidence. Trading readiness needs sustained forward results and verified costs and fills. Existing paper balances and loss limits remain separate.</p>
      </>}
  </section>;
}
