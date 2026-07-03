import { LucideIcon } from "lucide-react";

interface VibrantStatCardProps {
  label: string;
  value: string;
  sub?: string;
  icon: LucideIcon;
  color: string;
}

export default function VibrantStatCard({ label, value, sub, icon: Icon, color }: VibrantStatCardProps) {
  // We use inline styles for dynamic tailwind colors that might get purged if constructed dynamically
  const colorMap: Record<string, { border: string, text: string, iconText: string }> = {
    indigo: { border: "border-t-indigo-500", text: "text-indigo-400", iconText: "text-indigo-500" },
    emerald: { border: "border-t-emerald-500", text: "text-emerald-400", iconText: "text-emerald-500" },
    cyan: { border: "border-t-cyan-500", text: "text-cyan-400", iconText: "text-cyan-500" },
    pink: { border: "border-t-pink-500", text: "text-pink-400", iconText: "text-pink-500" },
    red: { border: "border-t-red-500", text: "text-red-400", iconText: "text-red-500" },
    purple: { border: "border-t-purple-500", text: "text-purple-400", iconText: "text-purple-500" },
    sky: { border: "border-t-sky-500", text: "text-sky-400", iconText: "text-sky-500" },
    amber: { border: "border-t-amber-500", text: "text-amber-400", iconText: "text-amber-500" },
    yellow: { border: "border-t-yellow-500", text: "text-yellow-400", iconText: "text-yellow-500" },
  };

  const theme = colorMap[color] || colorMap.indigo;

  return (
    <div className={`glass-card p-5 border-t-4 ${theme.border} relative overflow-hidden group hover:shadow-[0_0_20px_rgba(255,255,255,0.05)] transition-all`}>
      <div className={`absolute -right-4 -top-4 opacity-10 group-hover:opacity-20 transition-opacity ${theme.iconText} group-hover:scale-110 duration-300`}>
        <Icon className="w-24 h-24" />
      </div>
      <p className="text-gray-400 text-sm font-medium tracking-wide uppercase mb-2">{label}</p>
      <p className={`text-3xl font-extrabold text-transparent bg-clip-text bg-gradient-to-br from-white to-gray-400 mb-1`}>{value}</p>
      {sub && <p className={`text-xs ${theme.text} font-medium`}>{sub}</p>}
    </div>
  );
}
