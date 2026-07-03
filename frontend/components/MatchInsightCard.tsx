import { Match } from "@/lib/api";
import { CloudRain, Wind, Thermometer, ShieldAlert, Crosshair, AlertTriangle } from "lucide-react";

export default function MatchInsightCard({ match }: { match: Match }) {
  const cp = match.consensus_picks?.[0];
  const mlPreds = match.model_predictions?.filter((p) => p.source === "sports_ml" && !p.outcome.includes(" ")) || [];
  const ml = mlPreds.length > 0 ? mlPreds[0] : null;
  const meta = ml?.metadata || {};

  // Extract deep dive metrics
  const weatherImpact = meta.weather_impact || "Normal";
  const tempC = meta.temperature_c;
  const windKph = meta.wind_speed_kph;
  const parkFactor = meta.dynamic_park_factor || 1.0;
  const homeFatigue = meta.home_freshness;
  const awayFatigue = meta.away_freshness;
  const umpFactor = meta.ump_k_zone_factor || 1.0;

  const isExtremeWeather = weatherImpact !== "Normal" && weatherImpact !== "Neutral (Dome)";
  const hasConflict = ml && cp && cp.predicted_winner !== ml.outcome;

  return (
    <div className="glass-card flex flex-col group hover:shadow-[0_0_30px_rgba(99,102,241,0.2)] transition-all overflow-hidden border-t-2 border-t-indigo-500/50">
      
      {/* Header */}
      <div className="p-4 border-b border-white/5 bg-white/[0.02]">
        <div className="flex justify-between items-start">
          <div>
            <div className="text-xs text-indigo-400 font-semibold uppercase tracking-wider mb-1">
              {match.sport === "mlb" ? "MLB Quant Slate" : match.tournament}
            </div>
            <div className="font-bold text-lg text-white">
              {match.home_team} <span className="text-gray-500 font-normal mx-1">vs</span> {match.away_team}
            </div>
          </div>
          <div className="text-right">
            <div className="text-[10px] text-gray-500">
              {new Date(match.scheduled_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
            </div>
          </div>
        </div>
      </div>

      {/* Deep Dive Metadata Bar (MLB only typically) */}
      {(tempC != null || homeFatigue != null || umpFactor !== 1.0) && (
        <div className="px-4 py-3 bg-black/40 flex flex-wrap gap-2 border-b border-white/5">
          {isExtremeWeather && (
            <div className="flex items-center gap-1.5 text-[10px] uppercase font-bold text-amber-400 bg-amber-500/10 px-2 py-1 rounded border border-amber-500/20">
              <CloudRain className="w-3 h-3" /> {weatherImpact}
            </div>
          )}
          {tempC != null && (
            <div className="flex items-center gap-1 text-[10px] text-sky-300 bg-sky-500/10 px-2 py-1 rounded border border-sky-500/20">
              <Thermometer className="w-3 h-3" /> {Math.round(tempC)}°C
            </div>
          )}
          {windKph != null && windKph > 10 && (
            <div className="flex items-center gap-1 text-[10px] text-gray-300 bg-gray-500/20 px-2 py-1 rounded border border-gray-500/30">
              <Wind className="w-3 h-3" /> {Math.round(windKph)} kph
            </div>
          )}
          {umpFactor > 1.05 && (
            <div className="flex items-center gap-1 text-[10px] text-emerald-400 bg-emerald-500/10 px-2 py-1 rounded border border-emerald-500/20">
              <Crosshair className="w-3 h-3" /> Pitcher Ump
            </div>
          )}
          {homeFatigue != null && homeFatigue >= 0.95 && (
            <div className="flex items-center gap-1 text-[10px] text-rose-400 bg-rose-500/10 px-2 py-1 rounded border border-rose-500/20">
              <ShieldAlert className="w-3 h-3" /> {match.home_team} Gassed
            </div>
          )}
        </div>
      )}

      {/* Model vs Crowd */}
      <div className="p-4 flex-1 flex flex-col justify-center">
        <div className="grid grid-cols-2 gap-4 items-center">
          
          {/* Crowd Consensus */}
          <div className="space-y-1">
            <div className="text-[10px] text-gray-500 uppercase tracking-wider font-semibold">Crowd</div>
            {cp ? (
              <>
                <div className="font-bold text-gray-200">{cp.predicted_winner}</div>
                <div className="flex items-center gap-2">
                  <div className="w-full bg-gray-800 h-1.5 rounded-full overflow-hidden">
                    <div className="bg-gray-400 h-full" style={{ width: `${Math.round((cp.raw_confidence || cp.confidence) * 100)}%` }} />
                  </div>
                  <span className="text-xs text-gray-400">{Math.round((cp.raw_confidence || cp.confidence) * 100)}%</span>
                </div>
              </>
            ) : <div className="text-sm text-gray-600">—</div>}
          </div>

          {/* ML Model */}
          <div className="space-y-1 border-l border-white/5 pl-4">
            <div className="text-[10px] text-indigo-400 uppercase tracking-wider font-semibold">MLB Quant</div>
            {ml ? (
              <>
                <div className="font-bold text-indigo-300 drop-shadow-[0_0_8px_rgba(99,102,241,0.4)]">{ml.outcome}</div>
                <div className="flex items-center gap-2">
                  <div className="w-full bg-indigo-950 h-1.5 rounded-full overflow-hidden">
                    <div className="bg-gradient-to-r from-indigo-500 to-purple-500 h-full" style={{ width: `${Math.round(ml.prob * 100)}%` }} />
                  </div>
                  <span className="text-xs text-indigo-400">{Math.round(ml.prob * 100)}%</span>
                </div>
              </>
            ) : <div className="text-sm text-gray-600 text-[10px]">Awaiting Sim</div>}
          </div>

        </div>

        {/* Final Blended Pick & Warnings */}
        <div className="mt-4 pt-3 border-t border-white/5 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="text-[10px] text-gray-500 uppercase">Blended Pick:</span>
            {cp ? (
              <span className={`font-extrabold text-sm ${hasConflict ? 'text-gray-200' : 'text-emerald-400'}`}>
                {cp.predicted_winner}
              </span>
            ) : <span className="text-gray-700 text-sm">—</span>}
          </div>
          {hasConflict && (
            <div className="flex items-center gap-1 text-[9px] text-yellow-500 uppercase font-bold tracking-wider animate-pulse">
              <AlertTriangle className="w-3 h-3" /> Conflict
            </div>
          )}
        </div>

      </div>
    </div>
  );
}
