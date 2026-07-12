"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  BarChart2, Zap, TrendingUp, Tv2, CloudRain, BrainCircuit, Shield,
} from "lucide-react";
import SyncCountdown from "./SyncCountdown";

const nav = [
  { href: "/",           label: "Dashboard",      icon: BarChart2 },
  { href: "/trading",    label: "Trading",        icon: TrendingUp },
  { href: "/live",       label: "Live Readiness", icon: Shield },
  { href: "/models",     label: "Model Calibrations", icon: BrainCircuit },
  { href: "/mlb",        label: "MLB",            icon: Tv2 },
  { href: "/weather",    label: "Weather",        icon: CloudRain },
  { href: "/quant-validation", label: "Shadow Audit", icon: Zap },
];

export default function Sidebar() {
  const path = usePathname();
  return (
    <aside className="fixed left-0 top-0 h-full w-64 glass-panel border-r-0 border-r-white/5 flex flex-col rounded-none rounded-r-2xl shadow-2xl z-50">
      <div className="p-5 border-b border-gray-800">
        <div className="flex items-center gap-2">
          <Zap className="w-6 h-6 text-emerald-400" />
          <span className="font-bold text-lg tracking-tight">QuantBet</span>
        </div>
        <p className="text-xs text-gray-400 mt-1">MLB + Weather · Shadow/Live</p>
      </div>

      <nav className="flex-1 p-4 space-y-1 overflow-y-auto">
        {nav.map(({ href, label, icon: Icon }) => {
          const active = path === href || (href !== "/" && path.startsWith(href));
          return (
            <Link
              key={href}
              href={href}
              className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                active
                  ? "bg-indigo-600 text-white"
                  : "text-gray-400 hover:text-white hover:bg-gray-800"
              }`}
            >
              <Icon className="w-4 h-4 flex-shrink-0" />
              {label}
              {href === "/live" && (
                <span className="ml-auto text-[9px] font-bold px-1.5 py-0.5 rounded bg-emerald-800 text-emerald-200 uppercase tracking-wide">
                  Gate
                </span>
              )}
            </Link>
          );
        })}
      </nav>

      <div className="p-4 border-t border-gray-800">
        <SyncCountdown />
      </div>
    </aside>
  );
}
