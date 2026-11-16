import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { sortByStaffDisplayOrder } from "../staffDisplayOrder";
type Kpis = {
  total_employees: number;
  present: number;
  present_pct: number;
  on_time: number;
  on_time_pct: number;
  late: number;
  late_pct: number;
  on_leave: number;
  on_leave_pct: number;
  absent: number;
  absent_pct: number;
  checkins: number;
};

type DailyItem = {
  employee_name: string;
  employee_number: string;
  date: string;
  date_iso: string;
  shift: string;
  check_in: string;
  check_in_display?: string;
  check_out: string;
  check_out_display?: string;
  late: string;
  late_minutes?: number;
  early: string;
  total_late: string;
  total_late_minutes?: number;
  working_hours: string;
  status: string;
};

type DailyPage = {
  ok?: boolean;
  page: number;
  page_size: number;
  total: number;
  pages: number;
  date_from?: string;
  date_to?: string;
  requested_date_from?: string;
  requested_date_to?: string;
  showing_last_working_day?: boolean;
  display_note?: string | null;
  shift?: { today_label?: string; weekday?: string; saturday?: string; sunday?: string; timezone?: string };
  kpis?: Kpis;
  items: DailyItem[];
};

const AVATAR_COLORS = ["#3b82f6", "#10b981", "#8b5cf6", "#f59e0b", "#ef4444", "#06b6d4", "#ec4899", "#6366f1"];

function todayISO() {
  const d = new Date();
  const off = d.getTimezoneOffset();
  return new Date(d.getTime() - off * 60000).toISOString().slice(0, 10);
}

