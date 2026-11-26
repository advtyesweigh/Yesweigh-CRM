import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode, type Ref } from "react";
import { useNavigate } from "react-router-dom";
import { api, apiWithTimeout } from "../api/client";
import ChatPreviewModal from "../components/intel/ChatPreviewModal";
import IntelAvatar from "../components/intel/IntelAvatar";
import { PackageIcon, SparklesIcon } from "../components/whatsapp/icons";

const PAGE_SIZE_MIN = 5;
const PAGE_SIZE_MAX = 25;
const DEFAULT_ROW_HEIGHT_PX = 46;
const DEFAULT_THEAD_HEIGHT_PX = 44;
const DEFAULT_PAGER_HEIGHT_PX = 44;

function clampPageSize(n: number) {
  if (!Number.isFinite(n) || n < PAGE_SIZE_MIN) return PAGE_SIZE_MIN;
  if (n > PAGE_SIZE_MAX) return PAGE_SIZE_MAX;
  return Math.floor(n);
}

function useIntelPageSize(remeasureKey: string) {
  const tilesRef = useRef<HTMLDivElement>(null);
  const pagerRef = useRef<HTMLDivElement>(null);
  const toolbarRef = useRef<HTMLDivElement>(null);
  const theadRef = useRef<HTMLTableSectionElement>(null);
  const rowProbeRef = useRef<HTMLTableRowElement>(null);
  const [pageSize, setPageSize] = useState(10);

  const recompute = useCallback(() => {
    const tilesBottom = tilesRef.current?.getBoundingClientRect().bottom;
    if (tilesBottom == null) return;

    const pagerH = pagerRef.current?.getBoundingClientRect().height ?? DEFAULT_PAGER_HEIGHT_PX;
    const toolbarH = toolbarRef.current?.getBoundingClientRect().height ?? 0;
    const theadH = theadRef.current?.getBoundingClientRect().height ?? DEFAULT_THEAD_HEIGHT_PX;
    const rowH = rowProbeRef.current?.getBoundingClientRect().height || DEFAULT_ROW_HEIGHT_PX;

    const errEl = tilesRef.current?.closest(".intel-page")?.querySelector(":scope > .err");
    const errH = errEl?.getBoundingClientRect().height ?? 0;

    const available = window.innerHeight - tilesBottom - errH - toolbarH - theadH - pagerH - 6;
    const fitted = clampPageSize(Math.floor(available / rowH));
    setPageSize((prev) => (prev === fitted ? prev : fitted));
  }, []);

  useEffect(() => {
    recompute();
    const ro = new ResizeObserver(() => recompute());
    const targets = [
      tilesRef.current,
      pagerRef.current,
      toolbarRef.current,
      theadRef.current,
      rowProbeRef.current,
    ];
    for (const el of targets) {
      if (el) ro.observe(el);
    }
    window.addEventListener("resize", recompute);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", recompute);
    };
  }, [recompute, remeasureKey]);

  return { pageSize, tilesRef, pagerRef, toolbarRef, theadRef, rowProbeRef, recompute };
}

type IntelRowFields = {
  dealer_name?: string;
  phone?: string;
  avatar_url?: string;
  agent_name?: string;
  priority?: string;
  since_at?: string | null;
  since_ist?: string | null;
  age_seconds?: number | null;
  age_label?: string;
  time_till_label?: string;
  summary?: string;
  status?: string;
  ai_status?: string;
  ai_tag?: string;
  contact_type?: string;
  title?: string;
  subtitle?: string;
};

type Commitment = IntelRowFields & {
  id: string;
  promise_text: string;
  due_on: string | null;
  due_ist?: string | null;
  owner_side: string;
  status: string;
  confidence: string;
  high_lane: boolean;
  contact_name: string;
  href: string;
};

type FunnelItem = IntelRowFields & {
  id: string;
  href?: string;
  invoice_number?: string;
};

type FunnelStage = {
  key: string;
  label: string;
  count: number;
  sum_balance?: number | null;
  items?: FunnelItem[];
  note?: string;
  verified?: boolean;
  /** True once row payloads for this stage were fetched (even if empty). */
  itemsReady?: boolean;
};

function mergeFunnelStages(
  prev: FunnelStage[] | null,
  next: FunnelStage[],
  itemsReadyKeys?: Set<string>,
): FunnelStage[] {
  const byKey = new Map<string, FunnelStage>();
  for (const stage of prev || []) byKey.set(stage.key, stage);
  for (const stage of next) {
    const old = byKey.get(stage.key);
    const ready =
      itemsReadyKeys?.has(stage.key) ||
      stage.itemsReady === true ||
      old?.itemsReady === true ||
      false;
    const keepOldItems =
      !itemsReadyKeys?.has(stage.key) && !(stage.items && stage.items.length) && (old?.items?.length || 0) > 0;
    byKey.set(stage.key, {
      ...old,
      ...stage,
      count: stage.count ?? old?.count ?? 0,
      items: keepOldItems ? old?.items ?? [] : stage.items ?? old?.items ?? [],
      itemsReady: ready || keepOldItems,
    });
  }
  return Array.from(byKey.values());
}

