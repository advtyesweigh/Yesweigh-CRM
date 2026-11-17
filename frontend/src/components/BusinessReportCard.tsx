import { useMemo, useState } from "react";

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

/** Full dealer contact / WhatsApp list (Dealer, City, State, Phone) — mockup report card. */
function fromDealerContacts(raw: Record<string, unknown>): BusinessReportData | null {
  const items = raw.items;
  if (!Array.isArray(items) || items.length === 0) return null;
  const sample = asRecord(items[0]);
  if (!sample) return null;
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
  const title = scope ? `Dealer Contacts – ${titleCase(scope)}` : "Dealer Contacts";
  const totalMatching = Number(raw.total_matching ?? rows.length);

  return {
    title,
    subtitle: `Contact list | Phone / WhatsApp | Sorted A–Z`,
    chips: [
      { id: "count", label: `Dealers: ${formatInt(rows.length)}`, icon: "hash" },
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
    footInfo: `Showing ${rows.length} dealers${scope ? ` | ${titleCase(scope)}` : ""}${
      totalMatching > rows.length ? ` | ${formatInt(totalMatching)} matched` : ""
    }`,
    chartKey: "has_phone",
    chartLabelKey: "label",
    exportBasename: `dealer-contacts${scope ? `-${scope.replace(/\s+/g, "-").toLowerCase()}` : ""}`,
    shortText: `${title}: ${formatInt(rows.length)} dealers (${formatInt(withPhone)} with phone/WhatsApp).`,
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
  if (tool === "query_business_data") {
    if (Array.isArray(result.dealers)) {
      return fromProductByDealer(result);
    }
    // Delegated single-dealer shape
    if (result.resolved && asRecord(result.dealer)) {
      return fromDealerSales(result);
    }
    const ds = String(result.dataset || "").toLowerCase();
    if (ds === "dealers" || String(result.report_kind || "").toLowerCase() === "dealer_contacts") {
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
  const maxChart = useMemo(() => {
    const vals = data.rows.map((r) => Number(r[data.chartKey] || 0));
    return Math.max(1, ...vals);
  }, [data.rows, data.chartKey]);

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
            {data.rows.map((row, i) => (
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
                        ? "Total"
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
        </div>
      </div>
    </div>
  );
}
