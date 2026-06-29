/**
 * Three-way probability bar — home / draw / away.
 * All three props are 0–1 fractions; they're re-normalised internally if they
 * don't sum to exactly 1 (e.g. when only two are present).
 */

interface Props {
  homeProb?: number | null;
  drawProb?: number | null;
  awayProb?: number | null;
  homeLabel?: string;
  awayLabel?: string;
  className?: string;
}

function pct(n?: number | null) {
  if (n == null || isNaN(n)) return "—";
  return `${Math.round(n * 100)}%`;
}

export default function ProbBar({
  homeProb,
  drawProb,
  awayProb,
  homeLabel = "Home",
  awayLabel = "Away",
  className = "",
}: Props) {
  const h = homeProb ?? 0;
  const d = drawProb ?? 0;
  const a = awayProb ?? 0;
  const total = h + d + a || 1;
  const hp = (h / total) * 100;
  const dp = (d / total) * 100;
  const ap = (a / total) * 100;

  if (hp === 0 && dp === 0 && ap === 0) return null;

  return (
    <div className={`space-y-1 ${className}`}>
      {/* Bar */}
      <div className="flex h-2 rounded-full overflow-hidden bg-gray-800">
        {hp > 0 && (
          <div
            className="bg-indigo-500 transition-all"
            style={{ width: `${hp}%` }}
            title={`${homeLabel} ${pct(homeProb)}`}
          />
        )}
        {dp > 0 && (
          <div
            className="bg-amber-500 transition-all"
            style={{ width: `${dp}%` }}
            title={`Draw ${pct(drawProb)}`}
          />
        )}
        {ap > 0 && (
          <div
            className="bg-rose-500 transition-all"
            style={{ width: `${ap}%` }}
            title={`${awayLabel} ${pct(awayProb)}`}
          />
        )}
      </div>
      {/* Labels */}
      <div className="flex justify-between text-[10px] text-gray-400">
        <span>
          <span className="inline-block w-2 h-2 rounded-sm bg-indigo-500 mr-1 align-middle" />
          {homeLabel} {pct(homeProb)}
        </span>
        {dp > 0 && (
          <span>
            <span className="inline-block w-2 h-2 rounded-sm bg-amber-500 mr-1 align-middle" />
            Draw {pct(drawProb)}
          </span>
        )}
        <span>
          <span className="inline-block w-2 h-2 rounded-sm bg-rose-500 mr-1 align-middle" />
          {awayLabel} {pct(awayProb)}
        </span>
      </div>
    </div>
  );
}
