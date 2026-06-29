"use client";
import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { SYNC_SOURCES } from "@/lib/platforms";

/** Live countdown to the next GitHub Actions sync (scheduled every :00 and :30). */
function nextRunMs(): number {
  const now = new Date();
  const next = new Date(now);
  const m = now.getMinutes();
  const nextMin = m < 30 ? 30 : 60;
  next.setMinutes(nextMin, 0, 0);
  if (nextMin === 60) next.setHours(next.getHours() + 1);
  return next.getTime() - now.getTime();
}

function fmt(ms: number) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

export default function SyncCountdown() {
  const [remaining, setRemaining] = useState<number | null>(null);

  useEffect(() => {
    const update = () => setRemaining(nextRunMs());
    update();
    const tick = setInterval(update, 1000);
    return () => clearInterval(tick);
  }, []);

  const isRunning = remaining != null && remaining > 29 * 60 * 1000;

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-xs">
        <RefreshCw
          className={`w-3 h-3 ${isRunning ? "animate-spin text-indigo-400" : "text-gray-500"}`}
        />
        {remaining == null ? (
          <span className="text-gray-500">Next sync every 30 min</span>
        ) : isRunning ? (
          <span className="text-indigo-400 font-medium">Sync running…</span>
        ) : (
          <span className="text-gray-500">
            Next sync in{" "}
            <span className="font-mono text-gray-300">{fmt(remaining)}</span>
          </span>
        )}
      </div>
      <div className="flex flex-wrap gap-1">
        {SYNC_SOURCES.map((s) => (
          <span
            key={s.id}
            className={`text-[10px] px-1.5 py-0.5 rounded bg-gray-800 ${s.color}`}
          >
            {s.label}
          </span>
        ))}
      </div>
      <p className="text-[10px] text-gray-600 leading-snug">
        WC + MLB · X &amp; TikTok when cookies set
      </p>
    </div>
  );
}
