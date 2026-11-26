import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api/client";

type Period = "today" | "7d" | "30d" | "all";

type StaffReplyItem = {
  whatsapp_instance_id: number;
  staff_name: string;
  count: number;
  office_avg_seconds?: number;
  off_avg_seconds?: number;
  office_avg_label?: string | null;
  off_avg_label?: string | null;
};

type PendingStaff = {
  staff_name: string;
  sales: number;
  service: number;
  cloud: number;
  other: number;
  total: number;
};

type ReplyAnalytics = {
  period: Period;
  timezone: string;
  office_rules: string;
  staff_reply: { items: StaffReplyItem[] };
  pending: {
    total: Record<string, number>;
    grand_total: number;
    by_staff: PendingStaff[];
  };
  contact_reply: Record<
    string,
    {
      count: number;
      avg_label?: string | null;
      office_avg_label?: string | null;
      off_avg_label?: string | null;
    }
  >;
  categories: {
    active_conversations: Record<string, number>;
    active_total: number;
  };
  kb_leads: {
    detection: string;
    lead_kb_conversations: number;
    rate_of_active_chats_pct: number;
    suggestion_kb_hits: number;
    heuristic_conversations: number;
    by_staff: { staff_name: string; kb_signals: number }[];
    top_intents: { intent: string; count: number }[];
  };
};

const PERIODS: { id: Period; label: string }[] = [
  { id: "today", label: "Today" },
  { id: "7d", label: "7 days" },
  { id: "30d", label: "30 days" },
  { id: "all", label: "All time" },
];

const CAT_COLORS: Record<string, string> = {
  sales: "#2563eb",
  service: "#059669",
  cloud: "#7c3aed",
  other: "#94a3b8",
};

const CAT_LABELS: Record<string, string> = {
  sales: "Sales",
  service: "Service",
  cloud: "Cloud",
  other: "Other",
};

function parsePeriod(raw: string | null): Period {
  if (raw === "today" || raw === "7d" || raw === "30d" || raw === "all") return raw;
  return "7d";
}

function fmtPct(n: number, total: number) {
  if (!total) return "0%";
  return `${Math.round((n / total) * 100)}%`;
}

function Donut({ totals }: { totals: Record<string, number> }) {
  const slices = ["sales", "service", "cloud", "other"].map((key) => ({
    key,
    label: CAT_LABELS[key],
    value: totals[key] || 0,
    color: CAT_COLORS[key],
  }));
  const total = slices.reduce((s, x) => s + x.value, 0);
  const r = 54;
  const c = 2 * Math.PI * r;
  let offset = 0;
  if (total === 0) return <div className="rs-empty">No category data</div>;
  return (
    <div className="rs-donut-wrap">
      <svg className="rs-donut" viewBox="0 0 140 140" role="img" aria-label="Category breakdown">
        <circle cx="70" cy="70" r={r} fill="none" stroke="#e2e8f0" strokeWidth="18" />
        {slices.map((sl) => {
          const frac = sl.value / total;
          const dash = frac * c;
          const el = (
            <circle
              key={sl.key}
              cx="70"
              cy="70"
              r={r}
              fill="none"
              stroke={sl.color}
              strokeWidth="18"
              strokeDasharray={`${dash} ${c - dash}`}
              strokeDashoffset={-offset}
              transform="rotate(-90 70 70)"
            />
          );
          offset += dash;
          return el;
        })}
        <text x="70" y="66" textAnchor="middle" className="rs-donut-center-n">
          {total}
        </text>
        <text x="70" y="82" textAnchor="middle" className="rs-donut-center-l">
          chats
        </text>
      </svg>
      <ul className="rs-donut-legend">
        {slices.map((sl) => (
          <li key={sl.key}>
            <i style={{ background: sl.color }} />
            <span>{sl.label}</span>
            <strong>
              {sl.value} <span className="rs-muted">({fmtPct(sl.value, total)})</span>
            </strong>
          </li>
        ))}
      </ul>
    </div>
  );
}

