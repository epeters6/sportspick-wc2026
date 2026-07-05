import { Activity, Target, TrendingUp } from "lucide-react";

export default function ModelOverviewCard({ model, score }: { model: any; score: number }) {
  const isReady = score === 100;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-between items-start">
        <div>
          <h2 className="text-2xl font-bold text-white tracking-wide">{model.name}</h2>
          <p className="text-sm text-slate-400 mt-1 uppercase tracking-widest">{model.id} ENGINE</p>
        </div>
        <div className="flex flex-col items-center">
          <div className="relative w-16 h-16 flex items-center justify-center rounded-full bg-black/40 border border-slate-700 shadow-inner">
            <svg className="absolute inset-0 w-full h-full transform -rotate-90">
              <circle cx="32" cy="32" r="28" fill="none" stroke="currentColor" strokeWidth="4" className="text-slate-800" />
              <circle
                cx="32" cy="32" r="28" fill="none" stroke="currentColor" strokeWidth="4"
                strokeDasharray="175" strokeDashoffset={175 - (175 * score) / 100}
                className={`${isReady ? "text-emerald-400" : "text-indigo-500"} transition-all duration-1000 ease-out`}
              />
            </svg>
            <span className={`text-lg font-bold ${isReady ? "text-emerald-400" : "text-white"}`}>{score}</span>
          </div>
          <span className="text-[10px] text-slate-500 uppercase mt-2 tracking-wider">Readiness</span>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-4 mt-2">
        <div className="bg-black/20 p-4 rounded-2xl border border-white/5 backdrop-blur-sm">
          <div className="flex items-center gap-2 text-slate-400 mb-2">
            <Activity className="w-4 h-4 text-pink-400" />
            <span className="text-xs uppercase font-semibold">Win Rate</span>
          </div>
          <div className="text-2xl font-mono text-white">{(model.win_rate * 100).toFixed(1)}%</div>
          <div className="text-[10px] text-slate-500 mt-1">Based on {model.total_trades} trades</div>
        </div>

        <div className="bg-black/20 p-4 rounded-2xl border border-white/5 backdrop-blur-sm">
          <div className="flex items-center gap-2 text-slate-400 mb-2">
            <TrendingUp className="w-4 h-4 text-emerald-400" />
            <span className="text-xs uppercase font-semibold">Paper ROI</span>
          </div>
          <div className={`text-2xl font-mono ${model.roi > 0 ? "text-emerald-400" : "text-red-400"}`}>
            {model.roi > 0 ? "+" : ""}{(model.roi * 100).toFixed(1)}%
          </div>
          <div className="text-[10px] text-slate-500 mt-1">Simulated returns</div>
        </div>

        <div className="bg-black/20 p-4 rounded-2xl border border-white/5 backdrop-blur-sm">
          <div className="flex items-center gap-2 text-slate-400 mb-2">
            <Target className="w-4 h-4 text-indigo-400" />
            <span className="text-xs uppercase font-semibold">Brier Score</span>
          </div>
          <div className="text-2xl font-mono text-white">{model.brier_score.toFixed(4)}</div>
          <div className="text-[10px] text-slate-500 mt-1">Lower is better</div>
        </div>
      </div>
    </div>
  );
}
