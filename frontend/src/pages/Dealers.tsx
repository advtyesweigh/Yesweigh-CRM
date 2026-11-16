import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";

type CohortFilter = "" | "active" | "inactive";
type SalesFilter = "" | "with_sales" | "zero";

type Dealer = {
  id: number;
  name: string;
  company: string;
  phone: string;
  email: string;
  city: string;
  state: string;
  gstin: string;
  zoho_contact_id: string;
  source: string;
  status: string;
  assigned_salesman?: string | null;
  sales_total?: number | null;
  last_sale_date?: string | null;
};

type DealerStats = {
  total: number;
  active: number;
  inactive: number;
  no_whatsapp: number;
  high_value: number;
  states_covered: number;
  high_value_threshold?: number;
};

type KpiKey = "total" | "active" | "inactive" | "no_whatsapp" | "high_value" | "states";

const HIGH_VALUE_THRESHOLD_FALLBACK = 1_000_000;

type Page = {
  items: Dealer[];
  total: number;
  page: number;
  page_size: number;
  stats: DealerStats;
};

type ZohoAddress = {
  attention?: string;
  address?: string;
  street2?: string;
  city?: string;
  state?: string;
  zip?: string;
  country?: string;
  phone?: string;
  fax?: string;
};

type ZohoContactPerson = {
  contact_person_id?: string;
  first_name?: string;
  last_name?: string;
  email?: string;
  phone?: string;
  mobile?: string;
  is_primary_contact?: boolean;
  designation?: string;
};

type ZohoCustomField = {
  label?: string;
  value?: string | number | boolean | null;
  customfield_id?: string;
  api_name?: string;
};

type ZohoContact = Record<string, unknown> & {
  contact_id?: string;
  contact_name?: string;
  company_name?: string;
  email?: string;
  phone?: string;
  mobile?: string;
  status?: string;
  contact_type?: string;
  gst_no?: string;
  tax_reg_no?: string;
  pan_no?: string;
  notes?: string;
  billing_address?: ZohoAddress;
  shipping_address?: ZohoAddress;
  contact_persons?: ZohoContactPerson[];
  custom_fields?: ZohoCustomField[];
};

type ZohoDetail = {
  dealer_id: number;
  zoho_contact_id: string;
  source: "sql" | "cached" | "live";
  contact: ZohoContact;
  fallback_reason: string | null;
};

type SortKey = "sales_total" | "last_sale_date" | "name";
type SortOrder = "asc" | "desc";

const PAGE_SIZE_OPTIONS = [10, 50, 100] as const;

function dash(v: unknown): string {
  if (v === null || v === undefined) return "—";
  const s = String(v).trim();
  return s || "—";
}

function fmtNum(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-IN");
}

function fmtPct(part: number, whole: number): string {
  if (!whole) return "0% of total";
  return `${((part / whole) * 100).toFixed(1)}% of total`;
}

function fmtAmount(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

function normalizeInMobile(value: string | null | undefined): string {
  let digits = String(value || "").replace(/\D+/g, "");
  if (!digits) return "";
  if (digits.startsWith("91") && digits.length > 10) digits = digits.slice(2);
  if (digits.startsWith("0") && digits.length === 11) digits = digits.slice(1);
  if (digits.length >= 10) return digits.slice(-10);
  return "";
}

function fmtSaleDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const s = String(iso).trim();
  if (!s) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
  if (!m) return s;
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const month = months[Number(m[2]) - 1] || m[2];
  return `${Number(m[3])} ${month} ${m[1]}`;
}

function formatAddress(addr?: ZohoAddress | null): string {
  if (!addr || typeof addr !== "object") return "";
  return [
    addr.attention,
    addr.address,
    addr.street2,
    [addr.city, addr.state].filter(Boolean).join(", "),
    [addr.zip, addr.country].filter(Boolean).join(" "),
    addr.phone ? `Phone: ${addr.phone}` : "",
    addr.fax ? `Fax: ${addr.fax}` : "",
  ]
    .map((x) => (x || "").trim())
    .filter(Boolean)
    .join("\n");
}

function personName(p: ZohoContactPerson): string {
  const n = [p.first_name, p.last_name].filter(Boolean).join(" ").trim();
  return n || "Contact person";
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="dealer-zoho-field">
      <div className="dealer-zoho-label">{label}</div>
      <div className="dealer-zoho-value">{value}</div>
    </div>
  );
}

function IconUsers() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M22 21v-2a4 4 0 0 0-3-3.87" />
      <path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  );
}

function IconCheck() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}

function IconInactive() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <path d="M15 9 9 15" />
      <path d="M9 9l6 6" />
    </svg>
  );
}

