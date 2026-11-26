import { useCallback, useEffect, useMemo, useState } from "react";

/** Default Dealer Finder table rows before in-session "More" expand. */
export const DEALER_FINDER_TABLE_PREVIEW_ROWS = 100;
import { api } from "../api/client";

export type ReportColKind = "text" | "inr" | "qty" | "int";

export type ReportColumn = {
  key: string;
  label: string;
  align?: "left" | "right" | "center";
  kind?: ReportColKind;
};

export type ReportChip = {
  id: string;
  label: string;
  icon?: "bars" | "coins" | "hash";
};

export type DealerFinderListExclusions = {
  exclude_districts?: string[];
  exclude_cities?: string[];
  exclude_keywords?: string[];
};

export type DealerFinderImportMeta = {
  publicId: string;
  jobId?: number;
  /** Phone-eligible NEW count shown on this card. */
  importCount: number;
  placeRowIds?: number[];
  listExclusions?: DealerFinderListExclusions;
  /** When set, POST import uses explicit place_row_ids (matches card filters). */
  usePlaceRowIds?: boolean;
  /** False while statewide scan still Running — hide Import until complete. */
  importReady?: boolean;
};

export type BusinessReportData = {
  title: string;
  subtitle: string;
  chips: ReportChip[];
  columns: ReportColumn[];
  rows: Record<string, unknown>[];
  /** Footer totals keyed like rows (optional columns skipped if missing). */
  totals?: Record<string, unknown> | null;
  footInfo: string;
  /** Numeric field used for the bar chart. */
  chartKey: string;
  chartLabelKey: string;
  exportBasename: string;
  /** One-line blurb shown above the card (never a numbered list). */
  shortText: string;
  sortBy: "amount" | "quantity";
  /** Contact lists have no meaningful bar chart. */
  hideChart?: boolean;
  /** Dealer Finder results card — footer shows Import to Leads (not Export/Copy). */
  dealerFinderImport?: DealerFinderImportMeta;
};

type ToolOutcome = {
  ok?: boolean;
  tool?: string;
  result?: unknown;
  error?: string;
};

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

function titleCase(q: string): string {
  return (q || "")
    .trim()
    .split(/\s+/)
    .map((w) => (w ? w.charAt(0).toUpperCase() + w.slice(1).toLowerCase() : w))
    .join(" ");
}