function funnelStageHasItems(funnel: FunnelStage[] | null, key: string | null): boolean {
  if (!funnel || !key) return false;
  const stage = funnel.find((s) => s.key === key);
  return !!stage?.itemsReady;
}

type ServiceRow = IntelRowFields & {
  conversation_id: string;
  customer_name: string;
  tags: string[];
  severity: string;
  href: string;
  last_message_at: string | null;
};

const TABS = [
  "Enquiry",
  "Sales order",
  "Waiting",
  "Paid",
  "Stamping",
  "Service (unresolved)",
  "Commitments",
] as const;

const FUNNEL_STAGE_KEY: Record<(typeof TABS)[number], string | null> = {
  Enquiry: "enquiry",
  "Sales order": "sales_order",
  "Waiting": "invoice_open",
  Paid: "invoice_settled",
  Stamping: null,
  "Service (unresolved)": null,
  Commitments: null,
};

/** API stage keys for Move-to… (matches backend STAGE_KEYS). */
const TAB_TO_MOVE_STAGE: Record<(typeof TABS)[number], string> = {
  Enquiry: "enquiry",
  "Sales order": "sales_order",
  "Waiting": "waiting",
  Paid: "paid",
  Stamping: "stamping",
  "Service (unresolved)": "service",
  Commitments: "commitments",
};

const FUNNEL_TABS = new Set<(typeof TABS)[number]>([
  "Enquiry",
  "Sales order",
  "Waiting",
  "Paid",
]);
const QUEUE_TABS = new Set<(typeof TABS)[number]>(["Stamping", "Service (unresolved)"]);

type IntelTabTone = "blue" | "purple" | "amber" | "green" | "indigo" | "red" | "teal";

const INTEL_TAB_TILE: Record<
  (typeof TABS)[number],
  { tone: IntelTabTone; label: string; icon: ReactNode }
> = {
  Enquiry: { tone: "blue", label: "Enquiry", icon: <IconIntelChat /> },
  "Sales order": { tone: "purple", label: "Sales order", icon: <IconIntelCart /> },
  "Waiting": { tone: "amber", label: "Waiting", icon: <IconIntelWaiting /> },
  Paid: { tone: "green", label: "Paid", icon: <IconIntelPaid /> },
  Stamping: { tone: "indigo", label: "Stamping", icon: <IconIntelStamp /> },
  "Service (unresolved)": { tone: "red", label: "Service", icon: <IconIntelWrench /> },
  Commitments: { tone: "teal", label: "Commitments", icon: <IconIntelHandshake /> },
};

function IconIntelChat() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21 11.5a8.4 8.4 0 0 1-1 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-1L3 21l1.2-5.3A8.4 8.4 0 0 1 3 12.5 8.5 8.5 0 0 1 12 4a8.5 8.5 0 0 1 9 7.5Z" />
    </svg>
  );
}

function IconIntelCart() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="9" cy="20" r="1.5" />
      <circle cx="17" cy="20" r="1.5" />
      <path d="M2.5 2.5h2l2.4 12h10.8l2.4-9H6.2" />
    </svg>
  );
}

function IconIntelWaiting() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5.2l3.4 2" />
    </svg>
  );
}

function IconIntelPaid() {
  return (
    <span className="intel-ico-rupee" aria-hidden="true">
      ₹
    </span>
  );
}

function IconIntelStamp() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 2.75 5.25 5.5v5.35c0 4.65 3 8.85 6.75 10.15 3.75-1.3 6.75-5.5 6.75-10.15V5.5L12 2.75Z" />
      <path d="m8.25 12.25 2.35 2.35L16 9" strokeWidth="2.25" />
    </svg>
  );
}

function IconIntelWrench() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M14.7 6.3a4.25 4.25 0 0 0-5.95 5.95l-6.1 6.1a1.85 1.85 0 0 0 2.6 2.6l6.1-6.1a4.25 4.25 0 0 0 5.95-5.95l-2.2 2.2-3.4-3.4 2.2-2.2Z" />
    </svg>
  );
}

function IconIntelHandshake() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M11 12H3v4a4 4 0 0 0 4 4h12" />
      <path d="m7 16-3-3 3.5-3.5a2 2 0 0 1 2.8 0L15 11" />
      <path d="M22 8v5h-4" />
      <path d="m15 11 2-2 3 3-4.5 4.5a2 2 0 0 1-2.8 0L11 15" />
    </svg>
  );
}

function fmtIntelTileCount(n: number | null, pending: boolean): string {
  if (pending && n === null) return "…";
  if (n === null) return "—";
  return n.toLocaleString("en-IN");
}

function funnelStageCount(funnel: FunnelStage[] | null, stageKey: string): number | null {
  if (funnel === null) return null;
  return funnel.find((s) => s.key === stageKey)?.count ?? 0;
}