function IconWhatsAppOutline() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
      <path
        fill="currentColor"
        d="M12.04 2C6.58 2 2.15 6.4 2.15 11.83c0 1.97.52 3.88 1.5 5.57L2 22l4.77-1.55a9.9 9.9 0 0 0 5.27 1.5h.01c5.46 0 9.89-4.4 9.89-9.83C21.94 6.4 17.5 2 12.04 2Zm5.74 14.04c-.24.67-1.4 1.24-1.94 1.32-.5.07-1.13.1-1.82-.11-.42-.13-.96-.31-1.65-.61-2.9-1.25-4.79-4.17-4.93-4.36-.14-.19-1.15-1.53-1.15-2.92 0-1.39.73-2.07.99-2.35.26-.28.57-.35.76-.35h.55c.18 0 .41-.07.64.49.24.58.81 2 .88 2.15.07.14.12.31.02.5-.1.19-.15.31-.3.48-.14.16-.3.37-.43.5-.14.14-.29.29-.12.57.16.28.72 1.19 1.55 1.93 1.06.95 1.96 1.24 2.24 1.38.28.14.44.12.6-.07.16-.19.7-.81.88-1.09.19-.28.37-.23.62-.14.26.09 1.63.77 1.91.91.28.14.47.21.54.33.07.12.07.7-.17 1.37Z"
      />
    </svg>
  );
}

function IconCoins() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <ellipse cx="12" cy="6" rx="7" ry="3" />
      <path d="M5 6v4c0 1.7 3.1 3 7 3s7-1.3 7-3V6" />
      <path d="M5 10v4c0 1.7 3.1 3 7 3s7-1.3 7-3v-4" />
    </svg>
  );
}

function IconPin() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 22s7-4.5 7-11a7 7 0 1 0-14 0c0 6.5 7 11 7 11Z" />
      <circle cx="12" cy="11" r="2.5" />
    </svg>
  );
}

function WhatsAppIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
      <path
        fill="#25D366"
        d="M12.04 2C6.58 2 2.15 6.4 2.15 11.83c0 1.97.52 3.88 1.5 5.57L2 22l4.77-1.55a9.9 9.9 0 0 0 5.27 1.5h.01c5.46 0 9.89-4.4 9.89-9.83C21.94 6.4 17.5 2 12.04 2Zm5.74 14.04c-.24.67-1.4 1.24-1.94 1.32-.5.07-1.13.1-1.82-.11-.42-.13-.96-.31-1.65-.61-2.9-1.25-4.79-4.17-4.93-4.36-.14-.19-1.15-1.53-1.15-2.92 0-1.39.73-2.07.99-2.35.26-.28.57-.35.76-.35h.55c.18 0 .41-.07.64.49.24.58.81 2 .88 2.15.07.14.12.31.02.5-.1.19-.15.31-.3.48-.14.16-.3.37-.43.5-.14.14-.29.29-.12.57.16.28.72 1.19 1.55 1.93 1.06.95 1.96 1.24 2.24 1.38.28.14.44.12.6-.07.16-.19.7-.81.88-1.09.19-.28.37-.23.62-.14.26.09 1.63.77 1.91.91.28.14.47.21.54.33.07.12.07.7-.17 1.37Z"
      />
    </svg>
  );
}

function SortArrows({ active, order }: { active?: boolean; order?: SortOrder }) {
  return (
    <span className={`dealer-sort-arrows${active ? " active" : ""}`} aria-hidden="true">
      <svg viewBox="0 0 12 14" width="10" height="12">
        <path d="M6 1 10 6H2L6 1Z" fill={active && order === "asc" ? "currentColor" : "#94a3b8"} />
        <path d="M6 13 2 8h8L6 13Z" fill={active && order === "desc" ? "currentColor" : "#94a3b8"} />
      </svg>
    </span>
  );
}

function pageWindow(current: number, totalPages: number): (number | "…")[] {
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, i) => i + 1);
  }
  const pages = new Set<number>();
  pages.add(1);
  pages.add(totalPages);
  for (let i = current - 1; i <= current + 1; i++) {
    if (i >= 1 && i <= totalPages) pages.add(i);
  }
  if (current <= 3) {
    pages.add(2);
    pages.add(3);
    pages.add(4);
    pages.add(5);
  }
  if (current >= totalPages - 2) {
    pages.add(totalPages - 1);
    pages.add(totalPages - 2);
    pages.add(totalPages - 3);
    pages.add(totalPages - 4);
  }
  const sorted = [...pages].sort((a, b) => a - b);
  const out: (number | "…")[] = [];
  let prev = 0;
  for (const p of sorted) {
    if (prev && p - prev > 1) out.push("…");
    out.push(p);
    prev = p;
  }
  return out;
}

