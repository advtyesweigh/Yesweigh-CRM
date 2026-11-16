import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import IndiaSalesMap from "../components/IndiaSalesMap";

/* —— Types (match dashboard APIs) —— */

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
  prior_fy_total?: number;
  prior_fy_invoice_count?: number;
  prior_fy_label?: string;
  yoy_pct?: number | null;
  last_month_till_date_total: number;
  last_month_till_date_invoice_count: number;
  months: MonthSalesPoint[];
};

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
};

type CategorySalesItem = {
  category: string;
  value: number;
  qty: number;
  value_pct: number;
  qty_pct: number;
};

type CategorySalesOut = {
  year: number;
  total_value: number;
  total_qty: number;
  items: CategorySalesItem[];
};

type SalesByStateOut = {
  year: number;
  items: { state: string; sales_total: number; invoice_count?: number }[];
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

type DealerStats = {
  total: number;
  active: number;
  inactive: number;
  states_covered: number;
};

type DealersPage = {
  stats: DealerStats;
};

/* —— Helpers —— */

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
  if (abs >= 1e5) return `₹${(n / 1e5).toFixed(2)} L`;
  return `₹${Math.round(n).toLocaleString("en-IN")}`;
}

function fmtQty(n: number | null | undefined) {
  if (n == null || Number.isNaN(n)) return "0";
  return Math.round(n).toLocaleString("en-IN");
}

