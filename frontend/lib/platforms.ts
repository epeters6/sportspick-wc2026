import type { Platform } from "./api";

export const PLATFORM_LABELS: Record<Platform | string, string> = {
  twitter: "𝕏 Twitter",
  tiktok: "TikTok",
  instagram: "Instagram",
  covers: "Covers",
  youtube: "YouTube",
  actionnetwork: "ActionNetwork",
  reddit: "Reddit",
};

export const PLATFORM_FILTER_OPTIONS: { value: string; label: string }[] = [
  { value: "all", label: "All Platforms" },
  { value: "twitter", label: "𝕏 Twitter" },
  { value: "tiktok", label: "TikTok" },
  { value: "covers", label: "Covers" },
  { value: "youtube", label: "YouTube" },
  { value: "actionnetwork", label: "ActionNetwork" },
  { value: "instagram", label: "Instagram" },
];

export const SYNC_SOURCES = [
  { id: "covers", label: "Covers", color: "text-green-400" },
  { id: "youtube", label: "YouTube", color: "text-red-400" },
  { id: "actionnetwork", label: "ActionNetwork", color: "text-blue-400" },
  { id: "twitter", label: "X", color: "text-sky-400" },
  { id: "tiktok", label: "TikTok", color: "text-pink-400" },
] as const;

export function fmtFollowers(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toLocaleString();
}
