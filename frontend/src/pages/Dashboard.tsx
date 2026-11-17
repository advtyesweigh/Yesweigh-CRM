import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import AdmsStatusBadge from "../components/AdmsStatusBadge";
import IndiaSalesMap from "../components/IndiaSalesMap";
import { sortByStaffDisplayOrder } from "../staffDisplayOrder";

type DashboardOut = {
  leads_total: number;
  leads_new: number;
  work_open: number;
  employees_active: number;
  modules: string[];
};

type LeadPage = {
  items: Lead[];
  total: number;
  status_counts?: Record<string, number>;
};

type Lead = {
  id: number;
  name: string;
  phone: string;
  city: string;
  district: string;
  state: string;
  status: string;
  source: string;
};

type WaMetrics = {
  open: number;
  unread: number;
  unassigned: number;
  agents_online: number;
  agents_total: number;
};

type AdmsKpis = {
  total_employees: number;
  present: number;
  present_pct: number;
  on_time: number;
  late: number;
  on_leave: number;
  absent: number;
  checkins: number;
};

type AdmsDetailRow = {
  employee_name: string;
  employee_number: string;
  date: string;
  date_iso: string;
  shift?: string;
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

type AdmsDaily = {
  ok?: boolean;
  date_from?: string;
  date_to?: string;
  showing_last_working_day?: boolean;
  display_note?: string | null;
  total?: number;
  kpis?: AdmsKpis;
  items: AdmsDetailRow[];
};

const ATT_AVATAR_COLORS = ["#3b82f6", "#10b981", "#8b5cf6", "#f59e0b", "#ef4444", "#06b6d4", "#ec4899", "#6366f1"];

function attInitials(name: string) {
  const parts = name.replace(/^Employee\s+/i, "").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

function attWorkHoursLabel(iso: string, shiftFromApi?: string): string {
  if (shiftFromApi && shiftFromApi.trim()) return shiftFromApi;
  const d = new Date(`${iso.slice(0, 10)}T12:00:00`);
  if (Number.isNaN(d.getTime())) return "—";
  const wd = d.getDay();
  if (wd === 0) return "Off";
  if (wd === 6) return "9:30 to 5:00";
  return "9:30 to 5:30";
}

function AttLatePill({ minutes, status }: { minutes?: number; status: string }) {
  const m = minutes ?? 0;
  if (status === "Absent" || status === "On Leave") {
    return <span className="adms-muted">—</span>;
  }
  if (m <= 0) {
    return (
      <span className="adms-late-pill ontime">
        <span className="adms-late-ico" aria-hidden>
          ✓
        </span>
        On Time
      </span>
    );
  }
  return (
    <span className="adms-late-pill late">
      <span className="adms-late-ico" aria-hidden>
        ⏱
      </span>
      Late
    </span>
  );
}

type RangeKey = "this_month" | "last_month" | "last_7" | "today";

const RANGE_OPTIONS: { key: RangeKey; label: string }[] = [
  { key: "this_month", label: "This Month" },
  { key: "last_month", label: "Last Month" },
  { key: "last_7", label: "Last 7 Days" },
  { key: "today", label: "Today" },
];

const PRODUCT_PLACEHOLDERS = [
  "Top product 1",
  "Top product 2",
  "Top product 3",
  "Top product 4",
  "Top product 5",
  "Top product 6",
  "Top product 7",
  "Top product 8",
  "Top product 9",
  "Top product 10",
];

type ProductSalesItem = {
  name: string;
  sku?: string;
  value: number;
  qty: number;
  value_pct: number;
  qty_pct: number;
  image_url?: string;
};

type ProductSalesOut = {
  year: number;
  total_value: number;
  total_qty: number;
  by_value?: ProductSalesItem[];
  by_qty?: ProductSalesItem[];
  categories?: ProductSalesItem[];
};

type MonthSalesPoint = {
  year: number;
  month: number;
  label: string;
  total: number;
  total_lakh: number;
  invoice_count: number;
};

type SalesOverviewOut = {
  as_of: string;
  this_month_total: number;
  this_month_invoice_count: number;
  this_year_total: number;
  this_year_invoice_count: number;
  fy_label?: string;
  fy_from?: string;
  fy_to?: string;
  last_month_till_date_total: number;
  last_month_till_date_invoice_count: number;
  compare_from: string;
  compare_to: string;
  target: number | null;
  target_pct: number | null;
  months: MonthSalesPoint[];
};

type SalesmanSalesItem = {
  name: string;
  value: number;
  invoice_count: number;
  value_pct: number;
};

type SalesBySalesmanOut = {
  year: number;
  fy_label?: string;
  fy_from?: string;
  fy_to?: string;
  total_value: number;
  items: SalesmanSalesItem[];
};

type TopDealerYtdItem = {
  dealer_id: number;
  name: string;
  amount: number;
  invoice_count: number;
};

type TopDealersYtdOut = {
  year: number;
  fy_label?: string;
  as_of: string;
  from_date: string;
  items: TopDealerYtdItem[];
};

/** Indian FY start calendar year (Apr–Mar). Nov 2026 → 2026; Feb 2027 → 2026. */
function indianFyStartYear(d = new Date()) {
  const y = d.getFullYear();
  const m = d.getMonth() + 1;
  return m >= 4 ? y : y - 1;
}

function indianFyLabel(startYear = indianFyStartYear()) {
  return `${startYear}-${String(startYear + 1).slice(-2)}`;
}

function fmtINRCompact(n: number | null | undefined) {
  if (n == null || Number.isNaN(n)) return "₹ —";
  const abs = Math.abs(n);
  if (abs >= 1e7) return `₹${(n / 1e7).toFixed(2)} Cr`;
  if (abs >= 1e5) return `₹${(n / 1e5).toFixed(1)} L`;
  return `₹${Math.round(n).toLocaleString("en-IN")}`;
}

function fmtQty(n: number | null | undefined) {
  if (n == null || Number.isNaN(n)) return "0";
  return Math.round(n).toLocaleString("en-IN");
}

function todayISO() {
  const d = new Date();
  const off = d.getTimezoneOffset();
  return new Date(d.getTime() - off * 60000).toISOString().slice(0, 10);
}

function addDaysISO(iso: string, delta: number) {
  const d = new Date(`${iso}T12:00:00`);
  d.setDate(d.getDate() + delta);
  const off = d.getTimezoneOffset();
  return new Date(d.getTime() - off * 60000).toISOString().slice(0, 10);
}

function monthBounds(offset = 0): { from: string; to: string; label: string } {
  const now = new Date();
  const y = now.getFullYear();
  const m = now.getMonth() + offset;
  const start = new Date(y, m, 1);
  const end = new Date(y, m + 1, 0);
  const fmt = (d: Date) => {
    const off = d.getTimezoneOffset();
    return new Date(d.getTime() - off * 60000).toISOString().slice(0, 10);
  };
  const from = fmt(start);
  const to = fmt(end);
  const monthName = start.toLocaleString("en-GB", { month: "short" });
  return {
    from,
    to,
    label: `${monthName} ${start.getDate()} – ${monthName} ${end.getDate()}, ${end.getFullYear()}`,
  };
}

function rangeMeta(key: RangeKey) {
  const today = todayISO();
  if (key === "today") return { from: today, to: today, label: today };
  if (key === "last_7") return { from: addDaysISO(today, -6), to: today, label: "Last 7 days" };
  if (key === "last_month") {
    const b = monthBounds(-1);
    return { from: b.from, to: b.to, label: b.label };
  }
  const b = monthBounds(0);
  return { from: b.from, to: b.to, label: b.label };
}

function fmtNum(n: number | null | undefined) {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-IN");
}

function fmtInr(n: number | null | undefined) {
  if (n == null || Number.isNaN(n)) return "₹ —";
  return `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

type SalesByStateOut = {
  year: number;
  items: { state: string; sales_total: number; invoice_count?: number }[];
};

function Donut({
  pct,
  label,
  size = 120,
  color = "#2563eb",
}: {
  pct: number;
  label: string;
  size?: number;
  color?: string;
}) {
  const clamped = Math.max(0, Math.min(100, pct));
  const r = 42;
  const c = 2 * Math.PI * r;
  const dash = (clamped / 100) * c;
  return (
    <div className="sd-donut" style={{ width: size, height: size }}>
      <svg viewBox="0 0 100 100" width={size} height={size} aria-hidden>
        <circle cx="50" cy="50" r={r} fill="none" stroke="#e8eef8" strokeWidth="12" />
        <circle
          cx="50"
          cy="50"
          r={r}
          fill="none"
          stroke={color}
          strokeWidth="12"
          strokeLinecap="round"
          strokeDasharray={`${dash} ${c - dash}`}
          transform="rotate(-90 50 50)"
        />
      </svg>
      <div className="sd-donut-label">
        <strong>{Math.round(clamped)}%</strong>
        <span>{label}</span>
      </div>
    </div>
  );
}

function CategoryThumb({ src, label }: { src?: string; label: string }) {
  const [broken, setBroken] = useState(false);
  if (src && !broken) {
    return (
      <img
        className="sd-cat-thumb"
        src={src}
        alt=""
        loading="lazy"
        onError={() => setBroken(true)}
      />
    );
  }
  return (
    <span className="sd-cat-thumb sd-cat-thumb--ph" aria-hidden>
      {(label || "?").slice(0, 1)}
    </span>
  );
}

function SkuValueIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <ellipse cx="12" cy="6" rx="7" ry="3" />
      <path d="M5 6v4c0 1.7 3.1 3 7 3s7-1.3 7-3V6" />
      <path d="M5 10v4c0 1.7 3.1 3 7 3s7-1.3 7-3v-4" />
      <path d="M5 14v4c0 1.7 3.1 3 7 3s7-1.3 7-3v-4" />
    </svg>
  );
}

function SkuQtyIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
      <path d="M3.3 7 12 12l8.7-5" />
      <path d="M12 22V12" />
    </svg>
  );
}

type SkuModalMode = "value" | "qty" | null;

function SkuSalesModal({
  open,
  mode,
  year,
  onClose,
}: {
  open: boolean;
  mode: SkuModalMode;
  year: number;
  onClose: () => void;
}) {
  const [rows, setRows] = useState<ProductSalesItem[]>([]);
  const [totalValue, setTotalValue] = useState(0);
  const [totalQty, setTotalQty] = useState(0);
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!open || !mode) return;
    let cancelled = false;
    setLoading(true);
    setErr("");
    setQ("");
    api<ProductSalesOut>(`/api/dashboard/sales-by-product?year=${year}&limit=300`)
      .then((res) => {
        if (cancelled) return;
        setRows(mode === "value" ? res.by_value || [] : res.by_qty || []);
        setTotalValue(res.total_value || 0);
        setTotalQty(res.total_qty || 0);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : "Failed to load SKUs");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, mode, year]);

  if (!open || !mode) return null;

  const term = q.trim().toLowerCase();
  const filtered = term
    ? rows.filter(
        (r) =>
          r.name.toLowerCase().includes(term) ||
          (r.sku || "").toLowerCase().includes(term)
      )
    : rows;

  const title = mode === "value" ? "Sales by Value — all SKUs" : "Sales by Items Qty — catalog SKUs";
  const sub =
    mode === "value"
      ? `${year} · ${fmtINRCompact(totalValue)} total`
      : `${year} · Products catalog only · ${fmtQty(totalQty)} units`;

  return (
    <div
      className="sd-sku-modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="sd-sku-dialog" role="dialog" aria-modal="true" aria-label={title}>
        <div className="sd-sku-dialog-head">
          <div>
            <h2>{title}</h2>
            <p>{sub}</p>
          </div>
          <button type="button" className="btn" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <div className="sd-sku-dialog-toolbar">
          <input
            className="sd-sku-search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search name or SKU…"
            autoFocus
          />
          <span className="sd-sku-count">
            {filtered.length}
            {term ? ` / ${rows.length}` : ""} SKU{filtered.length === 1 ? "" : "s"}
          </span>
        </div>
        {err ? <div className="sd-sku-err">{err}</div> : null}
        {loading ? (
          <div className="sd-sku-err" style={{ color: "var(--muted)" }}>
            Loading…
          </div>
        ) : (
          <ul className="sd-sku-full-list">
            {filtered.map((row) => (
              <li key={`${mode}-${row.sku || row.name}`}>
                <CategoryThumb src={row.image_url} label={row.name} />
                <div className="sd-sku-meta">
                  <span className="sd-sku-full-name">{row.name}</span>
                  <span className="sd-sku-sub">
                    {row.qty > 0 ? `${fmtQty(row.qty)} sold` : "—"}
                  </span>
                  <span className="sd-sku-sub">
                    {row.value > 0 ? fmtINRCompact(row.value) : "₹ —"}
                  </span>
                </div>
                <span className="sd-sku-pct">
                  {mode === "value" ? `${row.value_pct}%` : `${row.qty_pct}%`}
                </span>
              </li>
            ))}
            {!filtered.length && !err ? (
              <li className="sd-sku-empty">No matching SKUs.</li>
            ) : null}
          </ul>
        )}
      </div>
    </div>
  );
}

const MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"];

function monthFullLabel(m: MonthSalesPoint) {
  const name = MONTH_SHORT[Math.max(0, Math.min(11, m.month - 1))] || m.label;
  return `${name} ${m.year}`;
}

function fmtSalesLakh(n: number | null | undefined) {
  if (n == null || Number.isNaN(n)) return "₹ —";
  if (Math.abs(n) >= 1e5) return `₹${(n / 1e5).toFixed(2)} L`;
  return fmtInr(n);
}

export default function Dashboard() {
  const nav = useNavigate();
  const [range, setRange] = useState<RangeKey>("this_month");
  const meta = useMemo(() => rangeMeta(range), [range]);

  const [loading, setLoading] = useState(true);
  const [dash, setDash] = useState<DashboardOut | null>(null);
  const [statusCounts, setStatusCounts] = useState<Record<string, number>>({});
  const [wa, setWa] = useState<WaMetrics | null>(null);
  const [att, setAtt] = useState<AdmsKpis | null>(null);
  const [attDetails, setAttDetails] = useState<AdmsDetailRow[]>([]);
  const [attTotal, setAttTotal] = useState(0);
  const [attWeek, setAttWeek] = useState<{ label: string; pct: number }[]>([]);
  const [salesByState, setSalesByState] = useState<{ state: string; sales_total: number }[]>([]);
  const [salesYear, setSalesYear] = useState(new Date().getFullYear());
  const [productSales, setProductSales] = useState<ProductSalesOut | null>(null);
  const [salesOverview, setSalesOverview] = useState<SalesOverviewOut | null>(null);
  const [salesBySalesman, setSalesBySalesman] = useState<SalesBySalesmanOut | null>(null);
  const [topDealersYtd, setTopDealersYtd] = useState<TopDealersYtdOut | null>(null);
  const [skuModal, setSkuModal] = useState<SkuModalMode>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setErr("");
      const today = todayISO();
      const weekFrom = addDaysISO(today, -6);

      try {
        const year = new Date().getFullYear();
        const fyYear = indianFyStartYear();
        const [
          d,
          leads,
          waRes,
          attToday,
          attWeekRes,
          salesState,
          productRes,
          overviewRes,
          salesmanRes,
          topDealersRes,
        ] = await Promise.all([
          api<DashboardOut>("/api/dashboard"),
          api<LeadPage>("/api/leads?page=1&page_size=1"),
          api<{ metrics: WaMetrics }>("/api/whatsapp/conversations?limit=1").catch(() => null),
          api<AdmsDaily>(
            `/api/integrations/adms/daily?date_from=${today}&date_to=${today}&page=1&page_size=200`
          ).catch(() => null),
          api<AdmsDaily>(
            `/api/integrations/adms/daily?date_from=${weekFrom}&date_to=${today}&page=1&page_size=500`
          ).catch(() => null),
          api<SalesByStateOut>("/api/dealers/sales-by-state").catch(() => null),
          api<ProductSalesOut>(`/api/dashboard/sales-by-product?year=${year}&limit=10`).catch(() => null),
          api<SalesOverviewOut>("/api/dashboard/sales-overview").catch(() => null),
          api<SalesBySalesmanOut>(`/api/dashboard/sales-by-salesman?year=${fyYear}&limit=20`).catch(() => null),
          api<TopDealersYtdOut>(`/api/dashboard/top-dealers-ytd?year=${fyYear}&limit=6`).catch(() => null),
        ]);

        if (cancelled) return;
        setDash(d);
        setStatusCounts(leads.status_counts || {});
        setWa(waRes?.metrics || null);
        setAtt(attToday?.kpis || null);
        setAttDetails(sortByStaffDisplayOrder(attToday?.items || []));
        setAttTotal(attToday?.total ?? attToday?.items?.length ?? 0);
        setProductSales(productRes);
        setSalesOverview(overviewRes);
        setSalesBySalesman(salesmanRes);
        setTopDealersYtd(topDealersRes);

        const totalEmp = attToday?.kpis?.total_employees || d.employees_active || 1;
        const byDay = new Map<string, number>();
        for (const item of attWeekRes?.items || []) {
          if (item.status === "On Time" || item.status === "Late") {
            byDay.set(item.date_iso, (byDay.get(item.date_iso) || 0) + 1);
          }
        }
        const weekBars: { label: string; pct: number }[] = [];
        for (let i = 6; i >= 0; i--) {
          const iso = addDaysISO(today, -i);
          const present = byDay.get(iso) || 0;
          const pct = Math.round((present / Math.max(1, totalEmp)) * 100);
          const dObj = new Date(`${iso}T12:00:00`);
          weekBars.push({
            label: dObj.toLocaleString("en-GB", { weekday: "short" }),
            pct: Math.min(100, pct),
          });
        }
        setAttWeek(weekBars);
        if (salesState) {
          setSalesYear(salesState.year);
          setSalesByState(salesState.items || []);
        } else {
          setSalesByState([]);
        }
      } catch (e) {
        if (!cancelled) setErr(e instanceof Error ? e.message : "Failed to load dashboard");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [range]);

  const leadsTotal = dash?.leads_total ?? 0;
  const workOpen = dash?.work_open ?? 0;
  const unreadWa = wa?.unread ?? 0;
  const presentPct = att?.present_pct ?? 0;
  const presentN = att?.present ?? 0;
  const totalEmp = att?.total_employees ?? dash?.employees_active ?? 0;
  const lateN = att?.late ?? 0;
  const leaveN = att?.on_leave ?? 0;
  const absentN = att?.absent ?? 0;

  const kpis = [
    {
      key: "missed",
      title: "Missed Calls",
      value: "0",
      sub: "Call module pending",
      tone: "muted" as const,
      icon: "☎",
      iconClass: "blue",
    },
    {
      key: "wa",
      title: "No Reply WhatsApp",
      value: fmtNum(unreadWa),
      sub: unreadWa > 0 ? "Unread messages from phone" : "All clear",
      tone: unreadWa > 0 ? ("neg" as const) : ("pos" as const),
      icon: "WA",
      iconClass: "green",
    },
    {
      key: "leads",
      title: "Total Leads",
      value: fmtNum(leadsTotal),
      sub: `${fmtNum(statusCounts.new ?? 0)} new`,
      tone: "pos" as const,
      icon: "👥",
      iconClass: "blue",
    },
    {
      key: "work",
      title: "Open Work",
      value: fmtNum(workOpen),
      sub: workOpen > 0 ? "Todo / doing / blocked" : "No open items",
      tone: workOpen > 0 ? ("neg" as const) : ("muted" as const),
      icon: "▤",
      iconClass: "blue",
    },
    {
      key: "att",
      title: "Staff Attendance",
      value: totalEmp ? `${Math.round(presentPct)}%` : "—",
      sub: totalEmp ? `Present: ${presentN} / ${totalEmp}` : "No ADMS data",
      tone: "muted" as const,
      icon: "📅",
      iconClass: "blue",
    },
    {
      key: "sale",
      title: "Total Sale This Month",
      value: fmtINRCompact(salesOverview?.this_month_total ?? 0),
      sub: salesOverview
        ? `${fmtNum(salesOverview.this_month_invoice_count)} invoices · MTD`
        : "Loading Zoho sales…",
      tone: (salesOverview?.this_month_total || 0) > 0 ? ("pos" as const) : ("muted" as const),
      icon: "₹",
      iconClass: "blue",
    },
    {
      key: "cmp",
      title: "Last Month Comparison",
      value: fmtINRCompact(salesOverview?.last_month_till_date_total ?? 0),
      sub: "Same days last month",
      tone: "muted" as const,
      icon: "↗",
      iconClass: "blue",
    },
  ];

  // API returns newest month first; keep placeholder in the same order.
  const sixMonthSeries = salesOverview?.months?.length
    ? salesOverview.months
    : (() => {
        const out: MonthSalesPoint[] = [];
        const now = new Date();
        for (let i = 0; i < 6; i++) {
          const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
          out.push({
            year: d.getFullYear(),
            month: d.getMonth() + 1,
            label: d.toLocaleString("en-GB", { month: "short", year: "2-digit" }),
            total: 0,
            total_lakh: 0,
            invoice_count: 0,
          });
        }
        return out;
      })();

  const topDealers = topDealersYtd?.items ?? [];

  const topStates = salesByState.slice(0, 20);
  const stateColMid = Math.ceil(topStates.length / 2);
  const stateCols = [topStates.slice(0, stateColMid), topStates.slice(stateColMid)];

  return (
    <div className="sd-page">
      <div className="heading sd-heading">
        <div className="actions sd-actions">
          <AdmsStatusBadge />
          <label className="sd-range" title={meta.label}>
            <span className="sd-range-ico" aria-hidden>
              📅
            </span>
            <select value={range} onChange={(e) => setRange(e.target.value as RangeKey)} aria-label="Date range">
              {RANGE_OPTIONS.map((o) => (
                <option key={o.key} value={o.key}>
                  {o.key === range ? `${o.label} (${meta.label})` : o.label}
                </option>
              ))}
            </select>
          </label>
          <button className="btn" type="button" onClick={() => nav("/leads")}>
            New lead
          </button>
          <button className="btn primary" type="button" onClick={() => nav("/campaigns")}>
            New campaign
          </button>
        </div>
      </div>

      {err && <p className="err">{err}</p>}
      {loading && <p className="sd-loading">Loading dashboard…</p>}

      <div className="sd-kpis">
        {kpis.map((k) => (
          <div key={k.key} className="sd-kpi">
            <div className="sd-kpi-top">
              <span className="sd-kpi-title">{k.title}</span>
              <span className={`sd-kpi-ico ${k.iconClass}`}>{k.icon}</span>
            </div>
            <div className="sd-kpi-val">{k.value}</div>
            <div className={`sd-kpi-sub ${k.tone}`}>{k.sub}</div>
          </div>
        ))}
      </div>

      <div className="sd-row sd-row-att">
        <section className="panel sd-card">
          <div className="panelhead">
            <div>
              <h2>Staff Attendance</h2>
              <p>Today · ADMS</p>
            </div>
          </div>
          <div className="sd-att-body">
            <ul className="sd-att-legend">
              <li>
                <i className="dot-g" /> Present <b>{presentN}</b>
              </li>
              <li>
                <i className="dot-r" /> Absent <b>{absentN}</b>
              </li>
              <li>
                <i className="dot-a" /> On Leave <b>{leaveN}</b>
              </li>
              <li>
                <i className="dot-o" /> Late <b>{lateN}</b>
              </li>
            </ul>
            <Donut pct={presentPct} label="Present" />
          </div>
          <div className="sd-att-week">
            <div className="sd-mini-title">Attendance (Last 7 Days)</div>
            <div className="sd-bars">
              {attWeek.map((d) => (
                <div key={d.label + d.pct} className="sd-bar-col">
                  <div className="sd-bar-track">
                    <div className="sd-bar-fill" style={{ height: `${Math.max(4, d.pct)}%` }} />
                  </div>
                  <span>{d.label}</span>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="panel sd-card sd-att-records">
          <div className="panelhead">
            <div>
              <h2>Attendance Records</h2>
              <p>Today · ADMS</p>
            </div>
            <span className="adms-total-badge">Total: {fmtNum(attTotal)} Records</span>
          </div>
          <div className="adms-table-wrap sd-att-table-wrap">
            <table className="adms-att-table">
              <thead>
                <tr>
                  <th>Employee Name</th>
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
                {!attDetails.length && (
                  <tr>
                    <td colSpan={8} style={{ textAlign: "center", color: "var(--muted)", padding: 28 }}>
                      {loading ? "Loading attendance…" : "No attendance for this date."}
                    </td>
                  </tr>
                )}
                {attDetails.map((r, i) => (
                  <tr key={`${r.employee_number}-${r.date_iso}-${r.status}`}>
                    <td>
                      <div className="adms-emp">
                        <span
                          className="adms-av"
                          style={{ background: ATT_AVATAR_COLORS[i % ATT_AVATAR_COLORS.length] }}
                        >
                          {attInitials(r.employee_name)}
                        </span>
                        <b>{r.employee_name}</b>
                      </div>
                    </td>
                    <td>
                      <div className="adms-shift">
                        <b className="mono">{r.date}</b>
                        <small>{attWorkHoursLabel(r.date_iso, r.shift)}</small>
                      </div>
                    </td>
                    <td className="mono">{r.check_in_display || r.check_in}</td>
                    <td>
                      <AttLatePill minutes={r.late_minutes} status={r.status} />
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
        </section>
      </div>

      <div className="sd-row sd-row-3">
        <section className="panel sd-card">
          <div className="panelhead">
            <div>
              <h2>Top Dealers this year</h2>
              <p>
                FY {topDealersYtd?.fy_label || salesOverview?.fy_label || indianFyLabel()} · excl. GST
              </p>
            </div>
          </div>
          <ul className="sd-top-dealers" aria-label="Top 6 dealers this financial year">
            {topDealers.length === 0 && (
              <li className="sd-top-dealers-empty">
                {loading ? "Loading dealers…" : "No dealer sales this FY"}
              </li>
            )}
            {topDealers.map((d, i) => (
              <li key={d.dealer_id}>
                <span className="sd-top-dealers-rank">{i + 1}</span>
                <span className="sd-top-dealers-name" title={d.name}>
                  {d.name}
                </span>
                <strong className="sd-top-dealers-amt">{fmtInr(d.amount)}</strong>
              </li>
            ))}
          </ul>
        </section>

        <section className="panel sd-card">
          <div className="panelhead">
            <div>
              <h2>Sales Last 6 Months</h2>
              <p>Month · Value (₹ Lakh, excl. GST)</p>
            </div>
          </div>
          <ul className="sd-month-sales" aria-label="Sales last 6 months">
            {sixMonthSeries.map((m) => (
              <li key={`${m.year}-${m.month}`} title={fmtInr(m.total)}>
                <span className="sd-month-sales-label">{monthFullLabel(m)}</span>
                <span className="sd-month-sales-sep" aria-hidden>
                  ·
                </span>
                <strong className="sd-month-sales-val">{fmtSalesLakh(m.total)}</strong>
              </li>
            ))}
          </ul>
        </section>

        <section className="panel sd-card">
          <div className="panelhead">
            <div>
              <h2>Sales by Salesman</h2>
              <p>FY {salesBySalesman?.fy_label || indianFyLabel()} · excl. GST</p>
            </div>
          </div>
          <div className="sd-salesman">
            {(salesBySalesman?.items?.length
              ? salesBySalesman.items
              : [{ name: "No sales data", value: 0, invoice_count: 0, value_pct: 0 }]
            ).map((row) => (
              <div key={row.name} className="sd-salesman-row">
                <div className="sd-salesman-meta">
                  <b>{row.name}</b>
                  <span>{row.value > 0 ? fmtINRCompact(row.value) : "₹ —"}</span>
                </div>
                <div className="track">
                  <div className="fill" style={{ width: `${Math.min(100, Math.max(0, row.value_pct))}%` }} />
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>

      <div className="sd-row sd-row-sku">
        <section className="panel sd-card sd-sku-card">
          <div className="panelhead sd-sku-head">
            <div className="sd-sku-head-left">
              <span className="sd-sku-ico" aria-hidden>
                <SkuValueIcon />
              </span>
              <div>
                <h2>Sales by Value</h2>
                <p>Top SKUs</p>
              </div>
            </div>
            <div className="sd-value-head-right">
              <div className="sd-value-total">
                <strong>{productSales && productSales.total_value > 0 ? fmtINRCompact(productSales.total_value) : "₹ —"}</strong>
                <span>{productSales?.year ?? new Date().getFullYear()} · year total</span>
              </div>
              <button type="button" className="btn" onClick={() => setSkuModal("value")}>
                All
              </button>
            </div>
          </div>
          <div className="sd-sku-table-wrap">
            <div className="sd-sku-table-cols" aria-hidden>
              <span className="sd-sku-col-rank">#</span>
              <span className="sd-sku-col-product">Product</span>
              <span className="sd-sku-col-share">Share</span>
            </div>
            <ul className="sd-sku-table">
              {(productSales?.by_value?.length
                ? productSales.by_value
                : PRODUCT_PLACEHOLDERS.map((name) => ({
                    name,
                    sku: "",
                    value: 0,
                    qty: 0,
                    value_pct: 0,
                    qty_pct: 0,
                    image_url: "",
                  }))
              ).map((row, i) => (
                <li key={`val-${row.sku || row.name}`}>
                  <button
                    type="button"
                    className="sd-sku-row-btn sd-sku-table-row"
                    title={row.sku ? `${row.name} · ${row.sku}` : row.name}
                    onClick={() => setSkuModal("value")}
                  >
                    <span className="sd-sku-rank">{i + 1}</span>
                    <div className="sd-sku-product">
                      <CategoryThumb src={row.image_url} label={row.name} />
                      <div className="sd-sku-meta">
                        <span className="sd-sku-name">{row.name}</span>
                        <span className="sd-sku-sub">
                          {row.qty > 0 ? `${fmtQty(row.qty)} sold` : "—"}
                        </span>
                        <span className="sd-sku-sub">
                          {row.value > 0 ? fmtINRCompact(row.value) : "₹ —"}
                        </span>
                      </div>
                    </div>
                    <div className="sd-sku-share">
                      <div className="track">
                        <div
                          className="fill"
                          style={{ width: `${Math.min(100, Math.max(0, row.value_pct))}%` }}
                        />
                      </div>
                      <span className="sd-sku-share-pct">{row.value_pct}%</span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section className="panel sd-card sd-sku-card">
          <div className="panelhead sd-sku-head">
            <div className="sd-sku-head-left">
              <span className="sd-sku-ico" aria-hidden>
                <SkuQtyIcon />
              </span>
              <div>
                <h2>Sales by Items Qty</h2>
                <p>Top SKUs · catalog products · {productSales?.year ?? new Date().getFullYear()}</p>
              </div>
            </div>
            <div className="sd-value-head-right">
              <div className="sd-value-total">
                <strong>
                  {productSales && productSales.total_qty > 0
                    ? fmtQty(productSales.total_qty)
                    : "—"}
                </strong>
                <span>{productSales?.year ?? new Date().getFullYear()} · year total</span>
              </div>
              <button type="button" className="btn" onClick={() => setSkuModal("qty")}>
                All
              </button>
            </div>
          </div>
          <div className="sd-sku-table-wrap">
            <div className="sd-sku-table-cols" aria-hidden>
              <span className="sd-sku-col-rank">#</span>
              <span className="sd-sku-col-product">Product</span>
              <span className="sd-sku-col-share">Share</span>
            </div>
            <ul className="sd-sku-table">
              {(productSales?.by_qty?.length
                ? productSales.by_qty
                : PRODUCT_PLACEHOLDERS.map((name) => ({
                    name,
                    sku: "",
                    value: 0,
                    qty: 0,
                    value_pct: 0,
                    qty_pct: 0,
                    image_url: "",
                  }))
              ).map((row, i) => (
                <li key={`qty-${row.sku || row.name}`}>
                  <button
                    type="button"
                    className="sd-sku-row-btn sd-sku-table-row"
                    title={row.sku ? `${row.name} · ${row.sku}` : row.name}
                    onClick={() => setSkuModal("qty")}
                  >
                    <span className="sd-sku-rank">{i + 1}</span>
                    <div className="sd-sku-product">
                      <CategoryThumb src={row.image_url} label={row.name} />
                      <div className="sd-sku-meta">
                        <span className="sd-sku-name">{row.name}</span>
                        <span className="sd-sku-sub">
                          {row.qty > 0 ? `${fmtQty(row.qty)} sold` : "—"}
                        </span>
                        <span className="sd-sku-sub">
                          {row.value > 0 ? fmtINRCompact(row.value) : "₹ —"}
                        </span>
                      </div>
                    </div>
                    <div className="sd-sku-share">
                      <div className="track">
                        <div
                          className="fill"
                          style={{ width: `${Math.min(100, Math.max(0, row.qty_pct))}%` }}
                        />
                      </div>
                      <span className="sd-sku-share-pct">{row.qty_pct}%</span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </section>
      </div>

      <section className="panel sd-card">
        <div className="panelhead">
          <div>
            <h2>Dealer Sales by State</h2>
            <p>This year · excl. GST ({salesYear})</p>
          </div>
        </div>
        <div className="sd-state-wrap">
          <IndiaSalesMap items={salesByState} />
          <div className="sd-state-cols">
            {topStates.length === 0 ? (
              <div className="sd-table-wrap sd-state-cols-empty">
                <table>
                  <tbody>
                    <tr>
                      <td colSpan={2} className="sd-empty-row">
                        No sales by state yet.
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            ) : (
              stateCols.map((col, ci) => (
                <div className="sd-table-wrap" key={ci}>
                  <table>
                    <thead>
                      <tr>
                        <th>State</th>
                        <th>Sales</th>
                      </tr>
                    </thead>
                    <tbody>
                      {col.map((r, ri) => {
                        const rank = ci * stateColMid + ri + 1;
                        return (
                          <tr key={r.state}>
                            <td>
                              {rank}. {r.state}
                            </td>
                            <td>{fmtInr(r.sales_total)}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              ))
            )}
          </div>
        </div>
      </section>

      <SkuSalesModal
        open={skuModal != null}
        mode={skuModal}
        year={productSales?.year ?? new Date().getFullYear()}
        onClose={() => setSkuModal(null)}
      />
    </div>
  );
}