function fmtPct(n: number | null | undefined, digits = 1) {
  if (n == null || Number.isNaN(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(digits)}%`;
}

function pctChange(cur: number, prev: number): number | null {
  if (!prev) return null;
  return (100 * (cur - prev)) / prev;
}

const REGION_OF: Record<string, string> = {
  Maharashtra: "West",
  Gujarat: "West",
  Goa: "West",
  Rajasthan: "West",
  "Madhya Pradesh": "West",
  "Dadra and Nagar Haveli and Daman and Diu": "West",
  Kerala: "South",
  Karnataka: "South",
  "Tamil Nadu": "South",
  Telangana: "South",
  "Andhra Pradesh": "South",
  Puducherry: "South",
  Lakshadweep: "South",
  Delhi: "North",
  Haryana: "North",
  Punjab: "North",
  "Uttar Pradesh": "North",
  Uttarakhand: "North",
  "Himachal Pradesh": "North",
  "Jammu and Kashmir": "North",
  Ladakh: "North",
  Chandigarh: "North",
  "West Bengal": "East",
  Odisha: "East",
  Bihar: "East",
  Jharkhand: "East",
  Assam: "East",
  "Arunachal Pradesh": "East",
  Manipur: "East",
  Meghalaya: "East",
  Mizoram: "East",
  Nagaland: "East",
  Sikkim: "East",
  Tripura: "East",
  Chhattisgarh: "East",
  "Andaman and Nicobar Islands": "East",
};

const CAT_COLORS = ["#1859e8", "#0ea5e9", "#087a56", "#f59e0b", "#6366f1", "#64748b"];

function MultiDonut({
  items,
  centerLabel,
  centerValue,
}: {
  items: { label: string; value: number; color: string }[];
  centerLabel: string;
  centerValue: string;
}) {
  const total = items.reduce((s, i) => s + i.value, 0);
  const r = 42;
  const c = 2 * Math.PI * r;
  let offset = 0;
  const slices =
    total <= 0
      ? []
      : items
          .filter((i) => i.value > 0)
          .map((i) => {
            const len = (i.value / total) * c;
            const slice = { ...i, dash: len, gap: c - len, offset };
            offset += len;
            return slice;
          });

  return (
    <div className="sa-donut-wrap">
      <div className="sa-donut">
        <svg viewBox="0 0 100 100" width={140} height={140} aria-hidden>
          <circle cx="50" cy="50" r={r} fill="none" stroke="#e8eef8" strokeWidth="14" />
          {slices.map((s) => (
            <circle
              key={s.label}
              cx="50"
              cy="50"
              r={r}
              fill="none"
              stroke={s.color}
              strokeWidth="14"
              strokeDasharray={`${s.dash} ${s.gap}`}
              strokeDashoffset={-s.offset}
              transform="rotate(-90 50 50)"
            />
          ))}
        </svg>
        <div className="sa-donut-center">
          <strong>{centerValue}</strong>
          <span>{centerLabel}</span>
        </div>
      </div>
      <ul className="sa-donut-legend">
        {items.map((i) => (
          <li key={i.label}>
            <i style={{ background: i.color }} />
            <span className="sa-donut-name">{i.label}</span>
            <b>{total > 0 ? `${((100 * i.value) / total).toFixed(1)}%` : "—"}</b>
          </li>
        ))}
      </ul>
    </div>
  );
}

function TrendArea({ points }: { points: { label: string; total: number }[] }) {
  const w = 560;
  const h = 180;
  const pad = { t: 16, r: 12, b: 28, l: 8 };
  const innerW = w - pad.l - pad.r;
  const innerH = h - pad.t - pad.b;
  const max = Math.max(1, ...points.map((p) => p.total));
  const n = points.length;
  if (n === 0) {
    return <div className="sa-empty">No trend data</div>;
  }
  const xs = points.map((_, i) => pad.l + (n === 1 ? innerW / 2 : (i / (n - 1)) * innerW));
  const ys = points.map((p) => pad.t + innerH - (p.total / max) * innerH);
  const line = points.map((_, i) => `${i === 0 ? "M" : "L"}${xs[i]},${ys[i]}`).join(" ");
  const area = `${line} L${xs[n - 1]},${pad.t + innerH} L${xs[0]},${pad.t + innerH} Z`;

  return (
    <svg className="sa-trend-svg" viewBox={`0 0 ${w} ${h}`} role="img" aria-label="Sales trend">
      <defs>
        <linearGradient id="saTrendFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#1859e8" stopOpacity="0.28" />
          <stop offset="100%" stopColor="#1859e8" stopOpacity="0.02" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#saTrendFill)" />
      <path d={line} fill="none" stroke="#1859e8" strokeWidth="2.4" strokeLinejoin="round" />
      {points.map((p, i) => (
        <g key={p.label}>
          <circle cx={xs[i]} cy={ys[i]} r="3.5" fill="#fff" stroke="#1859e8" strokeWidth="2" />
          <text x={xs[i]} y={h - 8} textAnchor="middle" className="sa-chart-axis">
            {p.label}
          </text>
        </g>
      ))}
    </svg>
  );
}

function MonthlyBars({
  points,
}: {
  points: { label: string; total: number; invoice_count: number }[];
}) {
  const w = 560;
  const h = 200;
  const pad = { t: 16, r: 36, b: 32, l: 8 };
  const innerW = w - pad.l - pad.r;
  const innerH = h - pad.t - pad.b;
  const maxVal = Math.max(1, ...points.map((p) => p.total));
  const maxUnits = Math.max(1, ...points.map((p) => p.invoice_count));
  const n = points.length;
  if (n === 0) return <div className="sa-empty">No monthly data</div>;
  const gap = 8;
  const barW = Math.max(8, (innerW - gap * (n - 1)) / n);

  const linePts = points.map((p, i) => {
    const x = pad.l + i * (barW + gap) + barW / 2;
    const y = pad.t + innerH - (p.invoice_count / maxUnits) * innerH;
    return { x, y, label: p.label };
  });
  const line = linePts.map((p, i) => `${i === 0 ? "M" : "L"}${p.x},${p.y}`).join(" ");

  return (
    <svg className="sa-trend-svg" viewBox={`0 0 ${w} ${h}`} role="img" aria-label="Monthly trend">
      {points.map((p, i) => {
        const x = pad.l + i * (barW + gap);
        const bh = (p.total / maxVal) * innerH;
        const y = pad.t + innerH - bh;
        return (
          <g key={p.label}>
            <rect x={x} y={y} width={barW} height={bh} rx="3" fill="#93c5fd" />
            <text x={x + barW / 2} y={h - 10} textAnchor="middle" className="sa-chart-axis">
              {p.label.split(" ")[0]}
            </text>
          </g>
        );
      })}
      <path d={line} fill="none" stroke="#1859e8" strokeWidth="2" />
      {linePts.map((p) => (
        <circle key={p.label} cx={p.x} cy={p.y} r="3.5" fill="#fff" stroke="#1859e8" strokeWidth="2" />
      ))}
    </svg>
  );
}

function HBarList({
  rows,
}: {
  rows: { name: string; value: number; pct: number; sub?: string }[];
}) {
  if (!rows.length) return <div className="sa-empty">No data</div>;
  return (
    <ul className="sa-hbar">
      {rows.map((r, i) => (
        <li key={r.name}>
          <div className="sa-hbar-meta">
            <span className="sa-hbar-rank">{i + 1}</span>
            <span className="sa-hbar-name" title={r.name}>
              {r.name}
            </span>
            <strong>{fmtINRCompact(r.value)}</strong>
          </div>
          <div className="sa-hbar-track">
            <div className="sa-hbar-fill" style={{ width: `${Math.min(100, Math.max(0, r.pct))}%` }} />
          </div>
          {r.sub ? <div className="sa-hbar-sub">{r.sub}</div> : null}
        </li>
      ))}
    </ul>
  );
}

export default function Analytics() {
  const now = new Date();
  const fyYear = indianFyStartYear(now);
  const calYear = now.getFullYear();
  const [year, setYear] = useState(calYear);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");

  const [overview, setOverview] = useState<SalesOverviewOut | null>(null);
  const [products, setProducts] = useState<ProductSalesOut | null>(null);
  const [categories, setCategories] = useState<CategorySalesOut | null>(null);
  const [byState, setByState] = useState<SalesByStateOut | null>(null);
  const [topDealers, setTopDealers] = useState<TopDealersYtdOut | null>(null);
  const [dealerStats, setDealerStats] = useState<DealerStats | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErr("");
    // Calendar year for state/product/category; same year as Indian FY start for dealers YTD
    const dealersFy = Math.min(year, fyYear);

    Promise.all([
      api<SalesOverviewOut>(`/api/dashboard/sales-overview?months_count=12`).catch(() => null),
      api<ProductSalesOut>(`/api/dashboard/sales-by-product?year=${year}&limit=10`).catch(() => null),
      api<CategorySalesOut>(`/api/dashboard/sales-by-category?year=${year}`).catch(() => null),
      api<SalesByStateOut>(`/api/dealers/sales-by-state?year=${year}`).catch(() => null),
      api<TopDealersYtdOut>(`/api/dashboard/top-dealers-ytd?year=${dealersFy}&limit=10`).catch(() => null),
      api<DealersPage>(`/api/dealers?page=1&page_size=1`).catch(() => null),
    ])
      .then(([ov, prod, cat, state, dealers, dpage]) => {
        if (cancelled) return;
        setOverview(ov);
        setProducts(prod);
        setCategories(cat);
        setByState(state);
        setTopDealers(dealers);
        setDealerStats(dpage?.stats ?? null);
        if (!ov && !prod && !state) setErr("Could not load sales analytics.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [year, fyYear]);

  const stateItems = byState?.items ?? [];
  const stateTotal = useMemo(
    () => stateItems.reduce((s, r) => s + (r.sales_total || 0), 0),
    [stateItems],
  );
  const topStates = useMemo(
    () => [...stateItems].sort((a, b) => b.sales_total - a.sales_total).slice(0, 10),
    [stateItems],
  );
  const lowStates = useMemo(
    () =>
      [...stateItems]
        .filter((r) => r.sales_total > 0)
        .sort((a, b) => a.sales_total - b.sales_total)
        .slice(0, 6),
    [stateItems],
  );

  const trendPoints = useMemo(() => {
    const months = [...(overview?.months ?? [])].reverse();
    return months.map((m) => ({ label: m.label, total: m.total }));
  }, [overview]);

  const monthlyPoints = useMemo(() => {
    return [...(overview?.months ?? [])].reverse().map((m) => ({
      label: m.label,
      total: m.total,
      invoice_count: m.invoice_count,
    }));
  }, [overview]);

  const catItems = categories?.items ?? [];
  const topCat = catItems[0];
  const donutItems = catItems.map((c, i) => ({
    label: c.category,
    value: c.value,
    color: CAT_COLORS[i % CAT_COLORS.length],
  }));

  const productRows = (products?.by_value ?? []).slice(0, 8).map((p) => ({
    name: p.name,
    value: p.value,
    pct: p.value_pct,
    sub: p.qty > 0 ? `${fmtQty(p.qty)} units · ${p.value_pct}%` : `${p.value_pct}%`,
  }));

  const qtyRows = (products?.by_qty ?? []).slice(0, 8).map((p) => ({
    name: p.name,
    value: p.value,
    pct: p.qty_pct,
    sub: `${fmtQty(p.qty)} units · ${p.qty_pct}% share`,
  }));

  const dealers = topDealers?.items ?? [];
  const dealersTotal = dealers.reduce((s, d) => s + d.amount, 0);
  const fyTotal = overview?.this_year_total ?? 0;
  const yoy = overview?.yoy_pct ?? null;
  const mom = pctChange(overview?.this_month_total ?? 0, overview?.last_month_till_date_total ?? 0);
  const activeDealers = dealerStats?.active ?? 0;
  const statesCovered = stateItems.length || dealerStats?.states_covered || 0;
  const avgBilling = activeDealers > 0 ? fyTotal / activeDealers : 0;
  const units = products?.total_qty ?? 0;
  // Top selling = catalog qty leader (Bench Scale merge via sales-by-product aliases).
  const topProduct = products?.by_qty?.[0] ?? null;

  const regions = useMemo(() => {
    const acc: Record<string, { sales: number; states: number }> = {
      West: { sales: 0, states: 0 },
      South: { sales: 0, states: 0 },
      North: { sales: 0, states: 0 },
      East: { sales: 0, states: 0 },
    };
    for (const row of stateItems) {
      const region = REGION_OF[row.state] || "East";
      if (!acc[region]) acc[region] = { sales: 0, states: 0 };
      acc[region].sales += row.sales_total;
      acc[region].states += 1;
    }
    return Object.entries(acc).map(([name, v]) => ({
      name,
      sales: v.sales,
      states: v.states,
      share: stateTotal > 0 ? (100 * v.sales) / stateTotal : 0,
    }));
  }, [stateItems, stateTotal]);

  const bestMonth = useMemo(() => {
    const months = overview?.months ?? [];
    if (!months.length) return null;
    return months.reduce((a, b) => (b.total > a.total ? b : a));
  }, [overview]);

  const concentration =
    fyTotal > 0 && dealers.length
      ? (100 * dealers.slice(0, 10).reduce((s, d) => s + d.amount, 0)) / fyTotal
      : null;

  const yearOptions = useMemo(() => {
    const ys: number[] = [];
    for (let y = calYear; y >= 2019; y--) ys.push(y);
    return ys;
  }, [calYear]);

  function exportCsv() {
    const lines = [
      "Metric,Value",
      `FY Total (excl GST),${fyTotal}`,
      `Units (${year}),${units}`,
      `Active Dealers,${activeDealers}`,
      `States with Sales,${statesCovered}`,
      `YoY %,${yoy ?? ""}`,
      "",
      "Rank,Dealer,Amount,Invoices",
      ...dealers.map((d, i) => `${i + 1},"${d.name.replace(/"/g, '""')}",${d.amount},${d.invoice_count}`),
      "",
      "State,Sales,Invoices",
      ...topStates.map((s) => `"${s.state}",${s.sales_total},${s.invoice_count ?? 0}`),
    ];
    const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `sales-analytics-${year}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const kpis = [
    {
      title: "Total Sales Value",
      value: fmtINRCompact(fyTotal),
      sub: yoy != null ? `${fmtPct(yoy)} YoY · FY ${overview?.fy_label || indianFyLabel()}` : `FY ${overview?.fy_label || indianFyLabel()} · excl. GST`,
      tone: yoy != null && yoy >= 0 ? "pos" : yoy != null ? "neg" : "muted",
      icon: "₹",
      iconClass: "blue",
    },
    {
      title: "Total Units Sold",
      value: fmtQty(units),
      sub: `Calendar ${year} · catalog SKUs`,
      tone: "muted",
      icon: "▣",
      iconClass: "blue",
    },
    {
      title: "Active Dealers",
      value: activeDealers ? activeDealers.toLocaleString("en-IN") : "—",
      sub: "Sale in last 6 months",
      tone: "muted",
      icon: "◎",
      iconClass: "green",
    },
    {
      title: "States Covered",
      value: String(statesCovered || "—"),
      sub: stateItems.length ? "With sales this year" : "Dealer master",
      tone: "muted",
      icon: "◉",
      iconClass: "blue",
    },
    {
      title: "Avg Dealer Billing",
      value: avgBilling ? fmtINRCompact(avgBilling) : "—",
      sub: "FY total ÷ active dealers",
      tone: "muted",
      icon: "≡",
      iconClass: "blue",
    },
    {
      title: "Top Product",
      value: topProduct?.name
        ? topProduct.name.length > 22
          ? `${topProduct.name.slice(0, 20)}…`
          : topProduct.name
        : "—",
      sub: topProduct
        ? `${fmtQty(topProduct.qty)} sold · ${fmtINRCompact(topProduct.value)}`
        : `Calendar ${year} · catalog`,
      tone: "muted",
      icon: "⬡",
      iconClass: "blue",
    },
    {
      title: "Top Segment",
      value: topCat?.category || "—",
      sub: topCat ? `Category · ${fmtINRCompact(topCat.value)}` : "No category data",
      tone: "muted",
      icon: "◆",
      iconClass: "green",
    },
    {
      title: "YoY Growth",
      value: yoy != null ? fmtPct(yoy) : "—",
      sub:
        overview?.prior_fy_label && overview.prior_fy_total
          ? `vs FY ${overview.prior_fy_label} YTD`
          : mom != null
            ? `MoM ${fmtPct(mom)} (same days)`
            : "Need prior FY",
      tone: yoy != null && yoy >= 0 ? "pos" : yoy != null ? "neg" : "muted",
      icon: "↗",
      iconClass: "green",
    },
  ];

  return (
    <div className="sa-page">
      <div className="heading sd-heading">
        <div>
          <h1>Sales Analytics</h1>
          <p>State-wise, product-wise, dealer-wise and segment-wise business insights.</p>
        </div>
        <div className="sd-actions sa-actions">
          <label className="sd-range">
            <span className="sd-range-ico" aria-hidden>
              📅
            </span>
            <select value={year} onChange={(e) => setYear(Number(e.target.value))} aria-label="Year">
              {yearOptions.map((y) => (
                <option key={y} value={y}>
                  {y}
                </option>
              ))}
            </select>
          </label>
          <button type="button" className="btn primary" onClick={exportCsv}>
            Export Report
          </button>
        </div>
      </div>

      {loading && <p className="sd-loading">Loading sales analytics…</p>}
      {err && !loading && <p className="sa-err">{err}</p>}

      <div className="sa-kpis">
        {kpis.map((k) => (
          <div key={k.title} className="sd-kpi sa-kpi">
            <div className="sd-kpi-top">
              <span className="sd-kpi-title">{k.title}</span>
              <span className={`sd-kpi-ico ${k.iconClass}`}>{k.icon}</span>
            </div>
            <div className="sd-kpi-val">{k.value}</div>
            <div className={`sd-kpi-sub ${k.tone}`}>{k.sub}</div>
          </div>
        ))}
      </div>

      <div className="sa-row sa-row-2">
        <section className="panel sd-card">
          <div className="panelhead">
            <div>
              <h2>Sales Trend</h2>
              <p>Last 12 months · excl. GST</p>
            </div>
            {yoy != null && (
              <span className={`sa-badge ${yoy >= 0 ? "pos" : "neg"}`}>YoY {fmtPct(yoy)}</span>
            )}
          </div>
          <div className="sa-pad">
            <TrendArea points={trendPoints} />
          </div>
        </section>

        <section className="panel sd-card">
          <div className="panelhead">
            <div>
              <h2>State-wise Sales</h2>
              <p>
                {year} · excl. GST · {fmtINRCompact(stateTotal)}
              </p>
            </div>
          </div>
          <div className="sa-state-grid">
            <IndiaSalesMap items={stateItems} />
            <ol className="sa-state-rank">
              {topStates.length === 0 && <li className="sa-empty">No state sales</li>}
              {topStates.map((s, i) => (
                <li key={s.state}>
                  <span className="sa-rank">{i + 1}</span>
                  <span className="sa-state-name">{s.state}</span>
                  <span className="sa-state-val">{fmtINRCompact(s.sales_total)}</span>
                  <span className="sa-state-share">
                    {stateTotal > 0 ? `${((100 * s.sales_total) / stateTotal).toFixed(1)}%` : "—"}
                  </span>
                </li>
              ))}
            </ol>
          </div>
        </section>
      </div>

      <div className="sa-row sa-row-2">
        <section className="panel sd-card">
          <div className="panelhead">
            <div>
              <h2>Product-wise Sales (by Value)</h2>
              <p>
                Top SKUs · {year} · {fmtINRCompact(products?.total_value)}
              </p>
            </div>
          </div>
          <div className="sa-pad">
            <HBarList rows={productRows} />
          </div>
        </section>

        <section className="panel sd-card">
          <div className="panelhead">
            <div>
              <h2>Product-wise Quantity</h2>
              <p>
                Catalog SKUs · {year} · {fmtQty(products?.total_qty)} units
              </p>
            </div>
          </div>
          <div className="sa-pad">
            <HBarList rows={qtyRows} />
          </div>
        </section>
      </div>

      <div className="sa-row sa-row-3">
        <section className="panel sd-card">
          <div className="panelhead">
            <div>
              <h2>Segment-wise Sales</h2>
              <p>Product categories · {year}</p>
            </div>
          </div>
          <div className="sa-pad">
            <MultiDonut
              items={donutItems}
              centerValue={fmtINRCompact(categories?.total_value)}
              centerLabel="Total"
            />
          </div>
        </section>

        <section className="panel sd-card">
          <div className="panelhead">
            <div>
              <h2>Dealer-wise Performance</h2>
              <p>
                Top 5 · FY {topDealers?.fy_label || indianFyLabel(topDealers?.year ?? fyYear)} · excl. GST
              </p>
            </div>
          </div>
          <div className="sa-table-wrap">
            <table className="sa-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Dealer</th>
                  <th>Sales</th>
                  <th>Invoices</th>
                </tr>
              </thead>
              <tbody>
                {dealers.slice(0, 5).map((d, i) => (
                  <tr key={d.dealer_id}>
                    <td>{i + 1}</td>
                    <td title={d.name}>{d.name}</td>
                    <td>{fmtINRCompact(d.amount)}</td>
                    <td>{d.invoice_count}</td>
                  </tr>
                ))}
                {!dealers.length && (
                  <tr>
                    <td colSpan={4} className="sa-empty">
                      No dealer sales this FY
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel sd-card">
          <div className="panelhead">
            <div>
              <h2>Monthly Trend</h2>
              <p>Bars = sales value · line = invoices</p>
            </div>
          </div>
          <div className="sa-pad">
            <MonthlyBars points={monthlyPoints} />
            <div className="sa-chart-legend">
              <span>
                <i className="bar" /> Sales value
              </span>
              <span>
                <i className="line" /> Invoice count
              </span>
            </div>
          </div>
        </section>
      </div>

      <div className="sa-row sa-row-3">
        <section className="panel sd-card">
          <div className="panelhead">
            <div>
              <h2>Category Contribution</h2>
              <p>Share of line-item value · {year}</p>
            </div>
          </div>
          <div className="sa-pad">
            <ul className="sa-cat-bars">
              {catItems.map((c, i) => (
                <li key={c.category}>
                  <div className="sa-cat-meta">
                    <span>{c.category}</span>
                    <b>{c.value_pct}%</b>
                  </div>
                  <div className="sa-hbar-track">
                    <div
                      className="sa-hbar-fill"
                      style={{
                        width: `${Math.min(100, c.value_pct)}%`,
                        background: CAT_COLORS[i % CAT_COLORS.length],
                      }}
                    />
                  </div>
                </li>
              ))}
              {!catItems.length && <li className="sa-empty">No category data</li>}
            </ul>
          </div>
        </section>

        <section className="panel sd-card">
          <div className="panelhead">
            <div>
              <h2>Regional Summary</h2>
              <p>Rolled up from state sales · {year}</p>
            </div>
          </div>
          <div className="sa-table-wrap">
            <table className="sa-table">
              <thead>
                <tr>
                  <th>Region</th>
                  <th>Sales</th>
                  <th>States</th>
                  <th>Share</th>
                </tr>
              </thead>
              <tbody>
                {regions.map((r) => (
                  <tr key={r.name}>
                    <td>{r.name}</td>
                    <td>{fmtINRCompact(r.sales)}</td>
                    <td>{r.states}</td>
                    <td>{r.share.toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel sd-card">
          <div className="panelhead">
            <div>
              <h2>Low-Sales States</h2>
              <p>Smallest positive sales · {year}</p>
            </div>
          </div>
          <div className="sa-table-wrap">
            <table className="sa-table">
              <thead>
                <tr>
                  <th>State</th>
                  <th>Sales</th>
                </tr>
              </thead>
              <tbody>
                {lowStates.map((s) => (
                  <tr key={s.state}>
                    <td>{s.state}</td>
                    <td>{fmtINRCompact(s.sales_total)}</td>
                  </tr>
                ))}
                {!lowStates.length && (
                  <tr>
                    <td colSpan={2} className="sa-empty">
                      No state data
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <div className="sa-row sa-row-bottom">
        <section className="panel sd-card">
          <div className="panelhead">
            <div>
              <h2>Top 10 Dealers</h2>
              <p>
                FY {topDealers?.fy_label || indianFyLabel()} · excl. GST
                {dealersTotal ? ` · ${fmtINRCompact(dealersTotal)} shown` : ""}
              </p>
            </div>
          </div>
          <div className="sa-table-wrap">
            <table className="sa-table sa-table-dealers">
              <thead>
                <tr>
                  <th>Rank</th>
                  <th>Dealer</th>
                  <th>Sales Value</th>
                  <th>Invoices</th>
                  <th>% of FY</th>
                </tr>
              </thead>
              <tbody>
                {dealers.map((d, i) => (
                  <tr key={d.dealer_id}>
                    <td>{i + 1}</td>
                    <td title={d.name}>{d.name}</td>
                    <td>{fmtINRCompact(d.amount)}</td>
                    <td>{d.invoice_count}</td>
                    <td>{fyTotal > 0 ? `${((100 * d.amount) / fyTotal).toFixed(1)}%` : "—"}</td>
                  </tr>
                ))}
                {!dealers.length && (
                  <tr>
                    <td colSpan={5} className="sa-empty">
                      No dealers
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel sd-card sa-insights">
          <div className="panelhead">
            <div>
              <h2>Insights / Highlights</h2>
              <p>Derived from live FY and {year} data</p>
            </div>
          </div>
          <ul className="sa-insight-grid">
            <li>
              <span>Top State</span>
              <strong>{topStates[0]?.state || "—"}</strong>
              <small>{topStates[0] ? fmtINRCompact(topStates[0].sales_total) : ""}</small>
            </li>
            <li>
              <span>Top Product</span>
              <strong>{topProduct?.name || "—"}</strong>
              <small>
                {topProduct
                  ? `${fmtQty(topProduct.qty)} sold · ${fmtINRCompact(topProduct.value)}`
                  : ""}
              </small>
            </li>
            <li>
              <span>Top Segment</span>
              <strong>{topCat?.category || "—"}</strong>
              <small>{topCat ? `Category · ${fmtINRCompact(topCat.value)}` : ""}</small>
            </li>
            <li>
              <span>Best Month</span>
              <strong>{bestMonth?.label || "—"}</strong>
              <small>{bestMonth ? fmtINRCompact(bestMonth.total) : ""}</small>
            </li>
            <li>
              <span>Active Dealers</span>
              <strong>{activeDealers ? activeDealers.toLocaleString("en-IN") : "—"}</strong>
              <small>Last 6 months</small>
            </li>
            <li>
              <span>Concentration</span>
              <strong>{concentration != null ? `${concentration.toFixed(1)}%` : "—"}</strong>
              <small>Top 10 dealers of FY</small>
            </li>
            <li>
              <span>This Month</span>
              <strong>{fmtINRCompact(overview?.this_month_total)}</strong>
              <small>{mom != null ? `${fmtPct(mom)} vs prior MTD` : "MTD"}</small>
            </li>
          </ul>
          <p className="sa-takeaway">
            {fyTotal > 0 ? (
              <>
                FY {overview?.fy_label || indianFyLabel()} sales reached{" "}
                <b>{fmtINRCompact(fyTotal)}</b> (excl. GST)
                {yoy != null ? (
                  <>
                    , {yoy >= 0 ? "up" : "down"} <b>{fmtPct(Math.abs(yoy))}</b> vs prior FY YTD
                  </>
                ) : null}
                {topStates[0] ? (
                  <>
                    . Led by <b>{topStates[0].state}</b>
                  </>
                ) : null}
                {topCat ? (
                  <>
                    {" "}
                    and <b>{topCat.category}</b>
                  </>
                ) : null}
                .
              </>
            ) : (
              <>Load sales data to generate highlights.</>
            )}
          </p>
        </section>
      </div>
    </div>
  );
}