export function formatInr(n: number | undefined | null): string {
  if (n == null || Number.isNaN(Number(n))) return "₹—";
  return `₹${Number(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function formatQty(n: number | undefined | null): string {
  if (n == null || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  const rounded = Math.abs(v - Math.round(v)) < 1e-9 ? Math.round(v) : Number(v.toFixed(3));
  return `${rounded.toLocaleString("en-IN")} pcs`;
}

function formatInt(n: number | undefined | null): string {
  if (n == null || Number.isNaN(Number(n))) return "—";
  return Math.round(Number(n)).toLocaleString("en-IN");
}

function formatCell(kind: ReportColKind | undefined, value: unknown): string {
  if (kind === "inr") return formatInr(typeof value === "number" ? value : Number(value));
  if (kind === "qty") return formatQty(typeof value === "number" ? value : Number(value));
  if (kind === "int") return formatInt(typeof value === "number" ? value : Number(value));
  if (value == null || value === "") return "—";
  return String(value);
}

function periodLabel(period: unknown, fallback = "FY YTD"): string {
  const p = asRecord(period);
  if (!p) return fallback;
  const label = String(p.label || "").trim();
  if (label) return label;
  const from = String(p.from || "").trim();
  const to = String(p.to || "").trim();
  if (from && to) return `${from} → ${to}`;
  return fallback;
}

function taxNote(currencyNote: unknown): string {
  const s = String(currencyNote || "").toLowerCase();
  if (s.includes("exclusive")) return "Exclusive";
  return "Exclusive";
}

function dealerDisplayName(row: Record<string, unknown>): string {
  return (
    String(row.customer_name || row.company || row.name || row.dealer || "").trim() || "—"
  );
}

function normalizeInMobile(value: string): string {
  const digits = value.replace(/\D/g, "");
  if (!digits) return "";
  let d = digits;
  if (d.startsWith("91") && d.length > 10) d = d.slice(2);
  if (d.startsWith("0") && d.length === 11) d = d.slice(1);
  if (d.length >= 10) return d.slice(-10);
  return "";
}

type IndianPhoneKind = "empty" | "mobile" | "landline" | "invalid";

function classifyIndianPhone(value: string): IndianPhoneKind {
  const digits = value.replace(/\D/g, "");
  if (!digits) return "empty";
  let national = digits;
  if (national.startsWith("91") && national.length > 10) national = national.slice(2);
  if (national.length === 11 && national[0] === "0") {
    return national[1] && "6789".includes(national[1]) ? "mobile" : "landline";
  }
  if (national.length === 10) {
    return national[0] && "6789".includes(national[0]) ? "mobile" : "landline";
  }
  if (national.length < 10) {
    return national.length >= 6 ? "landline" : "invalid";
  }
  return "landline";
}

function isIndianMobile(value: string): boolean {
  return classifyIndianPhone(value) === "mobile";
}

function exclusionsActive(excl: DealerFinderListExclusions | undefined): boolean {
  if (!excl) return false;
  return Boolean(
    (excl.exclude_districts?.length ?? 0) > 0 ||
      (excl.exclude_cities?.length ?? 0) > 0 ||
      (excl.exclude_keywords?.length ?? 0) > 0,
  );
}

function parseListExclusions(raw: unknown): DealerFinderListExclusions | undefined {
  const excl = asRecord(raw);
  if (!excl) return undefined;
  const out: DealerFinderListExclusions = {
    exclude_districts: Array.isArray(excl.exclude_districts)
      ? excl.exclude_districts.map((x) => String(x).trim()).filter(Boolean)
      : undefined,
    exclude_cities: Array.isArray(excl.exclude_cities)
      ? excl.exclude_cities.map((x) => String(x).trim()).filter(Boolean)
      : undefined,
    exclude_keywords: Array.isArray(excl.exclude_keywords)
      ? excl.exclude_keywords.map((x) => String(x).trim()).filter(Boolean)
      : undefined,
  };
  return exclusionsActive(out) ? out : undefined;
}

function dealerFinderRowScore(row: Record<string, unknown>): number {
  let score = 0;
  const website = String(row.website || "").trim();
  if (website && website !== "—") score += 8;
  const name = String(row.label || "").trim();
  if (name) score += Math.min(name.length, 160);
  return score;
}

function dedupeDealerFinderRows(rows: Record<string, unknown>[]): {
  rows: Record<string, unknown>[];
  removed: number;
} {
  const byPhone = new Map<string, Record<string, unknown>>();
  for (const row of rows) {
    const phone10 = normalizeInMobile(String(row.phone || ""));
    if (!phone10) continue;
    const prev = byPhone.get(phone10);
    if (!prev || dealerFinderRowScore(row) > dealerFinderRowScore(prev)) {
      byPhone.set(phone10, row);
    }
  }
  const deduped = [...byPhone.values()].sort((a, b) =>
    String(a.label).localeCompare(String(b.label), undefined, { sensitivity: "base" }),
  );
  return { rows: deduped, removed: Math.max(0, rows.length - deduped.length) };
}

function sortRows(
  rows: Record<string, unknown>[],
  sortBy: "amount" | "quantity",
): Record<string, unknown>[] {
  return [...rows].sort((a, b) => {
    if (sortBy === "quantity") {
      return (
        Number(b.quantity ?? 0) - Number(a.quantity ?? 0) ||
        Number(b.total ?? 0) - Number(a.total ?? 0)
      );
    }
    return (
      Number(b.total ?? 0) - Number(a.total ?? 0) ||
      Number(b.quantity ?? b.invoice_count ?? 0) - Number(a.quantity ?? a.invoice_count ?? 0)
    );
  });
}

/** Product × dealer (qty DESC default, amount DESC when sort_by=amount). */
function fromProductByDealer(raw: Record<string, unknown>): BusinessReportData | null {
  const nested = asRecord(raw.sales);
  const src = nested && Array.isArray(nested.dealers) ? nested : raw;
  const dealersRaw = src.dealers;
  if (!Array.isArray(dealersRaw) || dealersRaw.length === 0) return null;
  if (src.resolved === false) return null;

  const sortBy =
    String(src.sort_by || "quantity").toLowerCase() === "amount" ? "amount" : "quantity";

  const rows = dealersRaw
    .map((d) => {
      const row = asRecord(d);
      if (!row) return null;
      return {
        customer_id: String(row.customer_id || ""),
        label: dealerDisplayName(row),
        quantity: Number(row.quantity ?? 0),
        total: Number(row.total ?? 0),
        line_count: Number(row.line_count ?? 0),
      };
    })
    .filter(Boolean) as Record<string, unknown>[];

  if (!rows.length) return null;
  const sorted = sortRows(rows, sortBy);
  const product = titleCase(String(src.product_query || raw.product_query || "product"));
  const period = periodLabel(src.period);
  const qty =
    typeof src.quantity === "number"
      ? src.quantity
      : sorted.reduce((s, d) => s + Number(d.quantity || 0), 0);
  const total =
    typeof src.total === "number"
      ? src.total
      : sorted.reduce((s, d) => s + Number(d.total || 0), 0);

  return {
    title: `Sales by Dealer – ${product}`,
    subtitle: `Financial Year: ${period} | Tax: ${taxNote(src.currency_note)}`,
    chips: [
      { id: "qty", label: `Total: ${formatQty(qty)}`, icon: "bars" },
      { id: "sales", label: `Sales: ${formatInr(total)}`, icon: "coins" },
    ],
    columns: [
      { key: "label", label: "Dealer", align: "left", kind: "text" },
      { key: "quantity", label: "Quantity (pcs)", align: "right", kind: "qty" },
      { key: "total", label: "Sales Amount (₹)", align: "right", kind: "inr" },
    ],
    rows: sorted,
    totals: { label: "Total", quantity: qty, total },
    footInfo: `Showing ${sorted.length} dealers | ${product} | ${period}`,
    chartKey: sortBy === "amount" ? "total" : "quantity",
    chartLabelKey: "label",
    exportBasename: `sales-by-dealer-${product.replace(/\s+/g, "-").toLowerCase()}`,
    shortText: `${product}: ${formatQty(qty)} / ${formatInr(total)} across ${sorted.length} dealers (${period}, tax-exclusive).`,
    sortBy,
  };
}

/** Top dealers by ₹ (Amount DESC) from query_business_data zoho_sales×dealer. */
function fromTopDealers(raw: Record<string, unknown>): BusinessReportData | null {
  const items = raw.items;
  if (!Array.isArray(items) || items.length === 0) return null;
  const groupBy = String(raw.group_by || "dealer").toLowerCase();
  if (groupBy && groupBy !== "dealer" && groupBy !== "none") return null;

  const sample = asRecord(items[0]);
  if (!sample) return null;
  // Must look like dealer ranking (has total + name/company)
  const hasDealer =
    "customer_id" in sample ||
    "dealer_id" in sample ||
    "company" in sample ||
    ("name" in sample && ("total" in sample || "invoice_count" in sample));
  if (!hasDealer || !("total" in sample)) return null;
  // Avoid product rows (quantity-heavy invoice_lines already handled elsewhere)
  if ("quantity" in sample && !("invoice_count" in sample) && !("city" in sample)) {
    return null;
  }

  const state = String(raw.state || raw.filter_state || "").trim();
  const city = String(raw.city || raw.filter_city || "").trim();
  const scope = state || city || String(raw.region || "").trim();
  const period = periodLabel(raw.period, "Selected period");

  const rows = items
    .map((it) => {
      const row = asRecord(it);
      if (!row) return null;
      return {
        customer_id: String(row.customer_id || row.dealer_id || ""),
        label: dealerDisplayName(row),
        city: String(row.city || "").trim() || "—",
        state: String(row.state || "").trim() || "—",
        invoice_count: Number(row.invoice_count ?? 0),
        total: Number(row.total ?? 0),
      };
    })
    .filter(Boolean) as Record<string, unknown>[];

  if (!rows.length) return null;
  const sorted = sortRows(rows, "amount");
  const total = sorted.reduce((s, d) => s + Number(d.total || 0), 0);
  const invoices = sorted.reduce((s, d) => s + Number(d.invoice_count || 0), 0);
  const title = scope ? `Top Dealers – ${titleCase(scope)}` : "Top Dealers";

  return {
    title,
    subtitle: `Period: ${period} | Tax: ${taxNote(raw.currency_note)} | Ranked by ₹ DESC`,
    chips: [
      { id: "count", label: `Dealers: ${sorted.length}`, icon: "hash" },
      { id: "sales", label: `Sales: ${formatInr(total)}`, icon: "coins" },
    ],
    columns: [
      { key: "label", label: "Dealer", align: "left", kind: "text" },
      { key: "city", label: "City", align: "left", kind: "text" },
      { key: "invoice_count", label: "Invoices", align: "right", kind: "int" },
      { key: "total", label: "Sales Amount (₹)", align: "right", kind: "inr" },
    ],
    rows: sorted,
    totals: { label: "Total", city: "", invoice_count: invoices, total },
    footInfo: `Showing ${sorted.length} dealers${scope ? ` | ${titleCase(scope)}` : ""} | ${period}`,
    chartKey: "total",
    chartLabelKey: "label",
    exportBasename: `top-dealers${scope ? `-${scope.replace(/\s+/g, "-").toLowerCase()}` : ""}`,
    shortText: `${title}: ${formatInr(total)} across ${sorted.length} dealers (${period}, tax-exclusive).`,
    sortBy: "amount",
  };
}

/** City/state sales breakdown tables. */
function fromGeoSales(raw: Record<string, unknown>): BusinessReportData | null {
  const items = raw.items;
  if (!Array.isArray(items) || items.length === 0) return null;
  const groupBy = String(raw.group_by || "").toLowerCase();
  if (groupBy !== "city" && groupBy !== "state") return null;

  const period = periodLabel(raw.period);
  const rows = items
    .map((it) => {
      const row = asRecord(it);
      if (!row) return null;
      const label = String(row[groupBy] || row.label || "").trim() || "—";
      return {
        label,
        invoice_count: Number(row.invoice_count ?? 0),
        dealer_count: Number(row.dealer_count ?? 0),
        total: Number(row.total ?? 0),
      };
    })
    .filter(Boolean) as Record<string, unknown>[];
  if (!rows.length) return null;
  const sorted = sortRows(rows, "amount");
  const total = sorted.reduce((s, d) => s + Number(d.total || 0), 0);
  const geoLabel = groupBy === "city" ? "City" : "State";

  return {
    title: `Sales by ${geoLabel}`,
    subtitle: `Period: ${period} | Tax: ${taxNote(raw.currency_note)} | Ranked by ₹ DESC`,
    chips: [
      { id: "count", label: `${geoLabel}s: ${sorted.length}`, icon: "hash" },
      { id: "sales", label: `Sales: ${formatInr(total)}`, icon: "coins" },
    ],
    columns: [
      { key: "label", label: geoLabel, align: "left", kind: "text" },
      { key: "dealer_count", label: "Dealers", align: "right", kind: "int" },
      { key: "invoice_count", label: "Invoices", align: "right", kind: "int" },
      { key: "total", label: "Sales Amount (₹)", align: "right", kind: "inr" },
    ],
    rows: sorted,
    totals: {
      label: "Total",
      dealer_count: sorted.reduce((s, d) => s + Number(d.dealer_count || 0), 0),
      invoice_count: sorted.reduce((s, d) => s + Number(d.invoice_count || 0), 0),
      total,
    },
    footInfo: `Showing ${sorted.length} ${groupBy === "city" ? "cities" : "states"} | ${period}`,
    chartKey: "total",
    chartLabelKey: "label",
    exportBasename: `sales-by-${groupBy}`,
    shortText: `Sales by ${geoLabel.toLowerCase()}: ${formatInr(total)} across ${sorted.length} ${groupBy === "city" ? "cities" : "states"} (${period}, tax-exclusive).`,
    sortBy: "amount",
  };
}

function fromRegionSales(raw: Record<string, unknown>): BusinessReportData | null {
  if (!raw.resolved) return null;
  const region = String(raw.region || raw.query || "Region").trim();
  const scope = String(raw.scope || "region");
  const period = periodLabel(raw.period);
  const total = Number(raw.total ?? 0);
  const invoices = Number(raw.invoice_count ?? 0);
  const dealers = Number(raw.dealer_count ?? 0);
  const cities = Array.isArray(raw.matched_cities) ? raw.matched_cities : [];

  const rows = cities
    .map((c) => {
      const row = asRecord(c);
      if (!row) return null;
      return {
        label: String(row.city || "").trim() || "—",
        dealer_count: Number(row.dealer_count ?? 0),
      };
    })
    .filter(Boolean) as Record<string, unknown>[];

  const title = `${titleCase(scope)} Sales – ${region}`;
  return {
    title,
    subtitle: `Period: ${period} | Tax: ${taxNote(raw.currency_note)}`,
    chips: [
      { id: "dealers", label: `Dealers: ${formatInt(dealers)}`, icon: "hash" },
      { id: "sales", label: `Sales: ${formatInr(total)}`, icon: "coins" },
    ],
    columns: rows.length
      ? [
          { key: "label", label: "City / District", align: "left", kind: "text" },
          { key: "dealer_count", label: "Dealers", align: "right", kind: "int" },
        ]
      : [
          { key: "label", label: "Metric", align: "left", kind: "text" },
          { key: "value", label: "Value", align: "right", kind: "text" },
        ],
    rows: rows.length
      ? rows
      : [
          { label: "Invoices", value: formatInt(invoices) },
          { label: "Dealers", value: formatInt(dealers) },
          { label: "Sales", value: formatInr(total) },
        ],
    totals: rows.length
      ? { label: "Total sales", dealer_count: dealers, value: formatInr(total) }
      : null,
    footInfo: `${titleCase(scope)} | ${region} | ${period} | ${formatInt(invoices)} invoices`,
    chartKey: rows.length ? "dealer_count" : "dealer_count",
    chartLabelKey: "label",
    exportBasename: `region-sales-${region.replace(/\s+/g, "-").toLowerCase()}`,
    shortText: `${title}: ${formatInr(total)} across ${formatInt(invoices)} invoices (${formatInt(dealers)} dealers, ${period}, tax-exclusive).`,
    sortBy: "amount",
  };
}

function fromDealerSales(raw: Record<string, unknown>): BusinessReportData | null {
  if (!raw.resolved) return null;
  const dealer = asRecord(raw.dealer) || {};
  const name = dealerDisplayName(dealer);
  const period = periodLabel(raw.period);
  const total = Number(raw.total ?? 0);
  const invoices = Number(raw.invoice_count ?? 0);
  const city = String(dealer.city || "").trim() || "—";

  return {
    title: `Dealer Sales – ${name}`,
    subtitle: `Period: ${period} | Tax: ${taxNote(raw.currency_note)}`,
    chips: [
      { id: "inv", label: `Invoices: ${formatInt(invoices)}`, icon: "hash" },
      { id: "sales", label: `Sales: ${formatInr(total)}`, icon: "coins" },
    ],
    columns: [
      { key: "label", label: "Dealer", align: "left", kind: "text" },
      { key: "city", label: "City", align: "left", kind: "text" },
      { key: "invoice_count", label: "Invoices", align: "right", kind: "int" },
      { key: "total", label: "Sales Amount (₹)", align: "right", kind: "inr" },
    ],
    rows: [{ label: name, city, invoice_count: invoices, total }],
    totals: { label: "Total", city: "", invoice_count: invoices, total },
    footInfo: `${name} | ${period}`,
    chartKey: "total",
    chartLabelKey: "label",
    exportBasename: `dealer-sales-${name.replace(/\s+/g, "-").toLowerCase()}`,
    shortText: `Sales for ${name}: ${formatInr(total)} across ${formatInt(invoices)} invoices (${period}, tax-exclusive).`,
    sortBy: "amount",
  };
}

function leadDisplayName(row: Record<string, unknown>): string {
  const business = String(row.business_name || "").trim();
  if (business) return business;
  return dealerDisplayName(row);
}

/** Full CRM Leads contact list (Business Name, City, State, Phone) — distinct from dealers. */
function fromLeadContacts(raw: Record<string, unknown>): BusinessReportData | null {
  const items = raw.items;
  if (!Array.isArray(items) || items.length === 0) return null;
  const sample = asRecord(items[0]);
  if (!sample) return null;
  const looksLead =
    String(raw.report_kind || "").toLowerCase() === "lead_contacts" ||
    String(raw.dataset || "").toLowerCase() === "leads" ||
    String(raw.entity || "").toLowerCase() === "leads" ||
    "lead_id" in sample;
  if (!looksLead) return null;
  if ("total" in sample && "invoice_count" in sample && !("phone" in sample)) return null;

  const state = String(raw.state || raw.filter_state || "").trim();
  const city = String(raw.city || raw.filter_city || "").trim();
  const scope = String(raw.scope || state || city || "").trim();

  const rows = items
    .map((it) => {
      const row = asRecord(it);
      if (!row) return null;
      const phone = String(row.phone || "").trim();
      return {
        label: leadDisplayName(row),
        city: String(row.city || row.district || "").trim() || "—",
        state: String(row.state || "").trim() || "—",
        phone: phone || "—",
        has_phone: phone ? 1 : 0,
      };
    })
    .filter(Boolean) as Record<string, unknown>[];

  if (!rows.length) return null;
  rows.sort((a, b) => String(a.label).localeCompare(String(b.label), undefined, { sensitivity: "base" }));
  const withPhone =
    typeof raw.with_phone === "number"
      ? Number(raw.with_phone)
      : rows.reduce((s, r) => s + Number(r.has_phone || 0), 0);
  const title = scope
    ? `CRM Leads (left menu) – ${titleCase(scope)}`
    : "CRM Leads (left menu)";
  const totalMatching = Number(raw.total_matching ?? rows.length);

  return {
    title,
    subtitle: `Left drawer → Leads (/leads) | Phone / WhatsApp | Sorted A–Z`,
    chips: [
      { id: "count", label: `Leads: ${formatInt(rows.length)}`, icon: "hash" },
      { id: "phone", label: `With phone: ${formatInt(withPhone)}`, icon: "bars" },
    ],
    columns: [
      { key: "label", label: "Business Name", align: "left", kind: "text" },
      { key: "city", label: "City", align: "left", kind: "text" },
      { key: "state", label: "State", align: "left", kind: "text" },
      { key: "phone", label: "Phone", align: "left", kind: "text" },
    ],
    rows,
    totals: {
      label: "Total",
      city: "",
      state: "",
      phone: `${formatInt(withPhone)} with phone`,
    },
    footInfo: `CRM Leads (left menu): ${rows.length}${scope ? ` | ${titleCase(scope)}` : ""}${
      totalMatching > rows.length ? ` | ${formatInt(totalMatching)} matched` : ""
    }`,
    chartKey: "has_phone",
    chartLabelKey: "label",
    exportBasename: `crm-leads${scope ? `-${scope.replace(/\s+/g, "-").toLowerCase()}` : ""}`,
    shortText: `${title}: ${formatInt(rows.length)} leads (${formatInt(withPhone)} with phone).`,
    sortBy: "amount",
    hideChart: true,
  };
}

/** Full statewide Dealer Finder — phone-eligible NEW places only (Google Places batch). */
function fromDealerFinderResults(raw: Record<string, unknown>): BusinessReportData | null {
  const kind = String(raw.report_kind || "").toLowerCase();
  if (kind !== "dealer_finder_results" && !raw.results_ready) return null;
  if (kind === "dealer_finder_progress" || kind === "dealer_finder_job") return null;
  const items = raw.items;
  if (!Array.isArray(items) || items.length === 0) {
    // Still allow empty finished jobs to show a card shell via shortText only — skip here.
    if (!raw.results_ready && kind !== "dealer_finder_results") return null;
    if (!Array.isArray(items) || items.length === 0) return null;
  }
  const job = asRecord(raw.job) || {};
  const state = String(job.state || raw.state || "").trim();
  const summary = asRecord(raw.summary) || {};
  const statusReport = asRecord(raw.status_report) || {};
  const displayFilter = String(raw.display_filter || "").toLowerCase();
  const mobileOnly =
    raw.mobile_only === true || displayFilter === "eligible_new_with_mobile";
  const alreadyFiltered =
    displayFilter === "eligible_new_with_phone" ||
    displayFilter === "eligible_new_with_mobile" ||
    displayFilter === "phone_new" ||
    displayFilter === "new_with_phone";

  let rows = items
    .map((it) => {
      const row = asRecord(it);
      if (!row) return null;
      const phone = String(row.whatsapp || row.phone || "").trim();
      if (!phone) return null;
      const status = String(row.crm_status || "").trim().toLowerCase();
      const eligibleImport = row.eligible_import;
      const imported = row.imported_lead_id != null && String(row.imported_lead_id).trim() !== "";
      // Legacy stored cards may include dealer/lead/no-phone rows — keep NEW+phone only.
      if (!alreadyFiltered) {
        if (imported) return null;
        if (status === "dealer" || status === "lead") return null;
        if (status && status !== "new" && eligibleImport !== true) return null;
        if (eligibleImport === false) return null;
      }
      const website = String(row.website || "").trim();
      return {
        label: String(row.name || "").trim() || "—",
        phone,
        website: website || "—",
        city: String(row.city || "").trim() || "—",
        district: String(row.district || "").trim() || "—",
        has_phone: 1,
      };
    })
    .filter(Boolean) as Record<string, unknown>[];
  if (!rows.length) return null;
  if (mobileOnly) {
    rows = rows.filter((r) => isIndianMobile(String(r.phone || "")));
  }
  if (!rows.length) return null;
  const deduped = dedupeDealerFinderRows(rows);
  rows = deduped.rows;

  // Global filtered count — unique phones (server-authoritative when present).
  const eligibleWithPhone = Number(
    raw.filtered_total ??
      raw.eligible_with_phone ??
      summary.eligible_with_phone ??
      statusReport.eligible_with_phone ??
      raw.with_phone ??
      summary.with_phone ??
      rows.length,
  );
  const phoneDupes = Number(
    raw.phone_dupes_removed ?? summary.phone_dupes_removed ?? deduped.removed ?? 0,
  );
  const unique = Number(
    raw.total_unique ?? summary.unique_places ?? statusReport.unique_places ?? rows.length,
  );
  const jobStatus = String(job.status || statusReport.status || "").trim();
  const done = Number(job.completed_units ?? statusReport.searched_units ?? 0);
  const totalUnits = Number(job.total_units ?? 0);
  const remaining = Math.max(0, totalUnits - done);
  const scanIncomplete =
    Boolean(raw.incomplete) || (totalUnits > 0 && done < totalUnits);
  const title = state ? `Dealer Finder – ${titleCase(state)}` : "Dealer Finder";
  const importPrompt = String(raw.example_import_confirm_prompt || "").trim();
  const truncated = Boolean(raw.truncated) && rows.length < eligibleWithPhone;
  const publicId = String(job.public_id || raw.public_id || "").trim();
  const jobIdNum = Number(job.id ?? raw.job_id ?? 0);
  const jobId = jobIdNum > 0 ? jobIdNum : undefined;
  const listExclusions = parseListExclusions(raw.list_exclusions);
  const placeRowIds = (Array.isArray(items) ? items : [])
    .map((it) => {
      const row = asRecord(it);
      const id = Number(row?.id ?? 0);
      return id > 0 ? id : 0;
    })
    .filter((id) => id > 0);
  const hasExcl = exclusionsActive(listExclusions);
  const fullListOnCard =
    !truncated && placeRowIds.length > 0 && placeRowIds.length >= eligibleWithPhone;
  const usePlaceRowIds = fullListOnCard;
  const dealerFinderImport: DealerFinderImportMeta | undefined = publicId
    ? {
        publicId,
        jobId,
        importCount: eligibleWithPhone,
        placeRowIds: usePlaceRowIds ? placeRowIds : undefined,
        listExclusions: hasExcl ? listExclusions : undefined,
        usePlaceRowIds,
        importReady: !scanIncomplete,
      }
    : undefined;
  return {
    title,
    subtitle: scanIncomplete
      ? `Google Places · unique ${mobileOnly ? "mobile" : "phone"} NEW (so far) · scan ${formatInt(done)}/${formatInt(totalUnits)} · ${jobStatus || "…"}`
      : `Google Places · unique ${mobileOnly ? "mobile" : "phone"} NEW only (dealers / leads / no-phone excluded)`,
    chips: [
      {
        id: "unique_phones",
        label: `Unique phones: ${formatInt(eligibleWithPhone)}`,
        icon: "hash",
      },
      ...(scanIncomplete
        ? [
            {
              id: "scan",
              label: `Scan: ${formatInt(done)}/${formatInt(totalUnits)}`,
              icon: "bars" as const,
            },
            {
              id: "remaining",
              label: `${formatInt(remaining)} units left`,
              icon: "hash" as const,
            },
            { id: "job_status", label: jobStatus || "…", icon: "hash" as const },
          ]
        : unique > eligibleWithPhone
          ? [
              {
                id: "scanned",
                label: `Places scanned: ${formatInt(unique)}`,
                icon: "bars" as const,
              },
            ]
          : []),
      ...(phoneDupes > 0
        ? [
            {
              id: "dupes",
              label: `${formatInt(phoneDupes)} phone dupes removed`,
              icon: "hash" as const,
            },
          ]
        : []),
    ],
    columns: [
      { key: "label", label: "Name", align: "left", kind: "text" },
      { key: "phone", label: "Phone", align: "left", kind: "text" },
      { key: "website", label: "Website", align: "left", kind: "text" },
      { key: "city", label: "City", align: "left", kind: "text" },
      { key: "district", label: "District", align: "left", kind: "text" },
    ],
    rows,
    totals: {
      label: "Summary",
      phone: `${formatInt(eligibleWithPhone)} unique ${mobileOnly ? "mobile" : "phone"} NEW`,
      website: phoneDupes > 0 ? `${formatInt(phoneDupes)} dupes removed` : "",
      city: "",
      district: "",
    },
    footInfo: scanIncomplete
      ? `Dealer Finder: ${formatInt(eligibleWithPhone)} unique phone NEW (so far) · scan ${formatInt(done)}/${formatInt(totalUnits)} · ${formatInt(remaining)} remaining · ${jobStatus || "…"}${state ? ` · ${titleCase(state)}` : ""}`
      : `Dealer Finder: ${formatInt(eligibleWithPhone)} unique phone NEW${state ? ` · ${titleCase(state)}` : ""}${
          truncated ? ` · table shows ${formatInt(rows.length)} of ${formatInt(eligibleWithPhone)}` : ""
        }`,
    chartKey: "has_phone",
    chartLabelKey: "label",
    exportBasename: `dealer-finder${state ? `-${state.replace(/\s+/g, "-").toLowerCase()}` : ""}`,
    shortText: importPrompt
      ? `${title}: ${formatInt(eligibleWithPhone)} unique phone NEW` +
        (scanIncomplete
          ? ` · scan ${formatInt(done)}/${formatInt(totalUnits)} (${formatInt(remaining)} left, ${jobStatus})`
          : unique > eligibleWithPhone
            ? ` (of ${formatInt(unique)} places scanned)`
            : "") +
        `. ${importPrompt}`
      : `${title}: ${formatInt(eligibleWithPhone)} unique phone NEW places.`,
    sortBy: "amount",
    hideChart: true,
    dealerFinderImport,
  };
}

/** Google Places discovery / NEW dealers (not already in CRM Dealers or Leads). */
function fromGoogleDealerDiscovery(raw: Record<string, unknown>): BusinessReportData | null {
  const kind = String(raw.report_kind || "").toLowerCase();
  if (kind !== "new_dealers" && kind !== "google_businesses") return null;
  const items = raw.items;
  if (!Array.isArray(items) || items.length === 0) return null;
  const sample = asRecord(items[0]);
  if (!sample) return null;

  const state = String(raw.state || raw.scope || "").trim();
  const scope = String(raw.scope || state || "").trim();
  const isNew = kind === "new_dealers";

  const rows = items
    .map((it) => {
      const row = asRecord(it);
      if (!row) return null;
      const phone = String(row.phone || "").trim();
      const address = String(row.address || "").trim();
      return {
        label: String(row.name || row.company || "").trim() || "—",
        address: address || "—",
        city: String(row.city || "").trim() || "—",
        state: String(row.state || "").trim() || "—",
        phone: phone || "—",
        has_phone: phone ? 1 : 0,
      };
    })
    .filter(Boolean) as Record<string, unknown>[];

  if (!rows.length) return null;
  rows.sort((a, b) => String(a.label).localeCompare(String(b.label), undefined, { sensitivity: "base" }));
  const withPhone =
    typeof raw.with_phone === "number"
      ? Number(raw.with_phone)
      : rows.reduce((s, r) => s + Number(r.has_phone || 0), 0);
  const googleTotal = Number(raw.google_total ?? rows.length);
  const already = Number(raw.already_in_crm ?? 0);
  const title = isNew
    ? scope
      ? `New Dealers (Google − CRM) – ${titleCase(scope)}`
      : "New Dealers (Google − CRM)"
    : scope
      ? `Google Businesses – ${titleCase(scope)}`
      : "Google Businesses";

  const chips = [
    { id: "count", label: `${isNew ? "New" : "Results"}: ${formatInt(rows.length)}`, icon: "hash" as const },
    { id: "phone", label: `With phone: ${formatInt(withPhone)}`, icon: "bars" as const },
  ];
  if (isNew && googleTotal > 0) {
    chips.push({
      id: "google",
      label: `Google: ${formatInt(googleTotal)} (−${formatInt(already)} in CRM)`,
      icon: "hash" as const,
    });
  }

  return {
    title,
    subtitle: isNew
      ? "External Google Places | Excludes CRM Dealers + Leads | Phone / name+city match"
      : "External Google Places Text Search | Not CRM-filtered",
    chips,
    columns: [
      { key: "label", label: "Name", align: "left", kind: "text" },
      { key: "address", label: "Address", align: "left", kind: "text" },
      { key: "city", label: "City", align: "left", kind: "text" },
      { key: "state", label: "State", align: "left", kind: "text" },
      { key: "phone", label: "Phone", align: "left", kind: "text" },
    ],
    rows,
    totals: {
      label: "Total",
      address: "",
      city: "",
      state: "",
      phone: `${formatInt(withPhone)} with phone`,
    },
    footInfo: isNew
      ? `NEW only: ${rows.length}${scope ? ` | ${titleCase(scope)}` : ""} | CRM dealers/leads excluded`
      : `Google: ${rows.length}${scope ? ` | ${titleCase(scope)}` : ""}`,
    chartKey: "has_phone",
    chartLabelKey: "label",
    exportBasename: `${isNew ? "new-dealers" : "google-businesses"}${
      scope ? `-${scope.replace(/\s+/g, "-").toLowerCase()}` : ""
    }`,
    shortText: `${title}: ${formatInt(rows.length)} (${formatInt(withPhone)} with phone).`,
    sortBy: "amount",
    hideChart: true,
  };
}

/** Full dealer contact / WhatsApp list (Dealer, City, State, Phone) — mockup report card. */
function fromDealerContacts(raw: Record<string, unknown>): BusinessReportData | null {
  const items = raw.items;
  if (!Array.isArray(items) || items.length === 0) return null;
  const sample = asRecord(items[0]);
  if (!sample) return null;
  // Never mis-label CRM Leads as dealers
  if (
    String(raw.report_kind || "").toLowerCase() === "lead_contacts" ||
    String(raw.dataset || "").toLowerCase() === "leads" ||
    String(raw.entity || "").toLowerCase() === "leads" ||
    "lead_id" in sample
  ) {
    return null;
  }
  // Contact rows have phone/city/state; not sales rankings
  const looksContact =
    "phone" in sample ||
    String(raw.report_kind || "").toLowerCase() === "dealer_contacts" ||
    String(raw.dataset || "").toLowerCase() === "dealers";
  if (!looksContact) return null;
  if ("total" in sample && "invoice_count" in sample && !("phone" in sample)) return null;

  const state = String(raw.state || raw.filter_state || "").trim();
  const city = String(raw.city || raw.filter_city || "").trim();
  const scope = String(raw.scope || state || city || "").trim();

  const rows = items
    .map((it) => {
      const row = asRecord(it);
      if (!row) return null;
      const phone = String(row.phone || "").trim();
      return {
        label: dealerDisplayName(row),
        city: String(row.city || "").trim() || "—",
        state: String(row.state || "").trim() || "—",
        phone: phone || "—",
        has_phone: phone ? 1 : 0,
      };
    })
    .filter(Boolean) as Record<string, unknown>[];

  if (!rows.length) return null;
  // Alphabetical (tool already sorts by name; keep stable)
  rows.sort((a, b) => String(a.label).localeCompare(String(b.label), undefined, { sensitivity: "base" }));
  const withPhone =
    typeof raw.with_phone === "number"
      ? Number(raw.with_phone)
      : rows.reduce((s, r) => s + Number(r.has_phone || 0), 0);
  const title = scope ? `CRM Dealers – ${titleCase(scope)}` : "CRM Dealers";
  const totalMatching = Number(raw.total_matching ?? rows.length);

  return {
    title,
    subtitle: `Existing CRM contacts (not Google Places) | Phone / WhatsApp | Sorted A–Z`,
    chips: [
      { id: "count", label: `CRM dealers: ${formatInt(rows.length)}`, icon: "hash" },
      { id: "phone", label: `With phone: ${formatInt(withPhone)}`, icon: "bars" },
    ],
    columns: [
      { key: "label", label: "Dealer", align: "left", kind: "text" },
      { key: "city", label: "City", align: "left", kind: "text" },
      { key: "state", label: "State", align: "left", kind: "text" },
      { key: "phone", label: "Phone", align: "left", kind: "text" },
    ],
    rows,
    totals: {
      label: "Total",
      city: "",
      state: "",
      phone: `${formatInt(withPhone)} with phone`,
    },
    footInfo: `CRM Dealers (not Google): ${rows.length}${scope ? ` | ${titleCase(scope)}` : ""}${
      totalMatching > rows.length ? ` | ${formatInt(totalMatching)} matched` : ""
    }`,
    chartKey: "has_phone",
    chartLabelKey: "label",
    exportBasename: `crm-dealers${scope ? `-${scope.replace(/\s+/g, "-").toLowerCase()}` : ""}`,
    shortText: `${title}: ${formatInt(rows.length)} CRM dealers (${formatInt(withPhone)} with phone/WhatsApp). Not Google Places.`,
    sortBy: "amount",
    hideChart: true,
  };
}

function normalizeFromResult(tool: string, result: Record<string, unknown>): BusinessReportData | null {
  if (tool === "get_product_sales_by_dealer") {
    return fromProductByDealer(result);
  }
  if (tool === "request_new_tool" && result.redirected === "get_product_sales_by_dealer") {
    return fromProductByDealer(result);
  }
  if (tool === "get_region_sales") {
    return fromRegionSales(result);
  }
  if (tool === "get_dealer_sales") {
    return fromDealerSales(result);
  }
  if (
    tool === "start_dealer_finder" ||
    tool === "get_dealer_finder_results" ||
    tool === "get_dealer_finder_job" ||
    tool === "filter_dealer_finder_results"
  ) {
    return fromDealerFinderResults(result);
  }
  if (tool === "find_new_dealers" || tool === "search_google_businesses") {
    return fromGoogleDealerDiscovery(result);
  }
  if (tool === "search_leads") {
    return fromLeadContacts(result);
  }
  if (tool === "query_business_data") {
    if (Array.isArray(result.dealers)) {
      return fromProductByDealer(result);
    }
    // Delegated single-dealer shape
    if (result.resolved && asRecord(result.dealer)) {
      return fromDealerSales(result);
    }
    const ds = String(result.dataset || "").toLowerCase();
    const kind = String(result.report_kind || "").toLowerCase();
    if (ds === "leads" || kind === "lead_contacts") {
      return fromLeadContacts(result);
    }
    if (ds === "dealers" || kind === "dealer_contacts") {
      return fromDealerContacts(result);
    }
    if (ds === "zoho_sales" || ds === "sales" || !ds) {
      return fromTopDealers(result) || fromGeoSales(result) || fromDealerContacts(result);
    }
    if (ds === "invoice_lines" && Array.isArray(result.items)) {
      // product group tables — treat as amount DESC ranking when totals present
      const items = result.items as unknown[];
      const sample = asRecord(items[0]);
      if (sample && ("total" in sample || "quantity" in sample)) {
        const groupBy = String(result.group_by || "product").toLowerCase();
        const period = periodLabel(result.period);
        const sortBy =
          "quantity" in sample && Number(sample.quantity) > 0 ? "quantity" : "amount";
        const rows = items
          .map((it) => {
            const row = asRecord(it);
            if (!row) return null;
            const label = String(
              row.item_name || row.product_category || row.category || row.name || "—",
            ).trim();
            return {
              label,
              quantity: Number(row.quantity ?? 0),
              total: Number(row.total ?? 0),
              invoice_count: Number(row.invoice_count ?? row.line_count ?? 0),
            };
          })
          .filter(Boolean) as Record<string, unknown>[];
        if (!rows.length) return null;
        const sorted = sortRows(rows, sortBy === "quantity" ? "quantity" : "amount");
        const qty = sorted.reduce((s, d) => s + Number(d.quantity || 0), 0);
        const total = sorted.reduce((s, d) => s + Number(d.total || 0), 0);
        const title = `Sales by ${titleCase(groupBy || "product")}`;
        return {
          title,
          subtitle: `Period: ${period} | Tax: Exclusive | Ranked by ${sortBy === "quantity" ? "pcs" : "₹"} DESC`,
          chips: [
            { id: "qty", label: `Total: ${formatQty(qty)}`, icon: "bars" },
            { id: "sales", label: `Sales: ${formatInr(total)}`, icon: "coins" },
          ],
          columns: [
            { key: "label", label: titleCase(groupBy || "Item"), align: "left", kind: "text" },
            { key: "quantity", label: "Quantity (pcs)", align: "right", kind: "qty" },
            { key: "total", label: "Sales Amount (₹)", align: "right", kind: "inr" },
          ],
          rows: sorted,
          totals: { label: "Total", quantity: qty, total },
          footInfo: `Showing ${sorted.length} rows | ${period}`,
          chartKey: sortBy === "quantity" ? "quantity" : "total",
          chartLabelKey: "label",
          exportBasename: `sales-by-${groupBy || "product"}`,
          shortText: `${title}: ${formatQty(qty)} / ${formatInr(total)} (${period}, tax-exclusive).`,
          sortBy: sortBy === "quantity" ? "quantity" : "amount",
        };
      }
    }
  }
  return null;
}

/** Prefer structured report cards over markdown lists for known sales tools. */
export function extractBusinessReport(toolResults?: unknown[]): BusinessReportData | null {
  if (!toolResults?.length) return null;
  for (let i = toolResults.length - 1; i >= 0; i -= 1) {
    const outcome = asRecord(toolResults[i]) as ToolOutcome | null;
    if (!outcome?.ok) continue;
    const tool = String(outcome.tool || "");
    const result = asRecord(outcome.result);
    if (!result) continue;
    const data = normalizeFromResult(tool, result);
    if (data) return data;
  }
  return null;
}

const DEALER_FINDER_TOOLS = new Set([
  "start_dealer_finder",
  "get_dealer_finder_job",
  "get_dealer_finder_results",
  "filter_dealer_finder_results",
]);

/** List exclusions from the newest Dealer Finder tool result in this turn. */
export function extractDealerFinderListExclusions(
  toolResults?: unknown[],
): DealerFinderListExclusions | undefined {
  if (!toolResults?.length) return undefined;
  for (let i = toolResults.length - 1; i >= 0; i -= 1) {
    const outcome = asRecord(toolResults[i]) as ToolOutcome | null;
    if (!outcome?.ok) continue;
    const tool = String(outcome.tool || "");
    if (!DEALER_FINDER_TOOLS.has(tool)) continue;
    const result = asRecord(outcome.result);
    if (!result) continue;
    const excl = parseListExclusions(result.list_exclusions);
    if (excl) return excl;
  }
  return undefined;
}

const UUID_RE =
  /\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/i;

/** Extract Dealer Finder job public_id from tool_results or assistant text. */
export function extractDealerFinderJobId(
  toolResults?: unknown[],
  resultSummary?: string | null,
): string | null {
  if (toolResults?.length) {
    for (let i = toolResults.length - 1; i >= 0; i -= 1) {
      const outcome = asRecord(toolResults[i]) as ToolOutcome | null;
      if (!outcome) continue;
      const tool = String(outcome.tool || "");
      if (!DEALER_FINDER_TOOLS.has(tool)) continue;
      const result = asRecord(outcome.result);
      if (!result) continue;
      const direct = String(result.public_id || "").trim();
      if (direct) return direct;
      const job = asRecord(result.job);
      const fromJob = String(job?.public_id || "").trim();
      if (fromJob) return fromJob;
    }
  }
  const m = UUID_RE.exec(String(resultSummary || ""));
  return m ? m[1] : null;
}

/** True when tool_results already contain a ready Dealer Finder results card payload. */
export function hasDealerFinderResultsCard(toolResults?: unknown[]): boolean {
  if (!toolResults?.length) return false;
  for (let i = toolResults.length - 1; i >= 0; i -= 1) {
    const outcome = asRecord(toolResults[i]) as ToolOutcome | null;
    if (!outcome?.ok) continue;
    const tool = String(outcome.tool || "");
    if (!DEALER_FINDER_TOOLS.has(tool)) continue;
    const result = asRecord(outcome.result);
    if (!result) continue;
    if (result.results_ready || String(result.report_kind || "").toLowerCase() === "dealer_finder_results") {
      const items = result.items;
      if (Array.isArray(items) && items.length > 0) return true;
    }
  }
  return false;
}

/** Build a BusinessReportData from a live /api/dealer-finder jobs…/results payload. */
export function businessReportFromDealerFinderApi(payload: Record<string, unknown>): BusinessReportData | null {
  const job = asRecord(payload.job) || {};
  const items = Array.isArray(payload.items) ? payload.items : [];
  const summary = asRecord(payload.summary) || asRecord(payload.status_report) || {};
  const phoneElig = Number(
    payload.filtered_total ??
      payload.eligible_with_phone ??
      summary.eligible_with_phone ??
      payload.with_phone ??
      summary.with_phone ??
      0,
  );
  const unique = Number(
    payload.total_unique ?? summary.unique_places ?? payload.total ?? items.length,
  );
  const synthetic: Record<string, unknown> = {
    report_kind: "dealer_finder_results",
    results_ready: true,
    incomplete: Boolean(payload.incomplete),
    scan_complete: Boolean(payload.scan_complete),
    job,
    state: job.state,
    items,
    display_filter: String(payload.display_filter || "eligible_new_with_mobile"),
    mobile_only: payload.mobile_only !== false,
    filtered_total: phoneElig,
    eligible_with_phone: phoneElig,
    with_phone: phoneElig,
    phone_dupes_removed: Number(payload.phone_dupes_removed ?? summary.phone_dupes_removed ?? 0),
    phone_list_format_version: Number(payload.phone_list_format_version ?? 2),
    truncated: Boolean(payload.truncated),
    summary: {
      unique_places: unique,
      eligible_import: Number(summary.eligible ?? summary.eligible_import ?? 0),
      eligible_with_phone: phoneElig,
      with_phone: phoneElig,
      phone_dupes_removed: Number(payload.phone_dupes_removed ?? summary.phone_dupes_removed ?? 0),
      with_website: Number(summary.with_website ?? 0),
    },
    status_report: asRecord(payload.status_report) || summary,
    total_unique: unique,
    count: items.length,
    example_import_confirm_prompt: String(
      payload.example_import_confirm_prompt ||
        `Import ${phoneElig} phone-eligible NEW places to Leads (source Google, status New)? Reply yes to import.`,
    ),
    list_exclusions: payload.list_exclusions,
    public_id: job.public_id,
    job_id: job.id,
  };
  return fromDealerFinderResults(synthetic);
}

/** Merge chat-stored list exclusions into a live-fetched card (live API is unfiltered). */
export function mergeDealerFinderCardExclusions(
  card: BusinessReportData,
  toolResults?: unknown[],
): BusinessReportData {
  const stored = extractDealerFinderListExclusions(toolResults);
  if (!stored || !card.dealerFinderImport) return card;
  const imp = card.dealerFinderImport;
  const fullList =
    Boolean(imp.usePlaceRowIds && (imp.placeRowIds?.length ?? 0) >= imp.importCount);
  return {
    ...card,
    dealerFinderImport: {
      ...imp,
      listExclusions: stored,
      usePlaceRowIds: fullList,
      placeRowIds: fullList ? imp.placeRowIds : undefined,
    },
  };
}

async function importDealerFinderCardToLeads(meta: DealerFinderImportMeta): Promise<{
  inserted: number;
  skipped_duplicates?: number;
}> {
  const body: Record<string, unknown> = {
    public_id: meta.publicId,
    confirm: true,
    with_phone_only: true,
  };
  if (meta.jobId) body.job_id = meta.jobId;
  if (meta.usePlaceRowIds && meta.placeRowIds?.length) {
    body.place_row_ids = meta.placeRowIds;
  } else if (meta.listExclusions) {
    if (meta.listExclusions.exclude_districts?.length) {
      body.exclude_districts = meta.listExclusions.exclude_districts;
    }
    if (meta.listExclusions.exclude_cities?.length) {
      body.exclude_cities = meta.listExclusions.exclude_cities;
    }
    if (meta.listExclusions.exclude_keywords?.length) {
      body.exclude_keywords = meta.listExclusions.exclude_keywords;
    }
  }
  return api<{ inserted: number; skipped_duplicates?: number }>("/api/dealer-finder/import", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** True when a stored dealer-finder card should be refreshed from the live job API. */
export function dealerFinderCardNeedsRefresh(toolResults?: unknown[]): boolean {
  if (!toolResults?.length) return true;
  for (let i = toolResults.length - 1; i >= 0; i -= 1) {
    const outcome = asRecord(toolResults[i]) as ToolOutcome | null;
    if (!outcome?.ok) continue;
    const tool = String(outcome.tool || "");
    if (!DEALER_FINDER_TOOLS.has(tool)) continue;
    const result = asRecord(outcome.result);
    if (!result) continue;
    if (!(result.results_ready || String(result.report_kind || "").toLowerCase() === "dealer_finder_results")) {
      continue;
    }
    const filter = String(result.display_filter || "").toLowerCase();
    const items = Array.isArray(result.items) ? result.items : [];
    const filteredTotal = Number(result.filtered_total ?? result.eligible_with_phone ?? 0);
    const fmtVer = Number(result.phone_list_format_version ?? 0);
    const job = asRecord(result.job);
    const jobStatus = String(job?.status || "");
    // Old cards: no filter flag, STATUS rows, empty/partial items, or mismatch vs filtered total.
    if (fmtVer < 2) return true;
    if (["Pending", "Running"].includes(jobStatus)) return true;
    if (filter !== "eligible_new_with_mobile" && filter !== "eligible_new_with_phone") return true;
    if (result.mobile_only !== true) return true;
    if (Boolean(result.stored_partial) || Boolean(result.truncated)) return true;
    if (items.length === 0 && filteredTotal > 0) return true;
    if (filteredTotal > 0 && items.length > 0 && items.length < filteredTotal) return true;
    const sample = asRecord(items[0]);
    if (sample && "crm_status" in sample && filter !== "eligible_new_with_phone") return true;
    const excl = asRecord(result.list_exclusions);
    const hasExcl =
      Boolean(excl) &&
      (Array.isArray(excl?.exclude_districts) && (excl.exclude_districts as unknown[]).length > 0 ||
        Array.isArray(excl?.exclude_cities) && (excl.exclude_cities as unknown[]).length > 0 ||
        Array.isArray(excl?.exclude_keywords) && (excl.exclude_keywords as unknown[]).length > 0);
  if (hasExcl && filteredTotal > 0 && items.length === filteredTotal) return false;
    return false;
  }
  return true;
}

/** @deprecated Prefer extractBusinessReport */
export function extractSalesByDealer(toolResults?: unknown[]): BusinessReportData | null {
  const report = extractBusinessReport(toolResults);
  if (!report) return null;
  // Only treat product×dealer as the legacy sales card shape when qty column exists
  if (report.columns.some((c) => c.key === "quantity") && report.title.startsWith("Sales by Dealer")) {
    return report;
  }
  return report;
}

function downloadCsv(data: BusinessReportData) {
  const cols = data.columns;
  const lines = [
    ["#", ...cols.map((c) => c.label)].join(","),
    ...data.rows.map((row, i) =>
      [
        i + 1,
        ...cols.map((c) => {
          const raw = row[c.key];
          const text = formatCell(c.kind, raw);
          if (c.kind === "inr" || c.kind === "qty" || c.kind === "int") {
            return typeof raw === "number" ? raw : text;
          }
          return `"${String(text).replace(/"/g, '""')}"`;
        }),
      ].join(","),
    ),
  ];
  if (data.totals) {
    lines.push(
      [
        "",
        ...cols.map((c) => {
          if (c.key === "label") return "Total";
          const raw = data.totals?.[c.key];
          if (raw == null || raw === "") return "";
          return typeof raw === "number" ? raw : `"${String(raw).replace(/"/g, '""')}"`;
        }),
      ].join(","),
    );
  }
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${data.exportBasename || "report"}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