export default function Dealers() {
  const [searchInput, setSearchInput] = useState("");
  const [q, setQ] = useState("");
  const [cohort, setCohort] = useState<CohortFilter>("");
  const [stateFilter, setStateFilter] = useState("");
  const [districtFilter, setDistrictFilter] = useState("");
  const [kamFilter, setKamFilter] = useState("");
  const [salesFilter, setSalesFilter] = useState<SalesFilter>("");
  const [stateOptions, setStateOptions] = useState<string[]>([]);
  const [districtOptions, setDistrictOptions] = useState<string[]>([]);
  const [kamOptions, setKamOptions] = useState<string[]>([]);
  const [advOpen, setAdvOpen] = useState(false);
  const advRef = useRef<HTMLDivElement | null>(null);
  const [kpiFilter, setKpiFilter] = useState<KpiKey>("active");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(10);
  const [sort, setSort] = useState<SortKey>("sales_total");
  const [order, setOrder] = useState<SortOrder>("desc");
  const [data, setData] = useState<Page | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  const [detailDealer, setDetailDealer] = useState<Dealer | null>(null);
  const [detail, setDetail] = useState<ZohoDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailErr, setDetailErr] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const pageIds = useMemo(() => (data?.items || []).map((d) => d.id), [data?.items]);
  const allPageSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id));
  const somePageSelected = pageIds.some((id) => selected.has(id));
  const pages = useMemo(
    () => Math.max(1, Math.ceil((data?.total || 0) / pageSize)),
    [data?.total, pageSize]
  );

  const rangeStart = data && data.total > 0 ? (page - 1) * pageSize + 1 : 0;
  const rangeEnd = data ? Math.min(page * pageSize, data.total) : 0;

  const advActiveCount = useMemo(() => {
    let n = 0;
    if (cohort) n += 1;
    if (stateFilter) n += 1;
    if (districtFilter) n += 1;
    if (kamFilter) n += 1;
    if (salesFilter) n += 1;
    return n;
  }, [cohort, stateFilter, districtFilter, kamFilter, salesFilter]);

  async function load(
    p = page,
    query = q,
    size = pageSize,
    sortKey = sort,
    sortOrder = order,
    kpi: KpiKey = kpiFilter,
    state = stateFilter,
    district = districtFilter,
    nextCohort: CohortFilter = cohort,
    kam = kamFilter,
    sales: SalesFilter = salesFilter
  ) {
    setLoading(true);
    setErr("");
    try {
      const params = new URLSearchParams({
        page: String(p),
        page_size: String(size),
        sort: sortKey,
        order: sortOrder,
      });
      if (query.trim()) params.set("q", query.trim());
      if (state.trim()) params.set("state", state.trim());
      if (district.trim()) params.set("district", district.trim());
      if (kam.trim()) params.set("kam", kam.trim());
      if (sales) params.set("sales_filter", sales);
      if (nextCohort) params.set("cohort", nextCohort);
      if (
        kpi === "no_whatsapp" ||
        kpi === "high_value" ||
        kpi === "active" ||
        kpi === "inactive" ||
        kpi === "total" ||
        kpi === "states"
      ) {
        params.set("kpi", kpi);
      }
      const res = await api<Page>(`/api/dealers?${params}`);
      setData(res);
      setSelected(new Set());
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load dealers");
    } finally {
      setLoading(false);
    }
  }

  function applyKpi(next: KpiKey) {
    if (next === "states") {
      setKpiFilter("states");
      return;
    }
    // KPI Active/Inactive compose with Advanced geo/KAM/sales; clear Advanced cohort
    // so the tile drives the sales-recency list filter.
    const nextCohort: CohortFilter =
      next === "active" || next === "inactive" ? "" : cohort;
    setKpiFilter(next);
    if (next === "active" || next === "inactive") setCohort("");
    setPage(1);
    void load(
      1,
      q,
      pageSize,
      sort,
      order,
      next,
      stateFilter,
      districtFilter,
      nextCohort,
      kamFilter,
      salesFilter
    );
  }

  function toggleAllPage() {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allPageSelected) {
        for (const id of pageIds) next.delete(id);
      } else {
        for (const id of pageIds) next.add(id);
      }
      return next;
    });
  }

  function toggleOne(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  useEffect(() => {
    void load(1, "", pageSize, sort, order, "active", "", "", "", "", "");
    void api<string[]>("/api/dealers/states")
      .then((rows) => setStateOptions(Array.isArray(rows) ? rows : []))
      .catch(() => setStateOptions([]));
    void api<string[]>("/api/dealers/kams")
      .then((rows) => setKamOptions(Array.isArray(rows) ? rows : []))
      .catch(() => setKamOptions([]));
    // initial load only
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Live search — debounce ~280ms (no Search button).
  useEffect(() => {
    const t = window.setTimeout(() => {
      const next = searchInput.trim();
      if (next === q.trim()) return;
      setQ(next);
      setPage(1);
      void load(1, next, pageSize, sort, order, kpiFilter, stateFilter, districtFilter, cohort, kamFilter, salesFilter);
    }, 280);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchInput]);

  useEffect(() => {
    if (!stateFilter) {
      setDistrictOptions([]);
      return;
    }
    let cancelled = false;
    void api<string[]>(`/api/dealers/cities?state=${encodeURIComponent(stateFilter)}`)
      .then((rows) => {
        if (!cancelled) setDistrictOptions(Array.isArray(rows) ? rows : []);
      })
      .catch(() => {
        if (!cancelled) setDistrictOptions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [stateFilter]);

  useEffect(() => {
    if (!advOpen) return;
    function onDoc(e: MouseEvent) {
      if (!advRef.current) return;
      if (!advRef.current.contains(e.target as Node)) setAdvOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setAdvOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [advOpen]);

  function reloadWith(
    patch: Partial<{
      cohort: CohortFilter;
      state: string;
      district: string;
      kam: string;
      sales: SalesFilter;
      kpi: KpiKey;
    }>
  ) {
    const nextCohort = patch.cohort !== undefined ? patch.cohort : cohort;
    const nextState = patch.state !== undefined ? patch.state : stateFilter;
    const nextDistrict = patch.district !== undefined ? patch.district : districtFilter;
    const nextKam = patch.kam !== undefined ? patch.kam : kamFilter;
    const nextSales = patch.sales !== undefined ? patch.sales : salesFilter;
    const nextKpi = patch.kpi !== undefined ? patch.kpi : kpiFilter;
    setPage(1);
    void load(
      1,
      q,
      pageSize,
      sort,
      order,
      nextKpi,
      nextState,
      nextDistrict,
      nextCohort,
      nextKam,
      nextSales
    );
  }

  function onCohortChange(next: CohortFilter) {
    setCohort(next);
    // Advanced cohort owns Active/Inactive — reset KPI tile to total so stats/list stay coherent.
    setKpiFilter("total");
    reloadWith({ cohort: next, kpi: "total" });
  }

  function onStateChange(next: string) {
    setStateFilter(next);
    setDistrictFilter("");
    reloadWith({ state: next, district: "" });
  }

  function onDistrictChange(next: string) {
    setDistrictFilter(next);
    reloadWith({ district: next });
  }

  function onKamChange(next: string) {
    setKamFilter(next);
    reloadWith({ kam: next });
  }

  function onSalesChange(next: SalesFilter) {
    setSalesFilter(next);
    reloadWith({ sales: next });
  }

  function clearAdvanced() {
    setCohort("");
    setStateFilter("");
    setDistrictFilter("");
    setKamFilter("");
    setSalesFilter("");
    setKpiFilter("total");
    setPage(1);
    void load(1, q, pageSize, sort, order, "total", "", "", "", "", "");
  }

  function applySort(nextSort: SortKey, nextOrder: SortOrder) {
    setSort(nextSort);
    setOrder(nextOrder);
    setPage(1);
    void load(1, q, pageSize, nextSort, nextOrder, kpiFilter, stateFilter, districtFilter, cohort, kamFilter, salesFilter);
  }

  function onHeaderSort(key: SortKey) {
    if (sort === key) {
      applySort(key, order === "desc" ? "asc" : "desc");
    } else {
      applySort(key, key === "name" ? "asc" : "desc");
    }
  }

  function goToPage(p: number) {
    const next = Math.min(Math.max(1, p), pages);
    setPage(next);
    void load(next, q, pageSize, sort, order, kpiFilter, stateFilter, districtFilter, cohort, kamFilter, salesFilter);
  }

  function onPageSizeChange(size: number) {
    setPageSize(size);
    setPage(1);
    void load(1, q, size, sort, order, kpiFilter, stateFilter, districtFilter, cohort, kamFilter, salesFilter);
  }

  async function openZohoDetail(d: Dealer) {
    if (!d.zoho_contact_id) {
      setDetailDealer(d);
      setDetail(null);
      setDetailErr("This dealer has no Zoho ID");
      return;
    }
    setDetailDealer(d);
    setDetail(null);
    setDetailErr("");
    setDetailLoading(true);
    try {
      const res = await api<ZohoDetail>(`/api/dealers/${d.id}/zoho`);
      setDetail(res);
    } catch (e) {
      setDetailErr(e instanceof Error ? e.message : "Failed to load Zoho details");
    } finally {
      setDetailLoading(false);
    }
  }

  function closeDetail() {
    setDetailDealer(null);
    setDetail(null);
    setDetailErr("");
    setDetailLoading(false);
  }

  useEffect(() => {
    if (!detailDealer) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") closeDetail();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [detailDealer]);

  const c = detail?.contact;
  const billing = formatAddress(c?.billing_address);
  const shipping = formatAddress(c?.shipping_address);
  const persons = Array.isArray(c?.contact_persons) ? c!.contact_persons! : [];
  const customFields = Array.isArray(c?.custom_fields) ? c!.custom_fields! : [];
  const title =
    (c?.contact_name as string) ||
    (c?.company_name as string) ||
    detailDealer?.name ||
    detailDealer?.company ||
    "Dealer";

  const st = data?.stats;
  const totalN = st?.total ?? 0;
  const activeN = st?.active ?? 0;
  const inactiveN = st?.inactive ?? 0;
  const noWaN = st?.no_whatsapp ?? 0;
  const highValueN = st?.high_value ?? 0;
  const statesN = st?.states_covered ?? 0;
  const highValueThreshold = st?.high_value_threshold ?? HIGH_VALUE_THRESHOLD_FALLBACK;
  const highValueLakhs = Math.round(highValueThreshold / 100_000);

  const kpis: {
    key: KpiKey;
    title: string;
    value: string;
    sub: string;
    tone: "blue" | "green" | "slate" | "red" | "purple" | "orange";
    subTone: "muted" | "pos" | "neg" | "orange" | "slate";
    icon: React.ReactNode;
    filters: boolean;
  }[] = [
    {
      key: "total",
      title: "Total Dealers",
      value: data ? fmtNum(totalN) : "—",
      sub: "All visible dealers",
      tone: "blue",
      subTone: "muted",
      icon: <IconUsers />,
      filters: true,
    },
    {
      key: "active",
      title: "Active",
      value: data ? fmtNum(activeN) : "—",
      sub: "Sale in last 6 mo",
      tone: "green",
      subTone: "pos",
      icon: <IconCheck />,
      filters: true,
    },
    {
      key: "inactive",
      title: "Inactive",
      value: data ? fmtNum(inactiveN) : "—",
      sub: "No sale in 6 mo",
      tone: "slate",
      subTone: "slate",
      icon: <IconInactive />,
      filters: true,
    },
    {
      key: "no_whatsapp",
      title: "No WhatsApp",
      value: data ? fmtNum(noWaN) : "—",
      sub: data ? fmtPct(noWaN, totalN) : "—",
      tone: "red",
      subTone: "neg",
      icon: <IconWhatsAppOutline />,
      filters: true,
    },
    {
      key: "high_value",
      title: "High Value Dealers",
      value: data ? fmtNum(highValueN) : "—",
      sub: `≥ ₹${highValueLakhs} L sales`,
      tone: "purple",
      subTone: "muted",
      icon: <IconCoins />,
      filters: true,
    },
    {
      key: "states",
      title: "States Covered",
      value: data ? fmtNum(statesN) : "—",
      sub: "Across India",
      tone: "orange",
      subTone: "orange",
      icon: <IconPin />,
      filters: false,
    },
  ];

  const pagerItems = pageWindow(page, pages);

  function renderPagerNav(keyPrefix: string) {
    return (
      <div className="dealers-pager-nav" aria-label="Pagination">
        <button
          type="button"
          className="dealers-page-btn"
          disabled={page <= 1 || loading}
          onClick={() => goToPage(1)}
          aria-label="First page"
        >
          «
        </button>
        <button
          type="button"
          className="dealers-page-btn"
          disabled={page <= 1 || loading}
          onClick={() => goToPage(page - 1)}
          aria-label="Previous page"
        >
          ‹
        </button>
        {pagerItems.map((item, i) =>
          item === "…" ? (
            <span key={`${keyPrefix}-e${i}`} className="dealers-page-ellipsis">
              …
            </span>
          ) : (
            <button
              key={`${keyPrefix}-p${item}`}
              type="button"
              className={`dealers-page-btn${item === page ? " active" : ""}`}
              disabled={loading}
              onClick={() => goToPage(item)}
              aria-label={`Page ${item}`}
              aria-current={item === page ? "page" : undefined}
            >
              {item}
            </button>
          )
        )}
        <button
          type="button"
          className="dealers-page-btn"
          disabled={page >= pages || loading}
          onClick={() => goToPage(page + 1)}
          aria-label="Next page"
        >
          ›
        </button>
      </div>
    );
  }

  return (
    <div className="emp-page">
      <div className="dealers-kpis" role="toolbar" aria-label="Dealer KPI filters">
        {kpis.map((k) => {
          const selectedKpi = kpiFilter === k.key;
          return (
            <button
              key={k.key}
              type="button"
              className={`dealer-kpi tone-${k.tone}${selectedKpi ? " is-active" : ""}`}
              onClick={() => applyKpi(k.key)}
              aria-pressed={selectedKpi}
              title={
                k.filters
                  ? `Filter dealers: ${k.title}`
                  : "States covered (summary only — list unchanged)"
              }
            >
              <div className={`dealer-kpi-ico tone-${k.tone}`}>{k.icon}</div>
              <div className="dealer-kpi-body">
                <div className="dealer-kpi-title">{k.title}</div>
                <div className="dealer-kpi-val">{k.value}</div>
                <div className={`dealer-kpi-sub ${k.subTone}`}>{k.sub}</div>
              </div>
            </button>
          );
        })}
      </div>

      <div className="dealers-toolbar">
        <div className={`dealers-search-wrap${searchInput ? " has-value" : ""}`}>
          <span className="dealers-search-ico" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.5-3.5" />
            </svg>
          </span>
          <input
            className="dealers-search"
            placeholder="Search name, phone, email, city, Zoho id…"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            aria-label="Search dealers"
          />
          {searchInput ? (
            <button
              type="button"
              className="dealers-search-clear"
              onClick={() => setSearchInput("")}
              aria-label="Clear search"
            >
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden="true">
                <path d="M18 6 6 18" />
                <path d="m6 6 12 12" />
              </svg>
            </button>
          ) : null}
        </div>
        <div className="dealers-adv-wrap" ref={advRef}>
          <button
            type="button"
            className={`btn dealers-adv-btn${advOpen ? " is-open" : ""}${advActiveCount ? " is-active" : ""}`}
            onClick={() => setAdvOpen((o) => !o)}
            aria-expanded={advOpen}
            aria-haspopup="dialog"
          >
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M4 5h16l-6.2 7.2V19l-3.6 2v-8.8L4 5Z" />
            </svg>
            <span>Advanced filter</span>
            {advActiveCount > 0 ? (
              <span className="dealers-adv-badge" aria-label={`${advActiveCount} filters active`}>
                {advActiveCount}
              </span>
            ) : null}
          </button>
          {advOpen && (
            <div className="dealers-adv-panel" role="dialog" aria-label="Advanced filters">
              <div className="dealers-adv-head">
                <div>
                  <div className="dealers-adv-title">Filters</div>
                  <div className="dealers-adv-sub">Narrow the dealer list · applies instantly</div>
                </div>
              </div>
              <div className="dealers-adv-grid">
                <label className="dealers-adv-field">
                  <span>Status</span>
                  <select
                    className="dealers-adv-select"
                    value={cohort}
                    onChange={(e) => onCohortChange(e.target.value as CohortFilter)}
                    aria-label="Filter by status cohort"
                  >
                    <option value="">All</option>
                    <option value="active">Active</option>
                    <option value="inactive">Inactive</option>
                  </select>
                </label>
                <label className="dealers-adv-field">
                  <span>State</span>
                  <select
                    className="dealers-adv-select"
                    value={stateFilter}
                    onChange={(e) => onStateChange(e.target.value)}
                    aria-label="Filter by state"
                  >
                    <option value="">All</option>
                    {stateOptions.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="dealers-adv-field">
                  <span>District</span>
                  <select
                    className="dealers-adv-select"
                    value={districtFilter}
                    onChange={(e) => onDistrictChange(e.target.value)}
                    aria-label="Filter by district"
                    disabled={!stateFilter}
                  >
                    <option value="">{stateFilter ? "All" : "Select state first"}</option>
                    {districtOptions.map((d) => (
                      <option key={d} value={d}>
                        {d}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="dealers-adv-field">
                  <span>KAM</span>
                  <select
                    className="dealers-adv-select"
                    value={kamFilter}
                    onChange={(e) => onKamChange(e.target.value)}
                    aria-label="Filter by KAM"
                  >
                    <option value="">All</option>
                    {kamOptions.map((k) => (
                      <option key={k} value={k}>
                        {k}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="dealers-adv-field">
                  <span>Sales</span>
                  <select
                    className="dealers-adv-select"
                    value={salesFilter}
                    onChange={(e) => onSalesChange(e.target.value as SalesFilter)}
                    aria-label="Filter by sales"
                  >
                    <option value="">All</option>
                    <option value="with_sales">With sales</option>
                    <option value="zero">Sales = zero</option>
                  </select>
                </label>
              </div>
              <div className="dealers-adv-actions">
                <button
                  type="button"
                  className="dealers-adv-clear"
                  onClick={clearAdvanced}
                  disabled={!advActiveCount}
                >
                  Clear filters
                </button>
              </div>
            </div>
          )}
        </div>
      </div>

      {err && <div className="err" style={{ marginBottom: 12 }}>{err}</div>}

      <p className="dealer-dblclick-hint">Double-click a row to view Zoho details (read-only).</p>

      <div className="panel adms-table-card dealers-table-card" style={{ padding: 0 }}>
        <div className="dealers-table-chrome">
          <div className="dealers-showing">
            {data
              ? `Showing ${fmtNum(rangeStart)} – ${fmtNum(rangeEnd)} of ${fmtNum(data.total)} dealers`
              : loading
                ? "Loading…"
                : "Showing 0 – 0 of 0 dealers"}
          </div>
          {renderPagerNav("top")}
        </div>

        <div className="adms-table-wrap">
          <table className="adms-att-table dealers-table">
            <thead>
              <tr>
                <th className="dealer-check-col">
                  <input
                    type="checkbox"
                    checked={allPageSelected}
                    ref={(el) => {
                      if (el) el.indeterminate = somePageSelected && !allPageSelected;
                    }}
                    onChange={toggleAllPage}
                    aria-label="Select all dealers on this page"
                  />
                </th>
                <th>
                  <button type="button" className="dealer-th-sort" onClick={() => onHeaderSort("name")}>
                    Name
                    <SortArrows active={sort === "name"} order={sort === "name" ? order : undefined} />
                  </button>
                </th>
                <th>Phone</th>
                <th>City</th>
                <th>State</th>
                <th>KAM</th>
                <th>
                  <button type="button" className="dealer-th-sort" onClick={() => onHeaderSort("last_sale_date")}>
                    Last Sale Date
                    <SortArrows
                      active={sort === "last_sale_date"}
                      order={sort === "last_sale_date" ? order : undefined}
                    />
                  </button>
                </th>
                <th className="dealer-num-col">
                  <button type="button" className="dealer-th-sort dealer-th-sort-end" onClick={() => onHeaderSort("sales_total")}>
                    Total Sales (excl. GST)
                    <SortArrows active={sort === "sales_total"} order={sort === "sales_total" ? order : undefined} />
                  </button>
                </th>
              </tr>
            </thead>
            <tbody>
              {loading && !data ? (
                <tr>
                  <td colSpan={8} style={{ padding: 24, color: "var(--muted)" }}>
                    Loading dealers…
                  </td>
                </tr>
              ) : (data?.items || []).length === 0 ? (
                <tr>
                  <td colSpan={8} style={{ padding: 24, color: "var(--muted)" }}>
                    No dealers yet. Sync from Settings → Zoho Sync after connecting Books.
                  </td>
                </tr>
              ) : (
                (data?.items || []).map((d) => {
                  const mobile = normalizeInMobile(d.phone);
                  const waHref = mobile ? `https://wa.me/91${mobile}` : null;
                  return (
                    <tr
                      key={d.id}
                      className="dealer-row"
                      onDoubleClick={() => void openZohoDetail(d)}
                      title="Double-click to open Zoho details"
                    >
                      <td className="dealer-check-col" onClick={(e) => e.stopPropagation()} onDoubleClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={selected.has(d.id)}
                          onChange={() => toggleOne(d.id)}
                          aria-label={`Select ${d.name || d.company || "dealer"}`}
                        />
                      </td>
                      <td>
                        <div className="dealer-name-main">{d.name || d.company || "—"}</div>
                        {d.company && d.name && d.company !== d.name ? (
                          <div className="dealer-name-sub">{d.company}</div>
                        ) : null}
                      </td>
                      <td>
                        {d.phone ? (
                          <span className="dealer-phone-cell">
                            {waHref ? (
                              <a
                                className="dealer-wa-link"
                                href={waHref}
                                target="_blank"
                                rel="noopener noreferrer"
                                title="Open WhatsApp"
                                onClick={(e) => e.stopPropagation()}
                                onDoubleClick={(e) => e.stopPropagation()}
                              >
                                <WhatsAppIcon />
                              </a>
                            ) : (
                              <span className="dealer-wa-muted" title="No valid WhatsApp number">
                                <WhatsAppIcon />
                              </span>
                            )}
                            <span>{d.phone}</span>
                          </span>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td>{d.city || "—"}</td>
                      <td>{d.state || "—"}</td>
                      <td>{(d.assigned_salesman || "").trim() || "—"}</td>
                      <td>{fmtSaleDate(d.last_sale_date)}</td>
                      <td className="dealer-num-col">{fmtAmount(d.sales_total)}</td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        <div className="dealers-pager">
          <label className="dealers-page-size">
            <span>Rows per page</span>
            <select
              value={pageSize}
              onChange={(e) => onPageSizeChange(Number(e.target.value))}
              disabled={loading}
              aria-label="Rows per page"
            >
              {PAGE_SIZE_OPTIONS.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>

          {renderPagerNav("bottom")}
        </div>
      </div>

      {detailDealer && (
        <div
          className="dealer-zoho-modal"
          onClick={(e) => {
            if (e.target === e.currentTarget) closeDetail();
          }}
        >
          <div
            className="dealer-zoho-dialog"
            role="dialog"
            aria-modal="true"
            aria-label="Zoho dealer details"
          >
            <div className="dealer-zoho-head">
              <div>
                <h2>{title}</h2>
                <p>
                  Zoho ID <code>{detailDealer.zoho_contact_id || "—"}</code>
                  {detail ? (
                    <>
                      {" · "}
                      <span className="dealer-zoho-src cached">
                        {detail.source === "sql" || detail.source === "cached"
                          ? "From SQL"
                          : "Stored Zoho payload"}
                      </span>
                    </>
                  ) : null}
                </p>
              </div>
              <button type="button" className="btn" onClick={closeDetail} aria-label="Close">
                Close
              </button>
            </div>

            {detailLoading ? (
              <p className="dealer-zoho-muted">Loading Zoho details…</p>
            ) : detailErr ? (
              <div className="err">{detailErr}</div>
            ) : c ? (
              <div className="dealer-zoho-body">
                {detail?.fallback_reason ? (
                  <div className="dealer-zoho-banner">
                    {detail.fallback_reason}
                  </div>
                ) : null}

                <section className="dealer-zoho-section">
                  <h3>Overview</h3>
                  <div className="dealer-zoho-grid">
                    <Field label="Name" value={dash(c.contact_name)} />
                    <Field label="Company" value={dash(c.company_name)} />
                    <Field label="Status" value={dash(c.status || c.contact_type)} />
                    <Field label="Zoho ID" value={<code>{dash(c.contact_id || detail?.zoho_contact_id)}</code>} />
                    <Field label="Email" value={dash(c.email)} />
                    <Field label="Phone" value={dash(c.phone)} />
                    <Field label="Mobile" value={dash(c.mobile)} />
                    <Field label="GST / Tax ID" value={dash(c.gst_no || c.tax_reg_no)} />
                    <Field label="PAN" value={dash(c.pan_no)} />
                  </div>
                </section>

                {persons.length > 0 ? (
                  <section className="dealer-zoho-section">
                    <h3>Contact persons</h3>
                    <div className="dealer-zoho-persons">
                      {persons.map((p, i) => (
                        <div key={String(p.contact_person_id || i)} className="dealer-zoho-person">
                          <div className="dealer-zoho-person-name">
                            {personName(p)}
                            {p.is_primary_contact ? <span className="badge ok">primary</span> : null}
                          </div>
                          <div className="dealer-zoho-person-meta">
                            {[p.designation, p.email, p.phone, p.mobile].filter(Boolean).join(" · ") || "—"}
                          </div>
                        </div>
                      ))}
                    </div>
                  </section>
                ) : null}

                <section className="dealer-zoho-section">
                  <h3>Addresses</h3>
                  <div className="dealer-zoho-grid">
                    <Field
                      label="Billing"
                      value={
                        billing ? (
                          <pre className="dealer-zoho-pre">{billing}</pre>
                        ) : (
                          "—"
                        )
                      }
                    />
                    <Field
                      label="Shipping"
                      value={
                        shipping ? (
                          <pre className="dealer-zoho-pre">{shipping}</pre>
                        ) : (
                          "—"
                        )
                      }
                    />
                  </div>
                </section>

                {c.notes ? (
                  <section className="dealer-zoho-section">
                    <h3>Notes</h3>
                    <pre className="dealer-zoho-pre">{String(c.notes)}</pre>
                  </section>
                ) : null}

                {customFields.length > 0 ? (
                  <section className="dealer-zoho-section">
                    <h3>Custom fields</h3>
                    <div className="dealer-zoho-grid">
                      {customFields.map((f, i) => (
                        <Field
                          key={String(f.customfield_id || f.api_name || i)}
                          label={f.label || f.api_name || `Field ${i + 1}`}
                          value={dash(f.value)}
                        />
                      ))}
                    </div>
                  </section>
                ) : null}
              </div>
            ) : (
              <p className="dealer-zoho-muted">No Zoho details available.</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
