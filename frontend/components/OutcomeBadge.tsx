import { type Pick } from "@/lib/api";

const CONFIG: Record<string, { label: string; cls: string }> = {
  pending:   { label: "Pending",   cls: "bg-blue-950 text-blue-300 border-blue-800" },
  correct:   { label: "Correct",   cls: "bg-emerald-950 text-emerald-300 border-emerald-800" },
  incorrect: { label: "Incorrect", cls: "bg-red-950 text-red-300 border-red-800" },
  void:      { label: "Void",      cls: "bg-gray-800 text-gray-400 border-gray-700" },
};

export default function OutcomeBadge({ outcome }: { outcome?: Pick["outcome"] | string | null }) {
  const key = outcome ?? "pending";
  const cfg = CONFIG[key] ?? CONFIG.pending;
  return (
    <span className={`inline-flex items-center rounded border font-medium px-1.5 py-0.5 text-[10px] ${cfg.cls}`}>
      {cfg.label}
    </span>
  );
}

export function SportBadge({ sport }: { sport?: string | null }) {
  if (!sport) return null;
  const sportLower = sport.toLowerCase();
  
  if (sportLower === "mlb") {
    return (
      <span className="inline-flex items-center rounded border font-medium px-1.5 py-0.5 text-[10px] bg-blue-950 text-blue-300 border-blue-800">
        ⚾ MLB
      </span>
    );
  }
  if (sportLower === "weather" || sportLower.startsWith("weather_")) {
    return (
      <span className="inline-flex items-center rounded border font-medium px-1.5 py-0.5 text-[10px] bg-sky-950 text-sky-300 border-sky-800">
        🌤️ Weather
      </span>
    );
  }
  if (sportLower === "politics") {
    return (
      <span className="inline-flex items-center rounded border font-medium px-1.5 py-0.5 text-[10px] bg-purple-950 text-purple-300 border-purple-800">
        🏛️ Politics
      </span>
    );
  }

  // Fallback to Football / WC
  return (
    <span className="inline-flex items-center rounded border font-medium px-1.5 py-0.5 text-[10px] bg-green-950 text-green-300 border-green-800">
      ⚽ WC
    </span>
  );
}

export function inferPickSport(p: {
  bet_type?: string | null;
  predicted_winner?: string | null;
  matches?: { sport?: string | null };
}): "football" | "mlb" | null {
  if (p.matches?.sport) return p.matches.sport as "football" | "mlb";
  const mlbTypes = new Set([
    "total_runs", "first_five_runs", "team_total_runs", "team_hits", "team_strikeouts",
    "player_strikeouts", "player_hits", "player_rbis",
  ]);
  if (p.bet_type && mlbTypes.has(p.bet_type)) return "mlb";
  if (p.bet_type && p.bet_type !== "moneyline") return "football";
  return null;
}
