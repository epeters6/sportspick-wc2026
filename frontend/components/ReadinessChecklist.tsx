import { CheckCircle2, Circle, XCircle } from "lucide-react";

export default function ReadinessChecklist({ criteria, ready }: { criteria: any; ready: boolean }) {
  if (!criteria) return null;

  const items = [
    {
      id: "sample_size",
      label: "Significant Sample Size",
      description: `At least ${criteria.sample_size.threshold} paper trades settled`,
      met: criteria.sample_size.met,
      value: `${criteria.sample_size.value} trades`
    },
    {
      id: "roi",
      label: "Profitable Strategy",
      description: `Paper ROI > ${(criteria.roi.threshold * 100).toFixed(0)}%`,
      met: criteria.roi.met,
      value: `${(criteria.roi.value * 100).toFixed(1)}%`
    },
    {
      id: "brier",
      label: "Well-Calibrated Probabilities",
      description: `Brier Score < ${criteria.brier_score.threshold}`,
      met: criteria.brier_score.met,
      value: criteria.brier_score.value.toFixed(4)
    }
  ];

  return (
    <div className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-widest mb-2">Live Transition Checklist</h3>
      <div className="space-y-3">
        {items.map((item) => (
          <div key={item.id} className={`flex items-center justify-between p-3 rounded-xl border ${item.met ? "bg-emerald-500/10 border-emerald-500/20" : "bg-slate-900/50 border-slate-800"}`}>
            <div className="flex items-start gap-3">
              {item.met ? (
                <CheckCircle2 className="w-5 h-5 text-emerald-400 mt-0.5" />
              ) : (
                <Circle className="w-5 h-5 text-slate-600 mt-0.5" />
              )}
              <div>
                <div className={`text-sm font-medium ${item.met ? "text-emerald-100" : "text-slate-300"}`}>{item.label}</div>
                <div className="text-xs text-slate-500 mt-0.5">{item.description}</div>
              </div>
            </div>
            <div className={`text-sm font-mono font-semibold ${item.met ? "text-emerald-400" : "text-slate-400"}`}>
              {item.value}
            </div>
          </div>
        ))}
      </div>
      
      {ready ? (
        <div className="mt-4 p-4 rounded-xl bg-gradient-to-r from-emerald-900/40 to-emerald-800/20 border border-emerald-500/30 text-center">
          <p className="text-emerald-400 font-semibold flex items-center justify-center gap-2">
            <CheckCircle2 className="w-5 h-5" /> Model is Ready for Live Betting
          </p>
        </div>
      ) : (
        <div className="mt-4 p-4 rounded-xl bg-slate-900/50 border border-slate-800 text-center">
          <p className="text-slate-500 text-sm">Meet all criteria to unlock live betting.</p>
        </div>
      )}
    </div>
  );
}