function initials(name: string) {
  const parts = name.replace(/^Employee\s+/i, "").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

/** Prefer API shift label; fallback Mon–Fri 9:30–5:30 · Sat 9:30–5:00 · Sun Off */
function workHoursLabel(iso: string, shiftFromApi?: string): string {
  if (shiftFromApi && shiftFromApi.trim()) return shiftFromApi;
  const d = new Date(`${iso.slice(0, 10)}T12:00:00`);
  if (Number.isNaN(d.getTime())) return "—";
  const wd = d.getDay(); // 0 Sun … 6 Sat
  if (wd === 0) return "Off";
  if (wd === 6) return "9:30 to 5:00";
  return "9:30 to 5:30";
}

function LatePill({ minutes, status }: { minutes?: number; status: string }) {
  const m = minutes ?? 0;
  if (status === "Absent" || status === "On Leave") {
    return <span className="adms-muted">—</span>;
  }
  if (m <= 0) {
    return (
      <span className="adms-late-pill ontime">
        <span className="adms-late-ico" aria-hidden>✓</span>
        On Time
      </span>
    );
  }
  return (
    <span className="adms-late-pill late">
      <span className="adms-late-ico" aria-hidden>⏱</span>
      Late
    </span>
  );
}

export default function Adms({ embedded = false }: { embedded?: boolean }) {
  const [dateFrom, setDateFrom] = useState(todayISO());
  const [dateTo, setDateTo] = useState(todayISO());
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [daily, setDaily] = useState<DailyPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [syncingUsers, setSyncingUsers] = useState(false);
  const [syncMsg, setSyncMsg] = useState("");

  const loadDaily = useCallback(
    async (p = 1) => {
      setLoading(true);
      setErr("");
      try {
        const params = new URLSearchParams({
          page: String(p),
          page_size: "50",
          date_from: dateFrom,
          date_to: dateTo || dateFrom,
        });
        if (q.trim()) params.set("q", q.trim());
        const res = await api<DailyPage>(`/api/integrations/adms/daily?${params}`);
        setDaily({ ...res, items: sortByStaffDisplayOrder(res.items || []) });
        setPage(res.page || p);
      } catch (e) {
        setErr(String(e));
        setDaily(null);
      } finally {
        setLoading(false);
      }
    },
    [dateFrom, dateTo, q]
  );

  useEffect(() => {
    loadDaily(1);
  }, []); // initial

  async function syncUsersFromDevice() {
    if (syncingUsers) return;
    setSyncingUsers(true);
    setSyncMsg("");
    try {
      const res = await api<{ ok?: boolean; message?: string; error?: string; queued?: unknown[] }>(
        "/api/integrations/adms/sync-device-users",
        { method: "POST" }
      );
      if (res.ok === false) {
        setSyncMsg(res.error || "Failed to queue USERINFO sync");
      } else {
        setSyncMsg(
          res.message ||
            `Queued user sync on ${Array.isArray(res.queued) ? res.queued.length : 0} device(s). Re-import employees after the device uploads names.`
        );
      }
    } catch (e) {
      setSyncMsg(String(e));
    } finally {
      setSyncingUsers(false);
    }
  }

  const k = daily?.kpis;

  return (
    <div className={`adms-page ${embedded ? "adms-embedded" : ""}`}>
      <div className="adms-filters panel">
        <div className="adms-filter-grid">
          <label>
            <span>Date From</span>
            <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} />
          </label>
          <label>
            <span>Date To</span>
            <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} />
          </label>
          <label className="adms-search-field">
            <span>Search</span>
            <input
              placeholder="Search employee name or number"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && loadDaily(1)}
            />
          </label>
          <button type="button" className="btn primary" onClick={() => loadDaily(1)} disabled={loading}>
            {loading ? "Loading…" : "Apply Filters"}
          </button>
          <button type="button" className="btn" onClick={() => void syncUsersFromDevice()} disabled={syncingUsers}>
            {syncingUsers ? "Syncing…" : "Query USERINFO"}
          </button>
        </div>
        {syncMsg && <p className="adms-sync-msg">{syncMsg}</p>}
      </div>

      {err && <p className="err">{err}</p>}

      <div className="adms-kpis">
        <Kpi icon="👥" label="Total Employees" value={k?.total_employees ?? "—"} tone="blue" />
        <Kpi icon="✓" label="Present Today" value={k?.present ?? "—"} sub={k ? `${k.present_pct}%` : undefined} tone="green" />
        <Kpi icon="◎" label="On Time" value={k?.on_time ?? "—"} sub={k ? `${k.on_time_pct}%` : undefined} tone="mint" />
        <Kpi icon="⏱" label="Late" value={k?.late ?? "—"} sub={k ? `${k.late_pct}%` : undefined} tone="amber" />
        <Kpi icon="📅" label="On Leave" value={k?.on_leave ?? "—"} sub={k ? `${k.on_leave_pct}%` : undefined} tone="purple" />
        <Kpi icon="✕" label="Absent" value={k?.absent ?? "—"} sub={k ? `${k.absent_pct}%` : undefined} tone="red" />
        <Kpi icon="→" label="Total Check-ins" value={k?.checkins ?? "—"} tone="sky" />
      </div>

      <section className="panel adms-table-card">
        <div className="panelhead">
          <div>
            <h2>Attendance Records</h2>
            {daily?.display_note && <p className="adms-display-note">{daily.display_note}</p>}
          </div>
          {daily && (
            <span className="adms-total-badge">Total: {daily.total.toLocaleString()} Records</span>
          )}
        </div>
        <div className="adms-table-wrap">
          <table className="adms-att-table">
            <thead>
              <tr>
                <th>Employee Name</th>
                <th>Emp Number</th>
                <th>Date</th>
                <th>Check In Time</th>
                <th>Late</th>
                <th>Check Out Time</th>
                <th>Early</th>
                <th>Total late</th>
                <th>Working Hours</th>
              </tr>
            </thead>
            <tbody>
              {(!daily || daily.items.length === 0) && (
                <tr>
                  <td colSpan={9} style={{ textAlign: "center", color: "var(--muted)", padding: 28 }}>
                    {loading ? "Loading attendance…" : "No attendance for this date range."}
                  </td>
                </tr>
              )}
              {(daily?.items || []).map((r, i) => (
                <tr key={`${r.employee_number}-${r.date_iso}-${r.status}`}>
                  <td>
                    <div className="adms-emp">
                      <span className="adms-av" style={{ background: AVATAR_COLORS[i % AVATAR_COLORS.length] }}>
                        {initials(r.employee_name)}
                      </span>
                      <b>{r.employee_name}</b>
                    </div>
                  </td>
                  <td className="mono">{r.employee_number}</td>
                  <td>
                    <div className="adms-shift">
                      <b className="mono">{r.date}</b>
                      <small>{workHoursLabel(r.date_iso, r.shift)}</small>
                    </div>
                  </td>
                  <td className="mono">{r.check_in_display || r.check_in}</td>
                  <td>
                    <LatePill minutes={r.late_minutes} status={r.status} />
                  </td>
                  <td className="mono">{r.check_out_display || r.check_out}</td>
                  <td>{r.early === "—" ? <span className="adms-muted">—</span> : r.early}</td>
                  <td>
                    {(r.total_late_minutes ?? 0) > 0 ? (
                      <span className="adms-late-text">{r.total_late}</span>
                    ) : (
                      <span className="adms-muted">—</span>
                    )}
                  </td>
                  <td className="mono">{r.working_hours}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="adms-pager">
          <span className="adms-pager-meta">
            {daily
              ? `Showing ${(page - 1) * (daily.page_size || 50) + (daily.items.length ? 1 : 0)}–${Math.min(page * (daily.page_size || 50), daily.total)} of ${daily.total} records`
              : "—"}
          </span>
          <div className="lead-pager">
            <button type="button" className="btn" disabled={!daily || page <= 1 || loading} onClick={() => loadDaily(page - 1)}>
              Prev
            </button>
            <span className="adms-page-pill">{daily?.page ?? page}</span>
            <span className="adms-pager-meta">/ {daily?.pages ?? 1}</span>
            <button
              type="button"
              className="btn"
              disabled={!daily || page >= (daily.pages || 1) || loading}
              onClick={() => loadDaily(page + 1)}
            >
              Next
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}

function Kpi({
  icon,
  label,
  value,
  sub,
  tone,
}: {
  icon: string;
  label: string;
  value: string | number;
  sub?: string;
  tone: string;
}) {
  return (
    <div className={`adms-kpi tone-${tone}`}>
      <div className="adms-kpi-top">
        <span className="adms-kpi-ico">{icon}</span>
        {sub != null && <span className="adms-kpi-pct">{sub}</span>}
      </div>
      <div className="adms-kpi-label">{label}</div>
      <div className="adms-kpi-val">{value}</div>
    </div>
  );
}
