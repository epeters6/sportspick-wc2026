interface StatCardProps {
  label: string;
  value: string | number;
  sub?: string;
  accent?: "green" | "blue" | "yellow" | "purple" | "red";
}

const accent = {
  green: "text-emerald-400",
  blue: "text-blue-400",
  yellow: "text-yellow-400",
  purple: "text-purple-400",
  red: "text-red-400",
};

export default function StatCard({ label, value, sub, accent: a = "blue" }: StatCardProps) {
  return (
    <div className="glass-card hover-glow p-5 flex flex-col justify-between h-full">
      <p className="text-xs font-medium text-gray-400 uppercase tracking-wider">{label}</p>
      <p className={`text-3xl font-bold mt-1 ${accent[a]}`}>{value}</p>
      {sub && <p className="text-xs text-gray-500 mt-1">{sub}</p>}
    </div>
  );
}
