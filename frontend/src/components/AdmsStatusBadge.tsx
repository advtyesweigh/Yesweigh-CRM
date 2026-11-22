import { useEffect, useState } from "react";
import { apiWithTimeout } from "../api/client";

type AdmsHealth = {
  ok: boolean;
  gateway_ok?: boolean;
  device_online?: boolean;
  device_unstable?: boolean;
  connection_status?: "Online" | "Unstable" | "Offline" | string;
  devices_online?: number;
  devices_unstable?: number;
  devices_total?: number;
  last_online_at?: string;
  last_online_ist?: string;
  offline_for_seconds?: number | null;
  needs_onsite?: boolean;
  recovery_hint?: string;
  primary?: {
    serial?: string;
    name?: string;
    online?: boolean;
    unstable?: boolean;
    connection_status?: string;
    last_heartbeat_at?: string;
    last_online_ist?: string;
  } | null;
};

const POLL_MS = 15_000;
const IST = "Asia/Kolkata";

type Props = {
  /** Compact logo height in px (default 32). */
  size?: number;
  className?: string;
};

type Tone = "online" | "unstable" | "offline" | "pending";

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
  return when ? `Offline · ${when}` : "Offline";
}

function resolveTone(res: AdmsHealth): Tone {
  if (res.ok || res.connection_status === "Online" || res.device_online) return "online";
  if (
    res.connection_status === "Unstable" ||
    res.device_unstable ||
    (res.devices_unstable ?? 0) > 0
  ) {
    return "unstable";
  }
  return "offline";
}

export default function AdmsStatusBadge({ size = 32, className = "" }: Props) {
  const [tone, setTone] = useState<Tone>("pending");
  const [label, setLabel] = useState("Checking…");
  const [hint, setHint] = useState("ADMS");

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async () => {
      try {
        // Hard cap so the pill cannot stick on "Checking…" if ADMS/gateway hangs.
        const res = await apiWithTimeout<AdmsHealth>("/api/integrations/adms/health", 5000);
        if (cancelled) return;

        const when = formatLastOnlineIst(
          res.last_online_at || res.primary?.last_heartbeat_at || null,
          res.last_online_ist || res.primary?.last_online_ist || null,
        );

        const next = resolveTone(res);
        setTone(next);

        if (next === "online") {
          const n = res.devices_online ?? 0;
          setLabel("Online");
          setHint(n > 1 ? `ADMS Online · ${n} devices` : "ADMS Online · device heartbeat OK");
          return;
        }

        if (next === "unstable") {
          setLabel(when ? `Unstable · ${when}` : "Unstable");
          setHint(
            res.recovery_hint ||
              (when
                ? `ADMS Unstable · last heartbeat ${when} IST (45–120s; not Offline yet)`
                : "ADMS Unstable · waiting for next device poll"),
          );
          return;
        }

        setLabel(offlineLabel(when));
        if (res.gateway_ok === false) {
          setHint(when ? `ADMS gateway unreachable · last online ${when} IST` : "ADMS Offline · gateway unreachable");
        } else if (res.needs_onsite) {
          setHint(
            res.recovery_hint ||
              (when
                ? `ADMS Offline · device silent since ${when} IST — toggle Cloud/ADMS or power-cycle`
                : "ADMS Offline · device silent — toggle Cloud/ADMS or power-cycle"),
          );
        } else {
          setHint(
            res.recovery_hint ||
              (when ? `ADMS Offline · last heartbeat ${when} IST` : "ADMS Offline · no device heartbeat"),
          );
        }
      } catch {
        if (!cancelled) {
          setTone("offline");
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

  const pillClass =
    tone === "online" ? "green" : tone === "unstable" ? "amber" : tone === "offline" ? "red" : "";

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
      <span className={`pill ${pillClass}`}>
        <span className="adms-status-dot" aria-hidden />
        <span className="adms-status-text">{label}</span>
      </span>
    </div>
  );
}