async function copyTable(data: BusinessReportData) {
  const cols = data.columns;
  const lines = [
    data.title,
    data.subtitle,
    "",
    ["#", ...cols.map((c) => c.label)].join("\t"),
    ...data.rows.map((row, i) =>
      [i + 1, ...cols.map((c) => formatCell(c.kind, row[c.key]))].join("\t"),
    ),
  ];
  if (data.totals) {
    lines.push(
      ["", ...cols.map((c) => (c.key === "label" ? "Total" : formatCell(c.kind, data.totals?.[c.key])))].join(
        "\t",
      ),
    );
  }
  await navigator.clipboard.writeText(lines.join("\n"));
}

function ChipIcon({ kind }: { kind?: ReportChip["icon"] }) {
  if (kind === "coins") {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
        <ellipse cx="12" cy="6" rx="7" ry="3" />
        <path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6" />
        <path d="M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" />
      </svg>
    );
  }
  if (kind === "hash") {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
        <path d="M4 9h16M4 15h16M10 3L8 21M16 3l-2 18" />
      </svg>
    );
  }
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
      <line x1="4" y1="20" x2="20" y2="20" />
      <rect x="6" y="10" width="3" height="7" rx="1" />
      <rect x="11" y="6" width="3" height="11" rx="1" />
      <rect x="16" y="12" width="3" height="5" rx="1" />
    </svg>
  );
}

