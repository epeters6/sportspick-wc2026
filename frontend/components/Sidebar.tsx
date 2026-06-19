"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Trophy, Users, Calendar, Star, BarChart2, Zap } from "lucide-react";

const nav = [
  { href: "/", label: "Dashboard", icon: BarChart2 },
  { href: "/recommendations", label: "Top Picks", icon: Zap },
  { href: "/leaderboard", label: "Leaderboard", icon: Trophy },
  { href: "/matches", label: "Matches", icon: Calendar },
];

export default function Sidebar() {
  const path = usePathname();
  return (
    <aside className="fixed left-0 top-0 h-full w-64 bg-gray-900 border-r border-gray-800 flex flex-col">
      <div className="p-5 border-b border-gray-800">
        <div className="flex items-center gap-2">
          <Star className="w-6 h-6 text-yellow-400" />
          <span className="font-bold text-lg tracking-tight">SportsPick</span>
        </div>
        <p className="text-xs text-gray-400 mt-1">World Cup 2026 Edition</p>
      </div>
      <nav className="flex-1 p-4 space-y-1">
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
              <Icon className="w-4 h-4" />
              {label}
            </Link>
          );
        })}
      </nav>
      <div className="p-4 border-t border-gray-800">
        <p className="text-xs text-gray-500 leading-relaxed">
          Tracking Covers.com experts & YouTube analysts.
          Updated every 30 min.
        </p>
      </div>
    </aside>
  );
}
