import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";

type CallType = "" | "missed" | "received" | "rejected" | "outbound";
type DurationFilter = "all" | "zero" | "under_1m" | "1_5m" | "over_5m";

type CallLog = {
  id: number;
  agent_user_id: number;
  agent_name: string;
  agent_email: string;
  agent_online?: boolean;
  phone_number: string;
  phone_normalized: string;
  contact_name: string;
  call_type: string;
  direction: string;
  started_at: string;
  duration_seconds: number;
  device_label: string;
  synced_at: string | null;
};

type AgentOpt = {
  id: number;
  name: string;
  email: string;
  call_count: number;
  online?: boolean;
  last_seen_at?: string | null;
};

type Page = {
  items: CallLog[];
  total: number;
  page: number;
  page_size: number;
  counts: Record<string, number>;
};

type Insights = {
  total_calls: number;
  total_talk_seconds: number;
  avg_duration_seconds: number;
  top_agents: AgentOpt[];
  follow_ups: {
    id: number;
    contact_name: string;
    phone_number: string;
    call_type: string;
    started_at: string;
    agent_name: string;
  }[];
  follow_up_total: number;
  devices: string[];
};

const TYPE_LABELS: Record<string, string> = {
  missed: "Missed",
  received: "Received",
  rejected: "Rejected",
  outbound: "Outbound",
};

const PAGE_SIZES = [10, 25, 50] as const;
const IST = "Asia/Kolkata";

type DatePreset = "last24h" | "today" | "week" | "month" | "year" | "lifetime" | "custom";

const DATE_PRESETS: { key: DatePreset; label: string }[] = [
  { key: "last24h", label: "Last 24h" },
  { key: "today", label: "Today" },
  { key: "week", label: "This week" },
  { key: "month", label: "This month" },
  { key: "year", label: "This year" },
  { key: "lifetime", label: "Lifetime" },
  { key: "custom", label: "Custom" },
];

const AVATAR_COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#8b5cf6", "#ef4444", "#06b6d4", "#ec4899", "#6366f1"];