function StaffOfficeOffBars({ items }: { items: StaffReplyItem[] }) {
  if (!items.length) return <div className="rs-empty">No completed reply cycles in this period.</div>;
  const maxSec = Math.max(60, ...items.flatMap((s) => [s.office_avg_seconds || 0, s.off_avg_seconds || 0]));
  return (
    <ul className="rs-staff-bars">
      {items.map((s) => {
        const oPct = Math.round(((s.office_avg_seconds || 0) / maxSec) * 100);
        const fPct = Math.round(((s.off_avg_seconds || 0) / maxSec) * 100);
        return (
          <li key={`${s.whatsapp_instance_id}-${s.staff_name}`}>
            <div className="rs-staff-bars-head">
              <span className="rs-staff-name">{s.staff_name}</span>
              <span className="rs-staff-n">{s.count} replies</span>
            </div>
            <div className="rs-dual-bar" title="Office hours avg">
              <span className="rs-dual-label">🏢</span>
              <span className="rs-dual-track">
                <span className="rs-dual-fill office" style={{ width: `${Math.max(oPct, 2)}%` }} />
              </span>
              <span className="rs-dual-val">{s.office_avg_label ?? "—"}</span>
            </div>
            <div className="rs-dual-bar" title="Off-hours avg">
              <span className="rs-dual-label">🌙</span>
              <span className="rs-dual-track">
                <span className="rs-dual-fill off" style={{ width: `${Math.max(fPct, 2)}%` }} />
              </span>
              <span className="rs-dual-val off">{s.off_avg_label ?? "—"}</span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function PendingStack({ s, max }: { s: PendingStaff; max: number }) {
  const w = Math.round((s.total / max) * 100);
  const total = Math.max(1, s.sales + s.service + s.cloud + s.other);
  const segs = [
    { k: "sales", n: s.sales, c: CAT_COLORS.sales },
    { k: "service", n: s.service, c: CAT_COLORS.service },
    { k: "cloud", n: s.cloud, c: CAT_COLORS.cloud },
    { k: "other", n: s.other, c: CAT_COLORS.other },
  ];
  return (
    <div className="rs-stack-track" style={{ width: `${Math.max(w, 4)}%` }}>
      {segs.map((seg) =>
        seg.n > 0 ? (
          <span
            key={seg.k}
            className="rs-stack-seg"
            style={{ flex: seg.n / total, background: seg.c }}
            title={`${seg.k}: ${seg.n}`}
          />
        ) : null
      )}
    </div>
  );
}

function PendingByStaff({ byStaff }: { byStaff: PendingStaff[] }) {
  if (!byStaff.length) return <div className="rs-empty">No pending replies right now.</div>;
  const max = Math.max(1, ...byStaff.map((s) => s.total));
  return (
    <ul className="rs-pending-bars">
      {byStaff.map((s) => {
        const bits = [
          s.sales ? `Sales ${s.sales}` : "",
          s.service ? `Service ${s.service}` : "",
          s.cloud ? `Cloud ${s.cloud}` : "",
          s.other ? `Other ${s.other}` : "",
        ].filter(Boolean);
        return (
          <li key={s.staff_name}>
            <div className="rs-pending-meta">
              <span>{s.staff_name}</span>
              <strong>{s.total}</strong>
            </div>
            <PendingStack s={s} max={max} />
            {bits.length ? <p className="rs-pending-sub">{bits.join(" · ")}</p> : null}
          </li>
        );
      })}
    </ul>
  );
}

function ContactCompare({ block }: { block: ReplyAnalytics["contact_reply"] }) {
  const rows = [
    { key: "dealer", label: "Dealer", emoji: "🏪" },
    { key: "lead", label: "Lead", emoji: "🎯" },
    { key: "other", label: "Other", emoji: "💬" },
  ];
  const maxCount = Math.max(1, ...rows.map((r) => block[r.key]?.count || 0));
  return (
    <ul className="rs-contact-compare">
      {rows.map((r) => {
        const st = block[r.key] || { count: 0, avg_label: "—" };
        const pct = Math.round(((st.count || 0) / maxCount) * 100);
        return (
          <li key={r.key}>
            <div className="rs-contact-head">
              <span>
                {r.emoji} {r.label}
              </span>
              <span className="rs-muted">{st.count} replies</span>
            </div>
            <div className="rs-contact-bar">
              <span style={{ width: `${Math.max(pct, st.count ? 6 : 0)}%` }} />
            </div>
            <div className="rs-contact-times">
              <span title="Overall">{st.avg_label ?? "—"}</span>
              <span className="rs-muted" title="Office">
                🏢 {st.office_avg_label ?? "—"}
              </span>
              <span className="rs-muted off" title="Off-hours">
                🌙 {st.off_avg_label ?? "—"}
              </span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function KbStaffBars({ rows }: { rows: { staff_name: string; kb_signals: number }[] }) {
  if (!rows.length) return <div className="rs-empty">No KB-interest signals from leads in this period.</div>;
  const max = Math.max(1, ...rows.map((r) => r.kb_signals));
  return (
    <ul className="rs-kb-bars">
      {rows.map((r) => (
        <li key={r.staff_name}>
          <span className="rs-staff-name">{r.staff_name}</span>
          <span className="rs-kb-track">
            <span style={{ width: `${Math.round((r.kb_signals / max) * 100)}%` }} />
          </span>
          <strong>{r.kb_signals}</strong>
        </li>
      ))}
    </ul>
  );
}

function IntentBars({ rows }: { rows: { intent: string; count: number }[] }) {
  if (!rows.length) return null;
  const max = Math.max(1, ...rows.map((r) => r.count));
  return (
    <ul className="rs-intent-bars">
      {rows.map((r) => (
        <li key={r.intent}>
          <span title={r.intent}>{r.intent.replace(/_/g, " ")}</span>
          <span className="rs-kb-track">
            <span style={{ width: `${Math.round((r.count / max) * 100)}%`, background: "#6366f1" }} />
          </span>
          <strong>{r.count}</strong>
        </li>
      ))}
    </ul>
  );
}

export default function WhatsAppReplyStats() {
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const period = parsePeriod(params.get("period"));
  const [data, setData] = useState<ReplyAnalytics | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");

  const setPeriod = (p: Period) => {
    const next = new URLSearchParams(params);
    next.set("period", p);
    setParams(next, { replace: true });
  };

  const load = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const r = await api<ReplyAnalytics>(`/api/whatsapp/reply-analytics?period=${period}`);
      setData(r);
    } catch (e) {
      setData(null);
      setErr(e instanceof Error ? e.message : "Failed to load analytics");
    } finally {
      setLoading(false);
    }
  }, [period]);

  useEffect(() => {
    void load();
  }, [load]);

  const pendingTotals = useMemo(() => data?.pending.total ?? {}, [data]);
  const staffItems = data?.staff_reply.items ?? [];

  return (
    <div className="rs-page">
      <header className="rs-head">
        <div>
          <p className="rs-kicker">
            <Link to="/whatsapp" className="rs-back">
              ← WhatsApp Inbox
            </Link>
          </p>
          <h1>Reply stats & staff performance</h1>
          <p className="rs-sub">
            Metrics and charts only — no chat threads or AI assistant logs. Office rules:{" "}
            {data?.office_rules ?? "Mon–Fri 09:30–17:30, Sat 09:30–17:00 IST"} ({data?.timezone ?? "Asia/Kolkata"}).
          </p>
        </div>
        <div className="rs-head-actions">
          <select
            className="rs-period"
            value={period}
            onChange={(e) => setPeriod(e.target.value as Period)}
            aria-label="Stats period"
          >
            {PERIODS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
          <button type="button" className="btn rs-refresh" onClick={() => void load()} disabled={loading}>
            Refresh
          </button>
        </div>
      </header>

      {err ? <div className="rs-error">{err}</div> : null}
      {loading && !data ? <div className="rs-loading">Loading…</div> : null}

      {data ? (
        <div className={`rs-grid ${loading ? "rs-dim" : ""}`}>
          <section className="rs-card rs-span-2">
            <h2>Staff reply time (office vs off)</h2>
            <StaffOfficeOffBars items={staffItems} />
          </section>

          <section className="rs-card">
            <h2>Pending unreplied</h2>
            <p className="rs-card-stat">
              <strong>{data.pending.grand_total}</strong> open chats need a reply
            </p>
            <ul className="rs-pill-row">
              {(["sales", "service", "cloud", "other"] as const).map((k) => (
                <li key={k}>
                  <span className="rs-pill" style={{ borderColor: CAT_COLORS[k] }}>
                    {CAT_LABELS[k]} <strong>{pendingTotals[k] ?? 0}</strong>
                  </span>
                </li>
              ))}
            </ul>
            <PendingByStaff byStaff={data.pending.by_staff} />
          </section>

          <section className="rs-card">
            <h2>Dealer vs lead response</h2>
            <p className="rs-hint">Uses CRM Dealer/Lead tags from phone &amp; dealer link on each conversation.</p>
            <ContactCompare block={data.contact_reply} />
          </section>

          <section className="rs-card">
            <h2>Chat category mix</h2>
            <p className="rs-hint">Active DMs in period (message activity or still awaiting reply).</p>
            <Donut totals={data.categories.active_conversations} />
          </section>

          <section className="rs-card rs-span-2">
            <h2>Lead asking KB (knowledge base)</h2>
            <div className="rs-kb-summary">
              <StatTile label="Lead KB chats" value={String(data.kb_leads.lead_kb_conversations)} />
              <StatTile label="% of active lead chats" value={`${data.kb_leads.rate_of_active_chats_pct}%`} />
              <StatTile label="AI KB suggestions" value={String(data.kb_leads.suggestion_kb_hits)} />
              <StatTile label="Heuristic keyword hits" value={String(data.kb_leads.heuristic_conversations)} />
            </div>
            <p className="rs-kb-doc" title={data.kb_leads.detection}>
              Detection: AI suggestion KB match + product/FAQ keyword heuristic on inbound lead messages.
            </p>
            <KbStaffBars rows={data.kb_leads.by_staff} />
            {data.kb_leads.top_intents.length ? (
              <>
                <h3 className="rs-h3">Top AI intents (period)</h3>
                <IntentBars rows={data.kb_leads.top_intents} />
              </>
            ) : null}
          </section>
        </div>
      ) : null}

      <footer className="rs-foot">
        <button type="button" className="btn" onClick={() => nav("/whatsapp")}>
          Back to inbox
        </button>
      </footer>
    </div>
  );
}

function StatTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rs-kb-stat">
      <span className="rs-muted">{label}</span>
      <strong>{value}</strong>
    </div>
  );
}