function threadIdOf(href?: string) {
  const match = (href || "").match(/[?&]thread=([^&]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

function priorityRank(priority?: string): number {
  const raw = (priority || "").trim().toLowerCase();
  if (raw === "high" || raw === "urgent" || raw === "critical") return 0;
  if (raw === "medium" || raw === "med" || raw === "mid") return 1;
  if (raw === "low") return 2;
  return 3;
}

function sortIntelRows<T extends IntelRowFields>(rows: T[]): T[] {
  return [...rows].sort((a, b) => {
    const byPriority = priorityRank(a.priority) - priorityRank(b.priority);
    if (byPriority !== 0) return byPriority;
    const ageA = a.age_seconds ?? 0;
    const ageB = b.age_seconds ?? 0;
    return ageB - ageA;
  });
}

type IntelListFilters = {
  agent: string;
  type: string;
  priority: string;
  waitSort: "default" | "asc" | "desc";
};

const DEFAULT_INTEL_FILTERS: IntelListFilters = {
  agent: "All",
  type: "All",
  priority: "All",
  waitSort: "default",
};

function agentDisplayName(row: IntelRowFields) {
  return (row.agent_name || "").trim() || "Unassigned";
}

function rowTypeLabel(row: IntelRowFields) {
  return contactTypeLabel(row);
}

function rowMatchesPriorityFilter(row: IntelRowFields, priorityFilter: string) {
  if (priorityFilter === "All") return true;
  const raw = (row.priority || "").trim().toLowerCase();
  const want = priorityFilter.toLowerCase();
  if (want === "high") return raw === "high" || raw === "urgent" || raw === "critical";
  if (want === "medium") return raw === "medium" || raw === "med" || raw === "mid";
  if (want === "low") return raw === "low";
  return raw === want;
}

function filterIntelRows<T extends IntelRowFields>(rows: T[], filters: IntelListFilters): T[] {
  return rows.filter((row) => {
    if (filters.agent !== "All" && agentDisplayName(row) !== filters.agent) return false;
    if (filters.type !== "All" && rowTypeLabel(row).toLowerCase() !== filters.type.toLowerCase()) {
      return false;
    }
    if (!rowMatchesPriorityFilter(row, filters.priority)) return false;
    return true;
  });
}

function sortIntelRowsWithWait<T extends IntelRowFields>(rows: T[], waitSort: IntelListFilters["waitSort"]): T[] {
  if (waitSort === "asc" || waitSort === "desc") {
    return [...rows].sort((a, b) => {
      const ageA = a.age_seconds ?? 0;
      const ageB = b.age_seconds ?? 0;
      const byWait = waitSort === "asc" ? ageA - ageB : ageB - ageA;
      if (byWait !== 0) return byWait;
      return priorityRank(a.priority) - priorityRank(b.priority);
    });
  }
  return sortIntelRows(rows);
}

function prepareIntelListRows<T extends IntelRowFields>(rows: T[], filters: IntelListFilters): T[] {
  return sortIntelRowsWithWait(filterIntelRows(rows, filters), filters.waitSort);
}

type WaInboxAccount = {
  display_name: string;
  provider?: string;
};

function isCareAccount(a: { provider?: string; display_name?: string } | null | undefined): boolean {
  if (!a) return false;
  return (a.provider || "") === "meta_cloud" || a.display_name === "Care";
}

/** Staff Evolution GO lines only — excludes Care / meta_cloud. */
function isEvolutionStaffAccount(a: WaInboxAccount): boolean {
  if (isCareAccount(a)) return false;
  const provider = (a.provider || "evolution").trim().toLowerCase();
  return provider === "evolution";
}

function evolutionStaffAgentNames(accounts: WaInboxAccount[]): string[] {
  return accounts
    .filter(isEvolutionStaffAccount)
    .map((a) => (a.display_name || "").trim())
    .filter(Boolean)
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
}

function filtersResetKey(filters: IntelListFilters) {
  return `${filters.agent}|${filters.type}|${filters.priority}|${filters.waitSort}`;
}

function intelFiltersActive(filters: IntelListFilters) {
  return (
    filters.agent !== "All" ||
    filters.type !== "All" ||
    filters.priority !== "All" ||
    filters.waitSort !== "default"
  );
}

function cycleWaitSort(current: IntelListFilters["waitSort"]): IntelListFilters["waitSort"] {
  if (current === "default") return "desc";
  if (current === "desc") return "asc";
  return "default";
}

function waitSortHint(waitSort: IntelListFilters["waitSort"]) {
  if (waitSort === "desc") return "Sorted by wait, longest first. Click to sort shortest first.";
  if (waitSort === "asc") return "Sorted by wait, shortest first. Click to use default order.";
  return "Default order. Click to sort by wait, longest first.";
}

function chatThreadId(row: TableRow) {
  const fromHref = threadIdOf(row.href);
  if (fromHref) return fromHref;
  if ((row.href || "").includes("/whatsapp") && row.key) return row.key;
  return "";
}

function contactIcon(type: string) {
  const t = type.toLowerCase();
  if (t === "dealer") return <PackageIcon size={14} />;
  return <SparklesIcon size={14} />;
}

function dealerLabel(row: IntelRowFields, fallback = "—") {
  return (row.dealer_name || row.title || fallback).trim() || fallback;
}

function contactTypeLabel(row: IntelRowFields) {
  const t = (row.contact_type || "").trim();
  if (t) return t;
  return "Lead";
}

function priorityClass(p?: string) {
  const v = (p || "").toLowerCase();
  if (v === "high") return "intel-badge intel-pri-high";
  if (v === "medium" || v === "med") return "intel-badge intel-pri-med";
  if (v === "low") return "intel-badge intel-pri-low";
  return "intel-badge intel-pri-none";
}

function typeClass(type?: string) {
  const t = (type || "").toLowerCase();
  if (t === "dealer") return "intel-badge intel-type-dealer";
  return "intel-badge intel-type-lead";
}

function Badge({ text, className }: { text: string; className: string }) {
  if (!text.trim()) return <span className="intel-dash">—</span>;
  return <span className={className}>{text}</span>;
}

type TableRow = IntelRowFields & {
  key: string;
  href?: string;
  invoice_number?: string;
};

function IntelTable({
  rows,
  copied,
  onCopyInvoice,
  onNavigate,
  onOpenChat,
  filters,
  agentOptions,
  onFiltersChange,
  shown,
  total,
  showFilters,
  theadRef,
  firstRowRef,
}: {
  rows: TableRow[];
  copied: string;
  onCopyInvoice: (n: string) => void;
  onNavigate: (href: string) => void;
  onOpenChat: (row: TableRow) => void;
  filters?: IntelListFilters;
  agentOptions?: string[];
  onFiltersChange?: (next: IntelListFilters) => void;
  shown?: number;
  total?: number;
  showFilters?: boolean;
  theadRef?: Ref<HTMLTableSectionElement>;
  firstRowRef?: Ref<HTMLTableRowElement>;
}) {
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const headerFilters = Boolean(showFilters && filters && onFiltersChange);
  if (rows.length === 0 && !headerFilters) return null;

  function handleRowDoubleClick(row: TableRow) {
    const tid = chatThreadId(row);
    if (tid) {
      onOpenChat(row);
      return;
    }
    const href = row.href?.trim();
    if (href && !href.includes("/whatsapp")) {
      onNavigate(href);
      return;
    }
    if (row.invoice_number) {
      void onCopyInvoice(row.invoice_number);
    }
  }

  function rowHint(row: TableRow) {
    if (chatThreadId(row)) return "Double-click to open chat";
    if (row.href?.trim() && !row.href.includes("/whatsapp")) return "Double-click to open";
    if (row.invoice_number) return copied === row.invoice_number ? "Copied" : "Double-click to copy invoice";
    return "Click to select";
  }

  return (
    <div className="intel-table-wrap">
      <table className="intel-table">
        <thead ref={theadRef}>
          <tr className="intel-thead-row">
            <th className="intel-th-dealer">Dealer</th>
            {headerFilters && filters && onFiltersChange ? (
              <>
                <th className="intel-th-filter">
                  <span className="intel-th-label">Agent</span>
                  <select
                    className="intel-th-select"
                    aria-label="Filter by agent"
                    value={filters.agent}
                    onChange={(e) => onFiltersChange({ ...filters, agent: e.target.value })}
                  >
                    <option value="All">All</option>
                    {(agentOptions || []).map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </select>
                </th>
                <th className="intel-th-filter">
                  <span className="intel-th-label">Type</span>
                  <select
                    className="intel-th-select"
                    aria-label="Filter by type"
                    value={filters.type}
                    onChange={(e) => onFiltersChange({ ...filters, type: e.target.value })}
                  >
                    <option value="All">All</option>
                    <option value="Dealer">Dealer</option>
                    <option value="Lead">Lead</option>
                  </select>
                </th>
                <th className="intel-th-filter">
                  <span className="intel-th-label">Priority</span>
                  <select
                    className="intel-th-select"
                    aria-label="Filter by priority"
                    value={filters.priority}
                    onChange={(e) => onFiltersChange({ ...filters, priority: e.target.value })}
                  >
                    <option value="All">All</option>
                    <option value="High">High</option>
                    <option value="Medium">Medium</option>
                    <option value="Low">Low</option>
                  </select>
                </th>
                <th className="intel-th-sort">
                  <button
                    type="button"
                    className={`intel-wait-sort${filters.waitSort !== "default" ? " active" : ""}`}
                    title={waitSortHint(filters.waitSort)}
                    aria-label={`Wait sort: ${filters.waitSort === "default" ? "default" : filters.waitSort === "asc" ? "ascending" : "descending"}`}
                    onClick={() =>
                      onFiltersChange({ ...filters, waitSort: cycleWaitSort(filters.waitSort) })
                    }
                  >
                    <span className="intel-th-label">Wait</span>
                    <span className="intel-sort-arrows" aria-hidden>
                      <span className={filters.waitSort === "asc" ? "on" : ""}>↑</span>
                      <span className={filters.waitSort === "desc" ? "on" : ""}>↓</span>
                    </span>
                  </button>
                </th>
                <th className="intel-th-summary">
                  <span className="intel-th-label">Summary</span>
                  {typeof shown === "number" ? (
                    <span className="intel-shown-count">
                      {shown}
                      {typeof total === "number" && shown !== total ? ` of ${total}` : ""} shown
                    </span>
                  ) : null}
                </th>
              </>
            ) : (
              <>
                <th>Agent</th>
                <th>Type</th>
                <th>Priority</th>
                <th>Wait</th>
                <th>Summary</th>
              </>
            )}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIdx) => {
            const wait = (row.time_till_label || row.age_label || "").trim();
            const summary = (row.summary || row.subtitle || "").trim();
            const type = contactTypeLabel(row);
            const interactive = Boolean(chatThreadId(row) || row.href?.trim() || row.invoice_number);
            const hint = rowHint(row);
            const dealer = dealerLabel(row);
            const agent = (row.agent_name || "").trim() || "Unassigned";
            return (
              <tr
                key={row.key}
                ref={rowIdx === 0 ? firstRowRef : undefined}
                className={[
                  interactive ? "intel-row-clickable" : "",
                  selectedKey === row.key ? "intel-row-selected" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                tabIndex={interactive ? 0 : undefined}
                title={hint || undefined}
                onClick={() => setSelectedKey(row.key)}
                onDoubleClick={() => {
                  if (interactive) handleRowDoubleClick(row);
                }}
                onKeyDown={(e) => {
                  if (!interactive) return;
                  if (e.key === "Enter") {
                    e.preventDefault();
                    handleRowDoubleClick(row);
                  }
                }}
              >
                <td className="intel-td-dealer">
                  <div className="intel-cell-person">
                    <IntelAvatar name={dealer} photo={row.avatar_url} />
                    <div className="intel-cell-text">
                      <strong>{dealer}</strong>
                      <span className="intel-dealer-phone">{row.phone?.trim() || "—"}</span>
                    </div>
                  </div>
                </td>
                <td className="intel-td-agent">
                  <div className="intel-cell-person intel-cell-agent">
                    <IntelAvatar name={agent} agent />
                    <span>{agent}</span>
                  </div>
                </td>
                <td>
                  <span className={`intel-type-chip ${typeClass(type).replace("intel-badge ", "")}`}>
                    <span className="intel-type-icon" aria-hidden>
                      {contactIcon(type)}
                    </span>
                    {type}
                  </span>
                </td>
                <td>
                  <Badge text={row.priority || ""} className={priorityClass(row.priority)} />
                </td>
                <td className="intel-td-wait">{wait || "—"}</td>
                <td className="intel-td-summary" title={summary}>
                  {summary || "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Pager({
  page,
  pages,
  total,
  pageSize,
  onPage,
  pagerRef,
}: {
  page: number;
  pages: number;
  total: number;
  pageSize: number;
  onPage: (n: number) => void;
  pagerRef?: Ref<HTMLDivElement>;
}) {
  if (total <= pageSize) return null;
  const nums = Array.from({ length: pages }, (_, i) => i);
  return (
    <div className="intel-pager" ref={pagerRef}>
      <span>
        {total} · {page + 1}/{pages}
      </span>
      <button type="button" disabled={page <= 0} onClick={() => onPage(page - 1)}>
        Prev
      </button>
      {nums.map((n) => (
        <button key={n} type="button" className={n === page ? "active" : ""} onClick={() => onPage(n)}>
          {n + 1}
        </button>
      ))}
      <button type="button" disabled={page >= pages - 1} onClick={() => onPage(page + 1)}>
        Next
      </button>
    </div>
  );
}

function usePaged(rows: TableRow[], resetKey: string, pageSize: number) {
  const [page, setPage] = useState(0);
  useEffect(() => {
    setPage(0);
  }, [resetKey, pageSize]);
  const pages = Math.max(1, Math.ceil(rows.length / pageSize));
  const safe = Math.min(page, pages - 1);
  const slice = rows.slice(safe * pageSize, safe * pageSize + pageSize);
  return { slice, page: safe, pages, setPage, total: rows.length };
}

export default function Intelligence() {
  const navigate = useNavigate();
  const [tab, setTab] = useState<(typeof TABS)[number]>("Enquiry");
  const [err, setErr] = useState("");
  const [chat, setChat] = useState<{ id: string; name: string; phone: string; photo?: string } | null>(null);
  const [funnelLoading, setFunnelLoading] = useState(false);
  const [commitments, setCommitments] = useState<Commitment[] | null>(null);
  const [showLow, setShowLow] = useState(false);
  const [funnel, setFunnel] = useState<FunnelStage[] | null>(null);
  const [service, setService] = useState<ServiceRow[] | null>(null);
  const [stamping, setStamping] = useState<ServiceRow[] | null>(null);
  const [copied, setCopied] = useState("");
  const [listFilters, setListFilters] = useState<IntelListFilters>(DEFAULT_INTEL_FILTERS);
  const [evolutionAgentNames, setEvolutionAgentNames] = useState<string[] | null>(null);

  const stage = useMemo(() => {
    if (!funnel) return null;
    const want = FUNNEL_STAGE_KEY[tab];
    if (!want) return null;
    return funnel.find((s) => s.key === want) ?? null;
  }, [funnel, tab]);

  useEffect(() => {
    api<{ items: WaInboxAccount[] }>("/api/whatsapp/inbox/accounts?lite=true")
      .then((res) => setEvolutionAgentNames(evolutionStaffAgentNames(res.items || [])))
      .catch(() => setEvolutionAgentNames([]));
  }, []);

  useEffect(() => {
    if (evolutionAgentNames === null) return;
    const picked = listFilters.agent;
    if (
      picked !== "All" &&
      picked !== "Unassigned" &&
      !evolutionAgentNames.includes(picked)
    ) {
      setListFilters((f) => ({ ...f, agent: "All" }));
    }
  }, [evolutionAgentNames, listFilters.agent]);

  const loadFunnelStageItems = useCallback((stageKey: string) => {
    setFunnelLoading(true);
    return apiWithTimeout<{ stages: FunnelStage[] }>(`/api/intel/funnel?stage=${encodeURIComponent(stageKey)}`, 45_000)
      .then((res) => {
        setFunnel((prev) => mergeFunnelStages(prev, res.stages || [], new Set([stageKey])));
      })
      .catch((e) => {
        setErr(e instanceof Error ? e.message : "Could not load funnel list");
      })
      .finally(() => setFunnelLoading(false));
  }, []);

  const reloadIntelLists = useCallback(() => {
    setFunnelLoading(true);
    setErr("");
    // Keep prior tiles/lists visible while refreshing (avoid blank flash + double init).
    setCommitments(null);
    setService(null);
    setStamping(null);

    // Counts first — tiles fill without waiting for row payloads.
    apiWithTimeout<{ stages: FunnelStage[]; items_omitted?: boolean }>("/api/intel/funnel?items=false", 45_000)
      .then((res) => {
        setFunnel((prev) => mergeFunnelStages(prev, res.stages || []));
      })
      .catch((e) => {
        setFunnel((prev) => prev ?? []);
        setErr(e instanceof Error ? e.message : "Could not load funnel");
      });

    // Active funnel tab list in parallel with counts.
    const activeStage = FUNNEL_STAGE_KEY[tab] || "enquiry";
    void loadFunnelStageItems(activeStage);

    api<{ items: Commitment[] }>("/api/intel/commitments?limit=300")
      .then((res) => setCommitments(res.items))
      .catch((e) => setErr(e instanceof Error ? e.message : "Commitments failed"));
    api<{ items: ServiceRow[] }>("/api/intel/service?limit=300")
      .then((res) => setService(res.items))
      .catch((e) => setErr(e instanceof Error ? e.message : "Service queue failed"));
    api<{ items: ServiceRow[] }>("/api/intel/stamping?limit=300")
      .then((res) => setStamping(res.items))
      .catch((e) => setErr(e instanceof Error ? e.message : "Stamping queue failed"));
  }, [loadFunnelStageItems, tab]);

  useEffect(() => {
    if (funnel !== null) return;
    reloadIntelLists();
  }, [funnel, reloadIntelLists]);

  // Lazy-load funnel stage rows when switching tiles.
  useEffect(() => {
    const want = FUNNEL_STAGE_KEY[tab];
    if (!want || funnel === null) return;
    if (funnelStageHasItems(funnel, want) || funnelLoading) return;
    void loadFunnelStageItems(want);
  }, [tab, funnel, funnelLoading, loadFunnelStageItems]);

  useEffect(() => {
    if (commitments === null || service === null || stamping === null) {
      if (commitments === null) {
        api<{ items: Commitment[] }>("/api/intel/commitments?limit=300")
          .then((res) => setCommitments(res.items))
          .catch((e) => setErr(e instanceof Error ? e.message : "Commitments failed"));
      }
      if (service === null) {
        api<{ items: ServiceRow[] }>("/api/intel/service?limit=300")
          .then((res) => setService(res.items))
          .catch((e) => setErr(e instanceof Error ? e.message : "Service queue failed"));
      }
      if (stamping === null) {
        api<{ items: ServiceRow[] }>("/api/intel/stamping?limit=300")
          .then((res) => setStamping(res.items))
          .catch((e) => setErr(e instanceof Error ? e.message : "Stamping queue failed"));
      }
    }
  }, [commitments, service, stamping]);

  const disabled = err.toLowerCase().includes("not enabled");
  const visibleCommitments = useMemo(
    () => (commitments || []).filter((row) => showLow || row.high_lane),
    [commitments, showLow],
  );

  const tabCounts = useMemo((): Record<(typeof TABS)[number], { n: number | null; pending: boolean }> => {
    const commitmentSource = commitments
      ? (commitments || []).filter((row) => showLow || row.high_lane)
      : null;
    const funnelPending = funnel === null;
    return {
      Enquiry: {
        n: funnelStageCount(funnel, "enquiry"),
        pending: funnelPending,
      },
      "Sales order": {
        n: funnelStageCount(funnel, "sales_order"),
        pending: funnelPending,
      },
      "Waiting": {
        n: funnelStageCount(funnel, "invoice_open"),
        pending: funnelPending,
      },
      Paid: {
        n: funnelStageCount(funnel, "invoice_settled"),
        pending: funnelPending,
      },
      Stamping: { n: stamping ? stamping.length : null, pending: stamping === null },
      "Service (unresolved)": { n: service ? service.length : null, pending: service === null },
      Commitments: {
        n: commitmentSource ? commitmentSource.length : null,
        pending: commitments === null,
      },
    };
  }, [funnel, stamping, service, commitments, showLow]);

  async function copyInvoice(number: string) {
    try {
      await navigator.clipboard.writeText(number);
      setCopied(number);
    } catch {
      setCopied("");
    }
  }

  const funnelRows: TableRow[] = useMemo(
    () =>
      (stage?.items ?? []).map((row) => ({
        ...row,
        key: row.id,
      })),
    [stage],
  );

  const serviceRows: TableRow[] = useMemo(
    () =>
      (service ?? []).map((row) => ({
        ...row,
        key: row.conversation_id,
        dealer_name: row.dealer_name || row.customer_name,
        priority: row.priority || (row.severity === "high" ? "High" : "Medium"),
        contact_type: row.contact_type || (row.href?.includes("/dealers") ? "Dealer" : "Lead"),
        summary: row.summary || (row.tags.length ? row.tags.join(" · ") : "Unresolved service"),
      })),
    [service],
  );

  const stampingRows: TableRow[] = useMemo(
    () =>
      (stamping ?? []).map((row) => ({
        ...row,
        key: row.conversation_id,
        dealer_name: row.dealer_name || row.customer_name,
        priority: row.priority || (row.severity === "high" ? "High" : "Medium"),
        contact_type: row.contact_type || (row.href?.includes("/dealers") ? "Dealer" : "Lead"),
        summary: row.summary || (row.tags.length ? row.tags.join(" · ") : "OV / stamping"),
      })),
    [stamping],
  );

  const commitmentRows: TableRow[] = useMemo(
    () =>
      visibleCommitments.map((row) => ({
        ...row,
        key: row.id,
        summary: row.summary || row.promise_text,
        time_till_label: row.due_ist ? `Due ${row.due_ist}` : row.time_till_label,
      })),
    [visibleCommitments],
  );

  const activeSourceRows: TableRow[] = useMemo(() => {
    if (tab === "Stamping") return stampingRows;
    if (tab === "Service (unresolved)") return serviceRows;
    if (tab === "Commitments") return commitmentRows;
    if (FUNNEL_TABS.has(tab)) return funnelRows;
    return [];
  }, [tab, funnelRows, serviceRows, stampingRows, commitmentRows]);

  const agentOptions = useMemo(() => {
    const names = [...(evolutionAgentNames ?? [])];
    if (activeSourceRows.some((row) => !((row.agent_name || "").trim()))) {
      if (!names.includes("Unassigned")) names.push("Unassigned");
    }
    return names.sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  }, [evolutionAgentNames, activeSourceRows]);

  const filteredActiveRows = useMemo(
    () => prepareIntelListRows(activeSourceRows, listFilters),
    [activeSourceRows, listFilters],
  );

  const activeFunnelKey = FUNNEL_STAGE_KEY[tab];
  const listStillLoading =
    (FUNNEL_TABS.has(tab) && (funnelLoading || !funnelStageHasItems(funnel, activeFunnelKey))) ||
    (tab === "Stamping" && stamping === null) ||
    (tab === "Service (unresolved)" && service === null) ||
    (tab === "Commitments" && commitments === null);

  const listPagerResetKey = `${tab}:${showLow}:${filtersResetKey(listFilters)}`;
  const pageSizeMeasureKey = `${listPagerResetKey}:${!!err}`;
  const { pageSize, tilesRef, pagerRef, toolbarRef, theadRef, rowProbeRef, recompute } =
    useIntelPageSize(pageSizeMeasureKey);
  const pagedList = usePaged(filteredActiveRows, listPagerResetKey, pageSize);

  useEffect(() => {
    recompute();
  }, [recompute, pagedList.slice.length, listStillLoading]);

  function openChat(row: TableRow) {
    const id = chatThreadId(row);
    if (!id) return;
    setChat({ id, name: dealerLabel(row), phone: row.phone || "", photo: row.avatar_url });
  }

  function listTable(slice: TableRow[], withHeaderFilters: boolean) {
    return (
      <IntelTable
        rows={slice}
        copied={copied}
        onCopyInvoice={(n) => void copyInvoice(n)}
        onNavigate={(href) => navigate(href)}
        onOpenChat={openChat}
        showFilters={withHeaderFilters}
        filters={listFilters}
        agentOptions={agentOptions}
        onFiltersChange={setListFilters}
        shown={filteredActiveRows.length}
        total={activeSourceRows.length}
        theadRef={theadRef}
        firstRowRef={rowProbeRef}
      />
    );
  }

  function intelListChrome(emptyCopy: string, toolbarExtra?: ReactNode) {
    const sourceCount = activeSourceRows.length;
    const showFilters = sourceCount > 0 || intelFiltersActive(listFilters);
    return (
      <div className="intel-list-shell">
        {toolbarExtra ? <div ref={toolbarRef}>{toolbarExtra}</div> : null}
        {!listStillLoading && sourceCount === 0 ? (
          <p className="intel-muted intel-empty-msg">{emptyCopy}</p>
        ) : null}
        {sourceCount > 0 && filteredActiveRows.length === 0 ? (
          <>
            <p className="intel-muted intel-empty-msg">No rows match these filters.</p>
            {showFilters ? (
              <div className="intel-table-area">{listTable([], true)}</div>
            ) : null}
          </>
        ) : null}
        {filteredActiveRows.length > 0 ? (
          <div className="intel-table-area">{listTable(pagedList.slice, showFilters)}</div>
        ) : null}
        {filteredActiveRows.length > 0 ? (
          <Pager
            page={pagedList.page}
            pages={pagedList.pages}
            total={pagedList.total}
            pageSize={pageSize}
            pagerRef={pagerRef}
            onPage={pagedList.setPage}
          />
        ) : (
          <div className="intel-pager intel-pager-measure" ref={pagerRef} aria-hidden="true" />
        )}
      </div>
    );
  }

  function funnelPanel() {
    return (
      <div className="panel intel-panel intel-panel-bleed">
        {funnelLoading ? <p className="intel-muted intel-empty-msg">Loading…</p> : null}
        {!funnelLoading ? intelListChrome("Nothing in this stage right now.") : null}
      </div>
    );
  }

  return (
    <div className="intel-page">
      {disabled ? (
        <div className="panel intel-empty">
          <h2>Intelligence is not enabled</h2>
          <p>An admin turns this on in settings (`intel_enabled`). Inbox and Reply Stats stay as they are.</p>
        </div>
      ) : (
        <>
          <div className="intel-stage-tiles" ref={tilesRef} role="tablist" aria-label="Intelligence stages">
            {TABS.map((name) => {
              const meta = INTEL_TAB_TILE[name];
              const { n, pending } = tabCounts[name];
              const active = tab === name;
              return (
                <button
                  key={name}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  className={`intel-stage-tile tone-${meta.tone}${active ? " is-active" : ""}`}
                  onClick={() => setTab(name)}
                >
                  <div className={`intel-stage-tile-ico tone-${meta.tone}`}>{meta.icon}</div>
                  <div className="intel-stage-tile-body">
                    <p className="intel-stage-tile-kicker">{meta.label}</p>
                    <span className="intel-stage-tile-num">{fmtIntelTileCount(n, pending)}</span>
                  </div>
                </button>
              );
            })}
          </div>

          {err && !disabled ? <div className="err">{err}</div> : null}

          <div className="intel-body">
            {tab === "Enquiry" && funnelPanel()}
            {tab === "Sales order" && funnelPanel()}
            {tab === "Waiting" && funnelPanel()}
            {tab === "Paid" && funnelPanel()}

            {tab === "Stamping" && (
              <div className="panel intel-panel intel-panel-bleed">
                {stamping === null ? null : intelListChrome("No open stamping certificate requests.")}
              </div>
            )}

            {tab === "Service (unresolved)" && (
              <div className="panel intel-panel intel-panel-bleed">
                {service === null ? null : intelListChrome("No dealers waiting for service to finish.")}
              </div>
            )}

            {tab === "Commitments" && (
              <div className="panel intel-panel intel-panel-bleed">
                {commitments === null
                  ? null
                  : intelListChrome(
                      "No open agent commitments. A promise the agent made shows up after the next message.",
                      <div className="intel-toolbar">
                        <label className="intel-check">
                          <input type="checkbox" checked={showLow} onChange={(e) => setShowLow(e.target.checked)} />
                          Show low confidence
                        </label>
                      </div>,
                    )}
              </div>
            )}
          </div>
        </>
      )}
      {chat ? (
        <ChatPreviewModal
          threadId={chat.id}
          name={chat.name}
          phone={chat.phone}
          photo={chat.photo}
          currentStage={TAB_TO_MOVE_STAGE[tab]}
          onClose={() => setChat(null)}
          onStageMoved={() => {
            setChat(null);
            reloadIntelLists();
          }}
        />
      ) : null}
    </div>
  );
}