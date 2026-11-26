import { useEffect, useState, type CSSProperties } from "react";

const INTEL_AVATAR_PALETTES = [
  { bg: "linear-gradient(145deg, #6366f1 0%, #a855f7 100%)", fg: "#fff" },
  { bg: "linear-gradient(145deg, #0ea5e9 0%, #06b6d4 100%)", fg: "#fff" },
  { bg: "linear-gradient(145deg, #f59e0b 0%, #f97316 100%)", fg: "#fff" },
  { bg: "linear-gradient(145deg, #10b981 0%, #14b8a6 100%)", fg: "#fff" },
  { bg: "linear-gradient(145deg, #ec4899 0%, #f43f5e 100%)", fg: "#fff" },
  { bg: "linear-gradient(145deg, #3b82f6 0%, #2563eb 100%)", fg: "#fff" },
  { bg: "linear-gradient(145deg, #8b5cf6 0%, #d946ef 100%)", fg: "#fff" },
  { bg: "linear-gradient(145deg, #22c55e 0%, #84cc16 100%)", fg: "#0f172a" },
] as const;

const AGENT_PALETTES = [
  { bg: "linear-gradient(145deg, #fde68a 0%, #fbbf24 100%)", fg: "#78350f" },
  { bg: "linear-gradient(145deg, #bfdbfe 0%, #60a5fa 100%)", fg: "#1e3a8a" },
  { bg: "linear-gradient(145deg, #fbcfe8 0%, #f472b6 100%)", fg: "#831843" },
  { bg: "linear-gradient(145deg, #bbf7d0 0%, #4ade80 100%)", fg: "#14532d" },
  { bg: "linear-gradient(145deg, #ddd6fe 0%, #a78bfa 100%)", fg: "#4c1d95" },
  { bg: "linear-gradient(145deg, #fed7aa 0%, #fb923c 100%)", fg: "#7c2d12" },
] as const;

function paletteFor(name: string, agent = false) {
  const list = agent ? AGENT_PALETTES : INTEL_AVATAR_PALETTES;
  let h = 0;
  const s = (name || "?").trim().toLowerCase();
  for (let i = 0; i < s.length; i += 1) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return list[h % list.length];
}

function initials(name: string) {
  const parts = (name || "").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

export default function IntelAvatar({
  name,
  agent,
  photo,
  className = "",
}: {
  name: string;
  agent?: boolean;
  photo?: string;
  className?: string;
}) {
  const [broken, setBroken] = useState(false);
  const [photoReady, setPhotoReady] = useState(false);
  const url = (photo || "").trim();
  const pal = paletteFor(name, agent);
  const showPhoto = Boolean(url) && !broken && photoReady;
  const style = {
    background: showPhoto ? "#e2e8f0" : pal.bg,
    color: pal.fg,
  } as CSSProperties;

  useEffect(() => {
    setBroken(false);
    setPhotoReady(false);
    if (!url) return undefined;
    const img = new Image();
    img.onload = () => setPhotoReady(true);
    img.onerror = () => setBroken(true);
    img.src = url;
    return () => {
      img.onload = null;
      img.onerror = null;
    };
  }, [url]);

  return (
    <span
      className={`intel-avatar ${agent ? "intel-avatar-agent" : "intel-avatar-dealer"} ${className}`.trim()}
      style={style}
      aria-hidden
    >
      {showPhoto ? (
        <img src={url} alt="" />
      ) : (
        <span className="intel-avatar-initials">{initials(name)}</span>
      )}
    </span>
  );
}