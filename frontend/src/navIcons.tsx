import type { ReactNode } from "react";

const stroke = {
  width: 18,
  height: 18,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true as const,
};

export type NavIconId =
  | "home"
  | "whatsapp"
  | "phone"
  | "users"
  | "store"
  | "user"
  | "file"
  | "megaphone"
  | "target"
  | "image"
  | "checklist"
  | "package"
  | "calendar"
  | "trending"
  | "bars"
  | "chart"
  | "intel"
  | "gear";

export const NAV_ICONS: Record<NavIconId, ReactNode> = {
  home: (
    <svg {...stroke}>
      <path d="M3 10.5 12 3l9 7.5" />
      <path d="M5 9.5V21h14V9.5" />
      <path d="M10 21v-7h4v7" />
    </svg>
  ),
  whatsapp: (
    <svg {...stroke}>
      <path d="M21 11.5a8.5 8.5 0 0 1-12.4 7.6L3 21l1.9-5.6A8.5 8.5 0 1 1 21 11.5Z" />
      <path d="M9.8 10.4c.3-.6.7-.9 1.2-.9.2 0 .4 0 .5.3l.5 1.2c.1.2 0 .4-.1.5l-.3.3c-.1.1-.1.3 0 .5.6 1 1.5 1.8 2.4 2.2.2.1.4 0 .5-.1l.3-.4c.1-.1.3-.2.5-.1l1.2.4c.2.1.3.3.3.5 0 .5-.3.9-.9 1.2-.5.2-1.2.2-1.9 0a6.2 6.2 0 0 1-3.4-2.8 5.9 5.9 0 0 1-1.2-2.9c-.1-.6.2-1.1.7-1.3Z" />
    </svg>
  ),
  phone: (
    <svg {...stroke}>
      <path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6 19.8 19.8 0 0 1-3.1-8.7A2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.3 1.8.6 2.6a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.5-1.1a2 2 0 0 1 2.1-.4c.9.3 1.7.5 2.6.6a2 2 0 0 1 1.7 1.9Z" />
    </svg>
  ),
  users: (
    <svg {...stroke}>
      <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M22 21v-2a4 4 0 0 0-3-3.9" />
      <path d="M16 3.1a4 4 0 0 1 0 7.8" />
    </svg>
  ),
  store: (
    <svg {...stroke}>
      <path d="M3 9 5.2 4.2A2 2 0 0 1 7 3h10a2 2 0 0 1 1.8 1.2L21 9" />
      <path d="M3 9h18v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9Z" />
      <path d="M9 22V12h6v10" />
    </svg>
  ),
  user: (
    <svg {...stroke}>
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </svg>
  ),
  file: (
    <svg {...stroke}>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" />
      <path d="M14 2v6h6" />
      <path d="M8 13h8" />
      <path d="M8 17h6" />
    </svg>
  ),
  megaphone: (
    <svg {...stroke}>
      <path d="M3 11v2a1 1 0 0 0 1 1h2l5 4V6L6 10H4a1 1 0 0 0-1 1Z" />
      <path d="M15.5 8.5a5 5 0 0 1 0 7" />
      <path d="M18 6a9 9 0 0 1 0 12" />
      <path d="M6 15v3a2 2 0 0 0 2 2h1" />
    </svg>
  ),
  target: (
    <svg {...stroke}>
      <circle cx="12" cy="12" r="8" />
      <circle cx="12" cy="12" r="4" />
      <circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none" />
    </svg>
  ),
  image: (
    <svg {...stroke}>
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <circle cx="9" cy="9" r="2" />
      <path d="m21 15-5-5L5 21" />
    </svg>
  ),
  checklist: (
    <svg {...stroke}>
      <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2" />
      <rect x="9" y="3" width="6" height="4" rx="1" />
      <path d="m9 12 1.5 1.5L14 10" />
      <path d="m9 17 1.5 1.5L14 15" />
    </svg>
  ),
  package: (
    <svg {...stroke}>
      <path d="M21 8 12 3 3 8l9 5 9-5Z" />
      <path d="M3 8v8l9 5" />
      <path d="M21 8v8l-9 5" />
      <path d="M12 13v10" />
    </svg>
  ),
  calendar: (
    <svg {...stroke}>
      <rect x="3" y="5" width="18" height="16" rx="2" />
      <path d="M16 3v4" />
      <path d="M8 3v4" />
      <path d="M3 11h18" />
    </svg>
  ),
  trending: (
    <svg {...stroke}>
      <path d="M3 17 10 10l4 4 7-7" />
      <path d="M14 7h7v7" />
    </svg>
  ),
  bars: (
    <svg {...stroke}>
      <path d="M4 20V10" />
      <path d="M12 20V4" />
      <path d="M20 20v-7" />
    </svg>
  ),
  chart: (
    <svg {...stroke}>
      <path d="M3 3v18h18" />
      <path d="M7 14v4" />
      <path d="M12 10v8" />
      <path d="M17 6v12" />
    </svg>
  ),
  intel: (
    <svg {...stroke}>
      <circle cx="12" cy="12" r="2" />
      <path d="M12 3v3" />
      <path d="M12 18v3" />
      <path d="M3 12h3" />
      <path d="M18 12h3" />
      <path d="m5.6 5.6 2.1 2.1" />
      <path d="m16.3 16.3 2.1 2.1" />
      <path d="m5.6 18.4 2.1-2.1" />
      <path d="m16.3 7.7 2.1-2.1" />
    </svg>
  ),
  gear: (
    <svg {...stroke}>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 1v2.5M12 20.5V23M4.2 4.2l1.8 1.8M18 18l1.8 1.8M1 12h2.5M20.5 12H23M4.2 19.8 6 18M18 6l1.8-1.8" />
    </svg>
  ),
};
