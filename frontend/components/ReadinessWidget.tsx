import { Lock, Unlock, AlertTriangle } from "lucide-react";

interface DomainReadiness {
  domain: string;
  is_ready: boolean;
  trades_count: number;
  trades_required: number;
  trades_progress_pct: number;
  shrunken_roi: number;
  raw_roi: number;
  status: string;
}

export default function ReadinessWidget({ domain }: { domain: DomainReadiness }) {
  const isCleared = domain.is_ready;
  
  return (
    <div className={`p-4 rounded-xl border ${isCleared ? 'bg-emerald-950/20 border-emerald-500/30' : 'bg-black/40 border-white/5'}`}>
      <div className="flex items-center justify-between mb-3">
        <span className="font-bold text-gray-200 capitalize tracking-wide">{domain.domain}</span>
        <span className={`flex items-center gap-1 text-xs font-bold px-2 py-1 rounded ${isCleared ? 'bg-emerald-500/20 text-emerald-400' : 'bg-gray-800 text-gray-400'}`}>
          {isCleared ? <Unlock className="w-3 h-3" /> : <Lock className="w-3 h-3" />}
          {domain.status}
        </span>
      </div>
      
      <div className="space-y-3">
        {/* Sample Size Progress */}
        <div>
          <div className="flex justify-between text-[10px] uppercase text-gray-500 font-semibold mb-1">
            <span>Sample Size (N≥{domain.trades_required})</span>
            <span>{domain.trades_count} Trades</span>
          </div>
          <div className="h-1.5 bg-gray-900 rounded-full overflow-hidden">
            <div className={`h-full ${domain.trades_count >= domain.trades_required ? 'bg-emerald-500' : 'bg-indigo-500'}`} style={{ width: `${domain.trades_progress_pct}%` }}></div>
          </div>
        </div>
        
        {/* Shrunken Expected ROI */}
        <div>
          <div className="flex justify-between text-[10px] uppercase text-gray-500 font-semibold mb-1">
            <span>Shrunken EV (&gt;0.00%)</span>
            <span className={domain.shrunken_roi > 0 ? "text-emerald-400" : "text-red-400"}>
              {(domain.shrunken_roi * 100).toFixed(2)}%
            </span>
          </div>
          <div className="h-1.5 bg-gray-900 rounded-full overflow-hidden flex">
            {/* Just a visual indicator of positive/negative */}
            <div className={`h-full ${domain.shrunken_roi > 0 ? 'bg-emerald-500 w-full' : 'bg-red-500 w-full'}`}></div>
          </div>
        </div>
        
        {/* Warning if Raw is vastly different from Shrunken */}
        {Math.abs(domain.raw_roi - domain.shrunken_roi) > 0.05 && (
           <div className="text-[10px] text-amber-400/80 flex items-start gap-1 mt-2 bg-amber-950/20 p-2 rounded border border-amber-900/30">
             <AlertTriangle className="w-3 h-3 shrink-0" />
             <span>High variance detected. Raw ROI is {(domain.raw_roi * 100).toFixed(1)}%. Shrinkage applied.</span>
           </div>
        )}
      </div>
    </div>
  );
}
