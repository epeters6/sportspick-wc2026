"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  BarChart2, Zap, Trophy, Calendar, Star, TrendingUp, Tv2, Layers, Users, CloudRain
} from "lucide-react";
import SyncCountdown from "./SyncCountdown";

const nav = [
  { href: "/",              label: "Dashboard",   icon: BarChart2 },
  { href: "/recommendations", label: "Top Picks", icon: Zap },
  { href: "/props",           label: "Alt Bets",    icon: Layers },
  { href: "/leaderboard",   label: "Leaderboard", icon: Trophy },
  { href: "/sources",       label: "Sources",     icon: Users },
  { href: "/matches",       label: "WC Matches",  icon: Calendar },
  { href: "/mlb",           label: "MLB",         icon: Tv2 },
  { href: "/weather",       label: "Weather",     icon: CloudRain },
  { href: "/trading",       label: "Trading",     icon: TrendingUp },
];

export default function Sidebar() {
  const path = usePathname();
  return (
    <aside className="fixed left-0 top-0 h-full w-64 glass-panel border-r-0 border-r-white/5 flex flex-col rounded-none rounded-r-2xl shadow-2xl z-50">
      {/* Logo */}
      <div className="p-5 border-b border-gray-800">
        <div className="flex items-center gap-2">
          <Star className="w-6 h-6 text-yellow-400" />
          <span className="font-bold text-lg tracking-tight">SportsPick</span>
        </div>
        <p className="text-xs text-gray-400 mt-1">WC 2026 + MLB Edition</p>
      </div>

      {/* Nav */}
      <nav className="flex-1 p-4 space-y-1 overflow-y-auto">
        {nav.map(({ href, label, icon: Icon }) => {
          const active = path === href;
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
              {href === "/trading" && (
                <span className="ml-auto text-[9px] font-bold px-1.5 py-0.5 rounded bg-violet-800 text-violet-200 uppercase tracking-wide">
                  NEW
                </span>
              )}
            </Link>
          );
        })}
      </nav>

      {/* Footer: sync countdown */}
      <div className="p-4 border-t border-gray-800">
        <SyncCountdown />
      </div>
    </aside>
  );
}