/** Calendar date (YYYY-MM-DD) in Asia/Kolkata. */
function todayISO(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: IST,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

function addDaysISO(iso: string, delta: number): string {
  const d = new Date(`${iso}T12:00:00+05:30`);
  d.setUTCDate(d.getUTCDate() + delta);
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: IST,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(d);
}

/** Monday=0 … Sunday=6 for an IST calendar date. */
function weekdayMon0(iso: string): number {
  const js = new Date(`${iso}T12:00:00+05:30`).getUTCDay();
  return js === 0 ? 6 : js - 1;
}

type ApiBounds = { from: string | null; to: string | null };

function istDayStartUtc(iso: string): string {
  return new Date(`${iso}T00:00:00+05:30`).toISOString();
}
function istDayEndUtc(iso: string): string {
  return new Date(`${iso}T23:59:59.999+05:30`).toISOString();
}

function boundsForPreset(preset: DatePreset, customFrom: string, customTo: string): ApiBounds {
  const today = todayISO();
  switch (preset) {
    case "last24h": {
      const now = new Date();
      return {
        from: new Date(now.getTime() - 24 * 60 * 60 * 1000).toISOString(),
        to: now.toISOString(),
      };
    }
    case "today":
      return { from: istDayStartUtc(today), to: istDayEndUtc(today) };
    case "week": {
      const from = addDaysISO(today, -weekdayMon0(today));
      return { from: istDayStartUtc(from), to: istDayEndUtc(today) };
    }
    case "month": {
      const from = `${today.slice(0, 8)}01`;
      return { from: istDayStartUtc(from), to: istDayEndUtc(today) };
    }
    case "year": {
      const from = `${today.slice(0, 4)}-01-01`;
      return { from: istDayStartUtc(from), to: istDayEndUtc(today) };
    }
    case "lifetime":
      return { from: null, to: null };
    case "custom":
      return {
        from: customFrom ? istDayStartUtc(customFrom) : null,
        to: customTo ? istDayEndUtc(customTo) : null,
      };
  }
}

function fmtDuration(sec: number): string {
  const s = Math.max(0, Math.floor(sec || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return r ? `${m}m ${r}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function fmtTalkTime(sec: number): string {
  const s = Math.max(0, Math.floor(sec || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm ? `${h}h ${rm}m` : `${h}h`;
}

function fmtWhen(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const today = todayISO();
  const yesterday = addDaysISO(today, -1);
  const day = new Intl.DateTimeFormat("en-CA", {
    timeZone: IST,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(d);
  const time = d.toLocaleString("en-IN", {
    timeZone: IST,
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  });
  if (day === today) return `Today, ${time}`;
  if (day === yesterday) return `Yesterday, ${time}`;
  const datePart = d.toLocaleString("en-IN", {
    timeZone: IST,
    day: "2-digit",
    month: "short",
  });
  return `${datePart}, ${time}`;
}

function displayPhone(row: { phone_number?: string; phone_normalized?: string }): string {
  const raw = (row.phone_number || row.phone_normalized || "").trim();
  if (!raw) return "—";
  const digits = raw.replace(/\D+/g, "");
  if (digits.length === 12 && digits.startsWith("91")) {
    return `+91 ${digits.slice(2, 7)} ${digits.slice(7)}`;
  }
  if (digits.length === 10) {
    return `+91 ${digits.slice(0, 5)} ${digits.slice(5)}`;
  }
  return raw;
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function avatarColor(seed: string): string {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

function contactLabel(row: CallLog): { name: string; sub: string } {
  const name = (row.contact_name || "").trim() || "Unknown";
  const agent = (row.agent_name || "").trim();
  const sub = agent ? `Agent · ${agent}` : TYPE_LABELS[row.call_type] || row.call_type || "";
  return { name, sub };
}

function IconSearch({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="2" />
      <path d="M20 20l-3.5-3.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

function IconLayers({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M12 2 2 7l10 5 10-5-10-5Z" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
      <path d="m2 12 10 5 10-5" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
      <path d="m2 17 10 5 10-5" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
    </svg>
  );
}

function IconMissed({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M22 16.92v2a2 2 0 0 1-2.18 2 19.8 19.8 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.12 4.18 2 2 0 0 1 4.11 2h2a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L7.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.91.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92Z"
        stroke="currentColor"
        strokeWidth="1.7"
      />
      <path d="M16 4l4 4M20 4l-4 4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function IconIncoming({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M16 8 8 16" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <path d="M9 8h7v7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      <path
        d="M22 16.92v2a2 2 0 0 1-2.18 2 19.8 19.8 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.12 4.18 2 2 0 0 1 4.11 2h2a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L7.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.91.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92Z"
        stroke="currentColor"
        strokeWidth="1.6"
      />
    </svg>
  );
}

function IconRejected({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.8" />
      <path d="M7.5 7.5 16.5 16.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function IconOutbound({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M8 16 16 8" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <path d="M9 8h7v7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      <path
        d="M22 16.92v2a2 2 0 0 1-2.18 2 19.8 19.8 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.12 4.18 2 2 0 0 1 4.11 2h2a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L7.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.91.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92Z"
        stroke="currentColor"
        strokeWidth="1.6"
      />
    </svg>
  );
}

function IconChart({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M4 19V5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <path d="M4 19h16" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <path d="M8 16v-5M12 16V8M16 16v-8" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function IconFlag({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M5 21V4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <path d="M5 5h11l-2 3.5L16 12H5" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
    </svg>
  );
}

function IconUsers({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="9" cy="8" r="3.2" stroke="currentColor" strokeWidth="1.7" />
      <path d="M3.5 19c.6-3 2.8-4.5 5.5-4.5S14 16 14.5 19" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
      <circle cx="17" cy="9" r="2.4" stroke="currentColor" strokeWidth="1.7" />
      <path d="M15.2 19c.3-1.7 1.4-2.8 3-3.3" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

function TypeIcon({ type, size = 14 }: { type: string; size?: number }) {
  switch (type) {
    case "missed":
      return <IconMissed size={size} />;
    case "received":
      return <IconIncoming size={size} />;
    case "rejected":
      return <IconRejected size={size} />;
    case "outbound":
      return <IconOutbound size={size} />;
    default:
      return <IconLayers size={size} />;
  }
}

/** Green = Android heartbeat recent; red = stale / never. */
function AgentOnlineDot({ online }: { online?: boolean }) {
  const connected = Boolean(online);
  return (
    <span
      className={`cl-online-dot${connected ? " cl-online-dot--on" : " cl-online-dot--off"}`}
      title={connected ? "Connected (YES CRM app)" : "Disconnected"}
      aria-label={connected ? "Connected" : "Disconnected"}
    />
  );
}

function pageWindow(current: number, total: number): (number | "…")[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const pages: (number | "…")[] = [1];
  const start = Math.max(2, current - 1);
  const end = Math.min(total - 1, current + 1);
  if (start > 2) pages.push("…");
  for (let p = start; p <= end; p++) pages.push(p);
  if (end < total - 1) pages.push("…");
  pages.push(total);
  return pages;
}

function buildFilterParams(opts: {
  callType: CallType;
  agentId: string;
  q: string;
  datePreset: DatePreset;
  customFrom: string;
  customTo: string;
  duration: DurationFilter;
}): URLSearchParams {
  const params = new URLSearchParams();
  if (opts.callType) params.set("call_type", opts.callType);
  if (opts.agentId) params.set("agent_user_id", opts.agentId);
  if (opts.q) params.set("q", opts.q);
  if (opts.duration && opts.duration !== "all") params.set("duration", opts.duration);
  const bounds = boundsForPreset(opts.datePreset, opts.customFrom, opts.customTo);
  if (bounds.from) params.set("date_from", bounds.from);
  if (bounds.to) params.set("date_to", bounds.to);
  return params;
}

export default function Calling() {
  const [callType, setCallType] = useState<CallType>("");
  const [agentId, setAgentId] = useState("");
  const [q, setQ] = useState("");
  const [qDebounced, setQDebounced] = useState("");
  const [datePreset, setDatePreset] = useState<DatePreset>("last24h");
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
  const [duration, setDuration] = useState<DurationFilter>("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(10);
  const [data, setData] = useState<Page | null>(null);
  const [insights, setInsights] = useState<Insights | null>(null);
  const [agents, setAgents] = useState<AgentOpt[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [showAllFollowUps, setShowAllFollowUps] = useState(false);

  useEffect(() => {
    const t = window.setTimeout(() => setQDebounced(q.trim()), 280);
    return () => window.clearTimeout(t);
  }, [q]);

  useEffect(() => {
    api<AgentOpt[]>("/api/call-logs/agents")
      .then(setAgents)
      .catch(() => setAgents([]));
  }, []);

  const filterKey = useMemo(
    () => ({ callType, agentId, qDebounced, datePreset, customFrom, customTo, duration }),
    [callType, agentId, qDebounced, datePreset, customFrom, customTo, duration],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const params = buildFilterParams({
        callType: filterKey.callType,
        agentId: filterKey.agentId,
        q: filterKey.qDebounced,
        datePreset: filterKey.datePreset,
        customFrom: filterKey.customFrom,
        customTo: filterKey.customTo,
        duration: filterKey.duration,
      });
      params.set("page", String(page));
      params.set("page_size", String(pageSize));
      const insightParams = buildFilterParams({
        callType: "",
        agentId: filterKey.agentId,
        q: filterKey.qDebounced,
        datePreset: filterKey.datePreset,
        customFrom: filterKey.customFrom,
        customTo: filterKey.customTo,
        duration: filterKey.duration,
      });
      const [res, insight] = await Promise.all([
        api<Page>(`/api/call-logs?${params}`),
        api<Insights>(`/api/call-logs/insights?${insightParams}`).catch(() => null),
      ]);
      setData(res);
      setInsights(insight);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load call list");
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, filterKey]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    setPage(1);
  }, [filterKey, pageSize]);

  function selectPreset(next: DatePreset) {
    if (next === "custom") {
      const today = todayISO();
      setCustomFrom((prev) => prev || today);
      setCustomTo((prev) => prev || today);
    }
    setDatePreset(next);
  }

  const counts = data?.counts || {};
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const fromRow = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const toRow = Math.min(page * pageSize, total);
  const pages = pageWindow(page, totalPages);

  const tiles = useMemo(
    () =>
      [
        { key: "" as CallType, label: "All", count: counts.all ?? 0, tone: "blue", icon: <IconLayers /> },
        { key: "missed" as CallType, label: "Missed", count: counts.missed ?? 0, tone: "red", icon: <IconMissed /> },
        {
          key: "received" as CallType,
          label: "Received",
          count: counts.received ?? 0,
          tone: "green",
          icon: <IconIncoming />,
        },
        {
          key: "rejected" as CallType,
          label: "Rejected",
          count: counts.rejected ?? 0,
          tone: "orange",
          icon: <IconRejected />,
        },
        {
          key: "outbound" as CallType,
          label: "Outbound",
          count: counts.outbound ?? 0,
          tone: "purple",
          icon: <IconOutbound />,
        },
      ] as const,
    [counts],
  );

  const topAgents = insights?.top_agents || [];
  const maxAgentCalls = Math.max(1, ...topAgents.map((a) => a.call_count));
  const followUps = insights?.follow_ups || [];
  const followTotal = insights?.follow_up_total ?? 0;

  const visibleFollowUps = showAllFollowUps ? followUps : followUps.slice(0, 3);

  return (
    <div className="cl-page">
      <div className="heading cl-heading">
        <div>
          <h1>Call List</h1>
        </div>
      </div>

      <div className="cl-tiles">
        {tiles.map((t) => (
          <button
            key={t.label}
            type="button"
            className={`cl-tile cl-tile--${t.tone}${callType === t.key ? " active" : ""}`}
            onClick={() => setCallType(t.key)}
          >
            <span className={`cl-tile-icon cl-tile-icon--${t.tone}`}>{t.icon}</span>
            <span className="cl-tile-label">{t.label}</span>
            <span className="cl-tile-count">{t.count.toLocaleString("en-IN")}</span>
          </button>
        ))}
      </div>

      <div className="cl-layout">
        <div className="cl-main">
          <div className="panel cl-panel">
            <div className="cl-filters">
              <label className="cl-search">
                <IconSearch />
                <input
                  placeholder="Search number or name…"
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  aria-label="Search number or name"
                />
              </label>
              <select value={agentId} onChange={(e) => setAgentId(e.target.value)} aria-label="Agent">
                <option value="">All agents</option>
                {agents.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.online ? "●" : "○"} {a.name || a.email}
                  </option>
                ))}
              </select>
              <select
                value={duration}
                onChange={(e) => setDuration(e.target.value as DurationFilter)}
                aria-label="Duration"
              >
                <option value="all">Duration: All</option>
                <option value="zero">No talk time</option>
                <option value="under_1m">Under 1 min</option>
                <option value="1_5m">1 – 5 min</option>
                <option value="over_5m">Over 5 min</option>
              </select>
            </div>

            <div className="cl-range-bar" role="group" aria-label="Date range">
              <div className="call-range-pills">
                {DATE_PRESETS.map((p) => (
                  <button
                    key={p.key}
                    type="button"
                    className={datePreset === p.key ? "active" : undefined}
                    aria-pressed={datePreset === p.key}
                    onClick={() => selectPreset(p.key)}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
              {datePreset === "custom" ? (
                <div className="call-range-custom">
                  <label className="field">
                    From
                    <input type="date" value={customFrom} onChange={(e) => setCustomFrom(e.target.value)} />
                  </label>
                  <label className="field">
                    To
                    <input type="date" value={customTo} onChange={(e) => setCustomTo(e.target.value)} />
                  </label>
                </div>
              ) : null}
            </div>

            {err ? <div className="err" style={{ padding: "12px 16px" }}>{err}</div> : null}

            <div className="cl-table-wrap">
              <table className="cl-table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Type</th>
                    <th>Number</th>
                    <th>Contact</th>
                    <th>Duration</th>
                    <th>Agent</th>
                  </tr>
                </thead>
                <tbody>
                  {loading && !data ? (
                    <tr>
                      <td colSpan={6} className="cl-empty">
                        Loading…
                      </td>
                    </tr>
                  ) : null}
                  {!loading && data && data.items.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="cl-empty">
                        No synced calls yet. Agents install YES CRM, sign in with CRM credentials, and grant call-log
                        permission — new calls appear here immediately.
                      </td>
                    </tr>
                  ) : null}
                  {(data?.items || []).map((row) => {
                    const contact = contactLabel(row);
                    const seed = contact.name + displayPhone(row);
                    const agentName = row.agent_name || row.agent_email || "—";
                    const showDur =
                      row.duration_seconds > 0 && row.call_type !== "missed" && row.call_type !== "rejected"
                        ? fmtDuration(row.duration_seconds)
                        : row.duration_seconds > 0
                          ? fmtDuration(row.duration_seconds)
                          : "—";
                    return (
                      <tr key={row.id}>
                        <td className="cl-when">{fmtWhen(row.started_at)}</td>
                        <td>
                          <span
                            className={`cl-type-badge cl-type-badge--${row.call_type || "other"}`}
                            title={TYPE_LABELS[row.call_type] || row.call_type}
                          >
                            <TypeIcon type={row.call_type} />
                          </span>
                        </td>
                        <td className="cl-number">{displayPhone(row)}</td>
                        <td>
                          <div className="cl-contact">
                            <span className="cl-avatar" style={{ background: avatarColor(seed) }}>
                              {initials(contact.name === "Unknown" ? displayPhone(row) : contact.name)}
                            </span>
                            <span className="cl-contact-text">
                              <strong>{contact.name}</strong>
                              {contact.sub ? <small>{contact.sub}</small> : null}
                            </span>
                          </div>
                        </td>
                        <td className="cl-duration">{showDur}</td>
                        <td>
                          <div className="cl-agent">
                            <span className="cl-avatar cl-avatar--sm" style={{ background: avatarColor(agentName) }}>
                              {initials(agentName)}
                            </span>
                            <span className="cl-agent-name">
                              {agentName}
                              <AgentOnlineDot online={row.agent_online} />
                            </span>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <div className="cl-pager">
              <span className="cl-pager-meta">
                {data
                  ? `Showing ${fromRow.toLocaleString("en-IN")} – ${toRow.toLocaleString("en-IN")} of ${total.toLocaleString("en-IN")} calls`
                  : ""}
                {loading ? " · refreshing…" : ""}
              </span>
              <label className="cl-pager-size">
                Rows per page:
                <select
                  value={pageSize}
                  onChange={(e) => setPageSize(Number(e.target.value))}
                  aria-label="Rows per page"
                >
                  {PAGE_SIZES.map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </label>
              <div className="cl-pager-pages" role="navigation" aria-label="Pagination">
                <button
                  type="button"
                  className="cl-page-btn"
                  disabled={page <= 1 || loading}
                  onClick={() => setPage((p) => p - 1)}
                  aria-label="Previous page"
                >
                  ‹
                </button>
                {pages.map((p, i) =>
                  p === "…" ? (
                    <span key={`e${i}`} className="cl-page-ellipsis">
                      …
                    </span>
                  ) : (
                    <button
                      key={p}
                      type="button"
                      className={`cl-page-btn${page === p ? " active" : ""}`}
                      disabled={loading}
                      onClick={() => setPage(p)}
                    >
                      {p}
                    </button>
                  ),
                )}
                <button
                  type="button"
                  className="cl-page-btn"
                  disabled={page >= totalPages || loading}
                  onClick={() => setPage((p) => p + 1)}
                  aria-label="Next page"
                >
                  ›
                </button>
              </div>
            </div>
          </div>
        </div>

        <aside className="cl-side">
          <section className="cl-side-card">
            <header className="cl-side-head">
              <span className="cl-side-icon cl-side-icon--blue">
                <IconChart />
              </span>
              <h2>Call Insights</h2>
            </header>
            <dl className="cl-insights">
              <div>
                <dt>Total calls</dt>
                <dd>{(insights?.total_calls ?? counts.all ?? 0).toLocaleString("en-IN")}</dd>
              </div>
              <div>
                <dt>Total talk time</dt>
                <dd>{fmtTalkTime(insights?.total_talk_seconds ?? 0)}</dd>
              </div>
              <div>
                <dt>Avg. duration</dt>
                <dd>
                  {insights && insights.avg_duration_seconds > 0
                    ? fmtDuration(Math.round(insights.avg_duration_seconds))
                    : "—"}
                </dd>
              </div>
            </dl>
          </section>

          <section className="cl-side-card">
            <header className="cl-side-head">
              <span className="cl-side-icon cl-side-icon--red">
                <IconFlag />
              </span>
              <h2>Follow-up Needed</h2>
              {followTotal > 0 ? <span className="cl-badge">{followTotal}</span> : null}
            </header>
            {followUps.length === 0 ? (
              <p className="cl-side-empty">No missed or rejected calls in this range.</p>
            ) : (
              <ul className="cl-follow-list">
                {visibleFollowUps.map((f) => {
                  const label = (f.contact_name || "").trim() || displayPhone(f) || "Unknown";
                  return (
                    <li key={f.id}>
                      <span className="cl-avatar cl-avatar--sm" style={{ background: avatarColor(label) }}>
                        {initials(label)}
                      </span>
                      <span className="cl-follow-text">
                        <strong>{label}</strong>
                        <small>
                          {TYPE_LABELS[f.call_type] || f.call_type} · {fmtWhen(f.started_at)}
                        </small>
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
            {followUps.length > 3 ? (
              <button
                type="button"
                className="cl-side-link"
                onClick={() => {
                  setShowAllFollowUps((v) => !v);
                  if (!showAllFollowUps) setCallType("missed");
                }}
              >
                {showAllFollowUps ? "Show less" : "View all follow-ups →"}
              </button>
            ) : followTotal > 0 ? (
              <button type="button" className="cl-side-link" onClick={() => setCallType("missed")}>
                View missed calls →
              </button>
            ) : null}
          </section>

          <section className="cl-side-card">
            <header className="cl-side-head">
              <span className="cl-side-icon cl-side-icon--indigo">
                <IconUsers />
              </span>
              <h2>Top Active Agents</h2>
            </header>
            {topAgents.length === 0 ? (
              <p className="cl-side-empty">No agent activity in this range.</p>
            ) : (
              <ul className="cl-agent-bars">
                {topAgents.map((a) => {
                  const name = a.name || a.email || `User ${a.id}`;
                  const pct = Math.round((a.call_count / maxAgentCalls) * 100);
                  return (
                    <li key={a.id}>
                      <div className="cl-agent-bars-row">
                        <span className="cl-avatar cl-avatar--sm" style={{ background: avatarColor(name) }}>
                          {initials(name)}
                        </span>
                        <span className="cl-agent-bars-name">
                          {name}
                          <AgentOnlineDot online={a.online} />
                        </span>
                        <span className="cl-agent-bars-count">{a.call_count}</span>
                      </div>
                      <div className="cl-bar" aria-hidden>
                        <span style={{ width: `${pct}%` }} />
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}