type Props = { data: BusinessReportData };

export default function BusinessReportCard({ data }: Props) {
  const [showChart, setShowChart] = useState(false);
  const [copied, setCopied] = useState(false);
  const [importBusy, setImportBusy] = useState(false);
  const [importFlash, setImportFlash] = useState("");
  const [dealerTableExpanded, setDealerTableExpanded] = useState(false);
  const dealerImport = data.dealerFinderImport;
  const isDealerFinderCard = Boolean(dealerImport?.publicId);
  const totalListRows = data.rows.length;
  const dealerListCap = isDealerFinderCard ? DEALER_FINDER_TABLE_PREVIEW_ROWS : totalListRows;
  const dealerListTruncated =
    isDealerFinderCard && !dealerTableExpanded && totalListRows > dealerListCap;
  const visibleRows = useMemo(() => {
    if (!dealerListTruncated) return data.rows;
    return data.rows.slice(0, dealerListCap);
  }, [data.rows, dealerListTruncated, dealerListCap]);
  const showingCount = dealerListTruncated ? dealerListCap : totalListRows;

  useEffect(() => {
    setDealerTableExpanded(false);
  }, [data.exportBasename, data.title, totalListRows]);
  const maxChart = useMemo(() => {
    const vals = data.rows.map((r) => Number(r[data.chartKey] || 0));
    return Math.max(1, ...vals);
  }, [data.rows, data.chartKey]);

  const onImportToLeads = useCallback(async () => {
    if (!dealerImport?.publicId || importBusy) return;
    const n = dealerImport.importCount;
    const stateHint = data.title.replace(/^Dealer Finder\s*[–-]\s*/i, "").trim();
    const msg =
      `Import ${formatInt(n)} unique phone NEW place${n === 1 ? "" : "s"}` +
      (stateHint ? ` from ${stateHint}` : "") +
      ` into CRM Leads (source Google)?`;
    if (!window.confirm(msg)) return;
    setImportBusy(true);
    setImportFlash("");
    try {
      const r = await importDealerFinderCardToLeads(dealerImport);
      const ins = Number(r.inserted ?? 0);
      const skipped = Number(r.skipped_duplicates ?? 0);
      setImportFlash(
        `Imported ${formatInt(ins)} lead${ins === 1 ? "" : "s"}` +
          (skipped > 0 ? ` · skipped ${formatInt(skipped)} duplicates` : "") +
          ".",
      );
    } catch (e) {
      setImportFlash(e instanceof Error ? e.message : "Import failed");
    } finally {
      setImportBusy(false);
    }
  }, [dealerImport, data.title, importBusy]);

  return (
    <div className="sbd-card">
      <div className="sbd-head">
        <div className="sbd-head-main">
          <span className="sbd-spark" aria-hidden>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M12 3l1.5 5.5L19 10l-5.5 1.5L12 17l-1.5-5.5L5 10l5.5-1.5L12 3z" />
              <path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15z" />
            </svg>
          </span>
          <div>
            <h3 className="sbd-title">{data.title}</h3>
            <p className="sbd-sub">{data.subtitle}</p>
          </div>
        </div>
        <div className="sbd-chips">
          {data.chips.map((chip) => (
            <span key={chip.id} className="sbd-chip">
              <ChipIcon kind={chip.icon} />
              {chip.label}
            </span>
          ))}
        </div>
      </div>

      <div className="sbd-table-wrap">
        <table className="sbd-table">
          <thead>
            <tr>
              <th className="sbd-col-num">#</th>
              {data.columns.map((col) => (
                <th
                  key={col.key}
                  className={
                    col.align === "right"
                      ? "sbd-col-amt"
                      : col.align === "center"
                        ? "sbd-col-num"
                        : "sbd-col-dealer"
                  }
                >
                  {col.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visibleRows.map((row, i) => (
              <tr key={`${String(row.label || row.customer_id || i)}-${i}`}>
                <td className="sbd-col-num">{i + 1}</td>
                {data.columns.map((col) => (
                  <td
                    key={col.key}
                    className={
                      col.align === "right"
                        ? "sbd-col-amt"
                        : col.align === "center"
                          ? "sbd-col-num"
                          : "sbd-col-dealer"
                    }
                  >
                    {formatCell(col.kind, row[col.key])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
          {data.totals && (
            <tfoot>
              <tr>
                <td className="sbd-col-num" />
                {data.columns.map((col) => (
                  <td
                    key={col.key}
                    className={
                      col.align === "right"
                        ? "sbd-col-amt"
                        : col.align === "center"
                          ? "sbd-col-num"
                          : "sbd-col-dealer"
                    }
                  >
                    <strong>
                      {col.key === "label"
                        ? String(data.totals?.label || "Total")
                        : data.totals?.[col.key] == null || data.totals?.[col.key] === ""
                          ? ""
                          : formatCell(col.kind, data.totals[col.key])}
                    </strong>
                  </td>
                ))}
              </tr>
            </tfoot>
          )}
        </table>
      </div>

      {showChart && (
        <div className="sbd-chart" aria-label="Report chart">
          {data.rows.slice(0, 12).map((row, i) => {
            const val = Number(row[data.chartKey] || 0);
            const pct = Math.max(4, Math.round((val / maxChart) * 100));
            const label = String(row[data.chartLabelKey] || "—");
            const col = data.columns.find((c) => c.key === data.chartKey);
            return (
              <div key={`bar-${i}`} className="sbd-bar-row">
                <span className="sbd-bar-label" title={label}>
                  {label}
                </span>
                <div className="sbd-bar-track">
                  <div className="sbd-bar-fill" style={{ width: `${pct}%` }} />
                </div>
                <span className="sbd-bar-val">{formatCell(col?.kind || "text", val)}</span>
              </div>
            );
          })}
        </div>
      )}

      <div className="sbd-foot">
        <p className="sbd-foot-info">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
            <circle cx="12" cy="12" r="9" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <circle cx="12" cy="16" r="0.8" fill="currentColor" stroke="none" />
          </svg>
          {data.footInfo}
        </p>
        <div className="sbd-actions">
          {isDealerFinderCard && totalListRows > dealerListCap ? (
            <span className="sbd-foot-show">
              Showing {formatInt(showingCount)} of {formatInt(totalListRows)}
            </span>
          ) : null}
          {dealerListTruncated ? (
            <button
              type="button"
              className="sbd-btn"
              onClick={() => setDealerTableExpanded(true)}
            >
              More
            </button>
          ) : null}
          {isDealerFinderCard ? (
            <button
              type="button"
              className="sbd-btn sbd-btn-primary"
              disabled={
                importBusy ||
                dealerImport!.importCount <= 0 ||
                dealerImport!.importReady === false
              }
              onClick={() => void onImportToLeads()}
            >
              {dealerImport!.importReady === false
                ? "Scan in progress…"
                : importBusy
                  ? "Importing…"
                  : "Imports to Leads"}
            </button>
          ) : (
            <>
              <button type="button" className="sbd-btn" onClick={() => downloadCsv(data)}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                  <path d="M12 3v12" />
                  <path d="M7 10l5 5 5-5" />
                  <path d="M5 21h14" />
                </svg>
                Export
              </button>
              {!data.hideChart && (
                <button type="button" className="sbd-btn" onClick={() => setShowChart((v) => !v)}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                    <line x1="4" y1="20" x2="20" y2="20" />
                    <rect x="6" y="10" width="3" height="7" rx="1" />
                    <rect x="11" y="6" width="3" height="11" rx="1" />
                    <rect x="16" y="12" width="3" height="5" rx="1" />
                  </svg>
                  {showChart ? "Hide Chart" : "View Chart"}
                </button>
              )}
              <button
                type="button"
                className="sbd-btn"
                onClick={() => {
                  void copyTable(data).then(() => {
                    setCopied(true);
                    window.setTimeout(() => setCopied(false), 1600);
                  });
                }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                  <rect x="9" y="9" width="11" height="11" rx="2" />
                  <path d="M5 15V5a2 2 0 0 1 2-2h10" />
                </svg>
                {copied ? "Copied" : "Copy"}
              </button>
            </>
          )}
        </div>
        {importFlash ? <p className="sbd-import-flash">{importFlash}</p> : null}
      </div>
    </div>
  );
}
