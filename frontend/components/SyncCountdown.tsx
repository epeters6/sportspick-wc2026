"use client";
import { useEffect, useState } from "react";
import { Clock } from "lucide-react";

/** Matches weather_cycle.yml. A scheduled time is not evidence that a run started. */
function nextRunMs(): number {
  const now = new Date();
  const next = new Date(now);
  next.setUTCMinutes(17, 0, 0);
  if (next <= now) next.setUTCHours(next.getUTCHours() + 1);
  return next.getTime() - now.getTime();
}

function format(ms: number) {
  const minutes = Math.max(0, Math.ceil(ms / 60_000));
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export default function SyncCountdown() {
  const [remaining, setRemaining] = useState<number | null>(null);
  useEffect(() => {
    const update = () => setRemaining(nextRunMs());
    update();
    const tick = setInterval(update, 30_000);
    return () => clearInterval(tick);
  }, []);
  return <div className="space-y-2"><p className="flex items-center gap-2 text-xs text-gray-400"><Clock className="w-3 h-3" />Weather schedule: {remaining == null ? "hourly at :17" : format(remaining)}</p><p className="text-[10px] text-gray-500 leading-snug">Configured hourly at :17 after deployment, with fixed local forecast windows. GitHub may delay runs; this is not a live status indicator.</p></div>;
}
