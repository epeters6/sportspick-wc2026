import { PLATFORM_LABELS } from "@/lib/platforms";

const colours: Record<string, string> = {
  twitter:       "bg-sky-900 text-sky-300",
  tiktok:        "bg-pink-900 text-pink-300",
  instagram:     "bg-orange-900 text-orange-300",
  covers:        "bg-green-900 text-green-300",
  youtube:       "bg-red-900 text-red-300",
  actionnetwork: "bg-blue-900 text-blue-300",
  reddit:        "bg-orange-950 text-orange-300",
};

export default function PlatformBadge({ platform }: { platform: string }) {
  return (
    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${colours[platform] ?? "bg-gray-800 text-gray-300"}`}>
      {PLATFORM_LABELS[platform] ?? platform}
    </span>
  );
}
