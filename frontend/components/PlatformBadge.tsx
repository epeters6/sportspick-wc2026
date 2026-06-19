const colours: Record<string, string> = {
  twitter: "bg-sky-900 text-sky-300",
  tiktok: "bg-pink-900 text-pink-300",
  instagram: "bg-orange-900 text-orange-300",
  covers: "bg-green-900 text-green-300",
  youtube: "bg-red-900 text-red-300",
};

const labels: Record<string, string> = {
  twitter: "𝕏 Twitter",
  tiktok: "TikTok",
  instagram: "Instagram",
  covers: "Covers.com",
  youtube: "YouTube",
};

export default function PlatformBadge({ platform }: { platform: string }) {
  return (
    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${colours[platform] ?? "bg-gray-800 text-gray-300"}`}>
      {labels[platform] ?? platform}
    </span>
  );
}
