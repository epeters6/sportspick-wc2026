"use client";

import { useQuery } from "@tanstack/react-query";
import { fetchModelsOverview, fetchModelsReadiness, fetchWeatherVerification } from "@/lib/api";
import ModelOverviewCard from "@/components/ModelOverviewCard";
import ReadinessChecklist from "@/components/ReadinessChecklist";
import VibrantStatCard from "@/components/VibrantStatCard";
import { BrainCircuit, Thermometer, Activity } from "lucide-react";

export default function ModelsPage() {
  const { data: overview, isLoading: ovLoading } = useQuery({
    queryKey: ["models-overview"],
    queryFn: fetchModelsOverview,
    refetchInterval: 120_000,
  });
  const { data: readiness, isLoading: rdLoading } = useQuery({
    queryKey: ["models-readiness"],
    queryFn: fetchModelsReadiness,
    refetchInterval: 120_000,
  });
  const { data: wv } = useQuery({
    queryKey: ["weather-verification"],
    queryFn: fetchWeatherVerification,
    refetchInterval: 300_000,
  });

  const loading = ovLoading || rdLoading;

  return (
    <div className="space-y-10 pb-12 text-slate-200">
      <div className="flex flex-col md:flex-row md:items-end justify-between gap-4 border-b border-gray-800/50 pb-6">
        <div>
          <h1 className="text-4xl font-extrabold text-transparent bg-clip-text bg-gradient-to-r from-indigo-400 via-purple-400 to-cyan-400 tracking-tight flex items-center gap-3">
            <BrainCircuit className="w-8 h-8 text-indigo-400" />
            Model Calibrations
          </h1>
          <p className="text-gray-400 text-sm mt-2 max-w-2xl">
            Advanced diagnostics for MLB quant, weather MOS, and crowd consensus. World Cup models excluded.
          </p>
        </div>
      </div>

      {/* MOS verification health */}
      {wv && (
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <VibrantStatCard label="Verification Rows" value={String(wv.total)} sub="Forecast vs actual" icon={Thermometer} color="cyan" />
          <VibrantStatCard label="High Temp MAE" value={wv.high_mae_f != null ? `${wv.high_mae_f}°F` : "—"} sub={`${wv.with_actual_high} graded`} icon={Activity} color="indigo" />
          <VibrantStatCard label="Low Temp MAE" value={wv.low_mae_f != null ? `${wv.low_mae_f}°F` : "—"} sub={`${wv.with_actual_low} graded`} icon={Activity} color="purple" />
          <VibrantStatCard label="MOS Engine" value={wv.mos_ready ? "Training" : "Collecting"} sub={wv.mos_ready ? "Bias correction active" : "Need ≥10 actuals"} icon={BrainCircuit} color={wv.mos_ready ? "emerald" : "pink"} />
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center h-64">
          <div className="w-8 h-8 border-4 border-indigo-500 border-t-transparent rounded-full animate-spin" />
        </div>
      ) : (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-8">
          {(overview ?? []).map((model: any) => (
            <div key={model.id} className="flex flex-col gap-6 bg-[#131B2F] border border-slate-800 rounded-3xl p-6 shadow-2xl relative overflow-hidden">
              <div className={`absolute -top-32 -right-32 w-64 h-64 rounded-full blur-3xl opacity-20 ${readiness?.[model.id]?.ready ? "bg-emerald-500" : "bg-indigo-500"}`} />
              <ModelOverviewCard model={model} score={readiness?.[model.id]?.score || 0} />
              <div className="h-px w-full bg-gradient-to-r from-transparent via-slate-800 to-transparent" />
              <ReadinessChecklist criteria={readiness?.[model.id]?.criteria} ready={readiness?.[model.id]?.ready} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
