/** Human-readable alt-bet label for the UI. */
export function formatPickDisplay(p: {
  predicted_winner?: string | null;
  bet_type?: string | null;
  bet_line?: string | null;
  bet_subject?: string | null;
}): string {
  const pw = p.predicted_winner ?? "";
  const bt = p.bet_type ?? "moneyline";
  const line = p.bet_line;
  const subject = p.bet_subject;

  if (bt === "moneyline") return pw;
  if (bt === "draw") return "Draw";
  if (bt === "player_scorer") return `${subject || pw} to score`;

  if (pw === "over" || pw === "under" || pw === "yes" || pw === "no") {
    const parts: string[] = [];
    if (subject && subject !== "match") parts.push(subject);
    parts.push(pw);
    if (line) parts.push(line);
    const stat = STAT_LABEL[bt];
    if (stat) parts.push(stat);
    return parts.join(" ");
  }

  if (subject && line) return `${subject} — ${pw} ${line}`;
  return pw || "—";
}

const STAT_LABEL: Record<string, string> = {
  total_goals: "goals",
  total_runs: "runs",
  team_total_goals: "team goals",
  team_total_runs: "team runs",
  team_shots: "shots",
  team_tackles: "tackles",
  player_shots: "shots",
  player_strikeouts: "K",
  player_hits: "hits",
  player_rbis: "RBI",
  player_goals: "goals",
  player_tackles: "tackles",
  team_hits: "team hits",
  team_strikeouts: "team K",
  first_half_goals: "1H goals",
  first_five_runs: "F5 runs",
  corners: "corners",
  cards: "cards",
  shots: "shots",
  btts: "",
};
