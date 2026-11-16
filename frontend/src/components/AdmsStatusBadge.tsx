import { useEffect, useState } from "react";
import { api } from "../api/client";

type AdmsHealth = {
  ok: boolean;
  gateway_ok?: boolean;
  device_online?: boolean;
  devices_online?: number;
  devices_total?: number;
  last_online_at?: string;
  last_online_ist?: string;
  primary?: {
    serial?: string;
    name?: string;
    online?: boolean;
    last_heartbeat_at?: string;
    last_online_ist?: string;
  } | null;
};

const POLL_MS = 45_000;
const IST = "Asia/Kolkata";

type Props = {
  /** Compact logo height in px (default 32). */
  size?: number;
  className?: string;
};

/** Format ADMS heartbeat as dd/mm/yyyy HH:mm in Asia/Kolkata. */
function formatLastOnlineIst(iso?: string | null, preformatted?: string | null): string {
  const pref = (preformatted || "").trim();
  if (pref) return pref;
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  try {
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: IST,
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).formatToParts(d);
    const get = (type: Intl.DateTimeFormatPartTypes) =>
      parts.find((p) => p.type === type)?.value || "";
    const day = get("day");
    const month = get("month");
    const year = get("year");
    const hour = get("hour");
    const minute = get("minute");
    if (!day || !month || !year) return "";
    return hour && minute ? `${day}/${month}/${year} ${hour}:${minute}` : `${day}/${month}/${year}`;
  } catch {
    return "";
  }
}

function offlineLabel(when: string): string {
  // Exact format requested: Offline · dd/mm/yyyy HH:mm
  return when ? `Offline · ${when}` : "Offline";
}

export default function AdmsStatusBadge({ size = 32, className = "" }: Props) {
  const [online, setOnline] = useState<boolean | null>(null);
  const [label, setLabel] = useState("Checking…");
  const [hint, setHint] = useState("ADMS");

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async () => {
      try {
        const res = await api<AdmsHealth>("/api/integrations/adms/health");
        if (cancelled) return;

        const when = formatLastOnlineIst(
          res.last_online_at || res.primary?.last_heartbeat_at || null,
          res.last_online_ist || res.primary?.last_online_ist || null,
        );

        // Green only when gateway up AND ≥1 device with recent heartbeat.
        const isOnline = !!res.ok;
        setOnline(isOnline);

        if (isOnline) {
          const n = res.devices_online ?? 0;
          setLabel("Online");
          setHint(n > 1 ? `ADMS Online · ${n} devices` : "ADMS Online · device heartbeat OK");
          return;
        }

        // Offline: always append last heartbeat time when we have it.
        setLabel(offlineLabel(when));
        if (res.gateway_ok === false) {
          setHint(when ? `ADMS gateway unreachable · last online ${when} IST` : "ADMS Offline · gateway unreachable");
        } else {
          setHint(when ? `ADMS Offline · last heartbeat ${when} IST` : "ADMS Offline · no device heartbeat");
        }
      } catch {
        if (!cancelled) {
          setOnline(false);
          // Keep prior label if it already had a timestamp; otherwise plain Offline.
          setLabel((prev) => (prev.startsWith("Offline ·") ? prev : "Offline"));
          setHint("ADMS Offline");
        }
      } finally {
        if (!cancelled) timer = setTimeout(() => void tick(), POLL_MS);
      }
    };

    void tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  const tone = online === null ? "pending" : online ? "online" : "offline";

  return (
    <div className={`adms-status-badge ${tone} ${className}`.trim()} title={hint}>
      <img
        src="/adms-logo.png"
        alt="ADMS"
        width={size}
        height={size}
        className="adms-status-logo"
        draggable={false}
      />
      <span className={`pill ${online ? "green" : online === false ? "red" : ""}`}>
        <span className="adms-status-dot" aria-hidden />
        <span className="adms-status-text">{label}</span>
      </span>
    </div>
  );
}
