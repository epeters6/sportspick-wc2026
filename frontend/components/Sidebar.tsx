"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { CloudRain, TrendingUp, Shield, ClipboardCheck, Archive } from "lucide-react";
import SyncCountdown from "./SyncCountdown";

const nav = [
  { href: "/weather", label: "Weather research", icon: CloudRain },
  { href: "/trading", label: "Positions & history", icon: TrendingUp },
  { href: "/live", label: "Readiness gates", icon: Shield },
  { href: "/quant-validation", label: "Validation records", icon: ClipboardCheck },
];
const archive = [
  { href: "/mlb", label: "MLB history" },
  { href: "/models", label: "Legacy model analysis" },
  { href: "/leaderboard", label: "Influencer history" },
  { href: "/matches", label: "Sports matches" },
  { href: "/sources", label: "Scraper sources" },
];

export default function Sidebar() {
  const path = usePathname();
  return (
    <aside className="fixed left-0 top-0 h-full w-64 glass-panel border-r-white/5 flex flex-col rounded-none rounded-r-2xl shadow-2xl z-50">
      <div className="p-5 border-b border-gray-800"><Link href="/weather" className="flex items-center gap-2"><CloudRain className="w-6 h-6 text-sky-400" /><span className="font-bold text-lg tracking-tight">QuantBet Weather</span></Link><p className="text-xs text-gray-400 mt-2">Kalshi + Polymarket US</p></div>
      <nav aria-label="Main navigation" className="flex-1 p-4 space-y-1 overflow-y-auto">
        {nav.map(({ href, label, icon: Icon }) => {
          const active = path === href || path.startsWith(href + "/");
          return <Link key={href} href={href} aria-current={active ? "page" : undefined} className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${active ? "bg-sky-700 text-white" : "text-gray-400 hover:text-white hover:bg-gray-800"}`}><Icon className="w-4 h-4 shrink-0" />{label}</Link>;
        })}
        <details className="pt-6" open={archive.some(({ href }) => path === href || path.startsWith(href + "/"))}>
          <summary className="text-xs text-gray-500 cursor-pointer px-3 py-2"><Archive className="inline w-3 h-3 mr-2" />Historical sports research</summary>
          {archive.map(({ href, label }) => <Link key={href} href={href} aria-current={path === href ? "page" : undefined} className={`block rounded-lg px-3 py-2 text-xs ${path === href ? "bg-gray-800 text-white" : "text-gray-500 hover:text-gray-200"}`}>{label}</Link>)}
        </details>
      </nav>
      <div className="p-4 border-t border-gray-800"><SyncCountdown /></div>
    </aside>
  );
}
