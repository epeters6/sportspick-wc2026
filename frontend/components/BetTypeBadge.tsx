import { type BetType } from "@/lib/api";

const CONFIG: Record<string, { label: string; cls: string }> = {
  moneyline:         { label: "ML",      cls: "bg-indigo-950 text-indigo-300 border-indigo-800" },
  draw:              { label: "Draw",    cls: "bg-amber-950 text-amber-300 border-amber-800" },
  total_goals:       { label: "O/U",     cls: "bg-cyan-950 text-cyan-300 border-cyan-800" },
  total_runs:        { label: "O/U",     cls: "bg-cyan-950 text-cyan-300 border-cyan-800" },
  team_total_goals:  { label: "Team",    cls: "bg-sky-950 text-sky-300 border-sky-800" },
  team_total_runs:   { label: "TTO",     cls: "bg-sky-950 text-sky-300 border-sky-800" },
  team_shots:        { label: "Shots",   cls: "bg-emerald-950 text-emerald-300 border-emerald-800" },
  team_tackles:      { label: "Tackles", cls: "bg-lime-950 text-lime-300 border-lime-800" },
  first_half_goals:  { label: "1H",      cls: "bg-yellow-950 text-yellow-300 border-yellow-800" },
  first_five_runs:   { label: "F5",      cls: "bg-yellow-950 text-yellow-300 border-yellow-800" },
  btts:              { label: "BTTS",    cls: "bg-violet-950 text-violet-300 border-violet-800" },
  spread:            { label: "Spread",  cls: "bg-blue-950 text-blue-300 border-blue-800" },
  corners:           { label: "Corners", cls: "bg-orange-950 text-orange-300 border-orange-800" },
  cards:             { label: "Cards",   cls: "bg-red-950 text-red-300 border-red-800" },
  shots:             { label: "Shots",   cls: "bg-emerald-950 text-emerald-300 border-emerald-800" },
  player_scorer:     { label: "Scorer",  cls: "bg-pink-950 text-pink-300 border-pink-800" },
  player_assists:    { label: "Assist",  cls: "bg-rose-950 text-rose-300 border-rose-800" },
  player_shots:      { label: "P.Shots", cls: "bg-teal-950 text-teal-300 border-teal-800" },
  player_strikeouts: { label: "P.K",     cls: "bg-fuchsia-950 text-fuchsia-300 border-fuchsia-800" },
  player_hits:       { label: "P.Hits",  cls: "bg-orange-950 text-orange-300 border-orange-800" },
  player_rbis:       { label: "P.RBI",   cls: "bg-amber-950 text-amber-300 border-amber-800" },
  player_goals:      { label: "P.Goals", cls: "bg-pink-950 text-pink-300 border-pink-800" },
  player_tackles:    { label: "P.Tck",   cls: "bg-lime-950 text-lime-300 border-lime-800" },
  team_hits:         { label: "T.Hits",  cls: "bg-orange-950 text-orange-300 border-orange-800" },
  team_strikeouts:   { label: "T.K",     cls: "bg-fuchsia-950 text-fuchsia-300 border-fuchsia-800" },
};

interface Props {
  betType?: BetType | string | null;
  betLine?: string | null;
  size?: "sm" | "xs";
}

export default function BetTypeBadge({ betType, betLine, size = "xs" }: Props) {
  const key = betType ?? "moneyline";
  const cfg = CONFIG[key] ?? { label: key, cls: "bg-gray-800 text-gray-400 border-gray-700" };
  const px = size === "xs" ? "px-1.5 py-0.5 text-[10px]" : "px-2 py-0.5 text-xs";

  return (
    <span className={`inline-flex items-center gap-0.5 rounded border font-medium ${px} ${cfg.cls}`}>
      {cfg.label}
      {betLine && <span className="opacity-70">&nbsp;{betLine}</span>}
    </span>
  );
}
