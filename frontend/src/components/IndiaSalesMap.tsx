import { useMemo, useState } from "react";
import { INDIA_MAP_VIEWBOX, INDIA_STATE_PATHS, mapStateKey } from "../data/indiaStatePaths";

export type StateSales = { state: string; sales_total: number };

function fillForSales(sales: number, max: number): string {
  if (!sales || max <= 0) return "#e8eef8";
  const t = Math.min(1, Math.max(0.12, sales / max));
  // blue intensity: light → primary
  const r = Math.round(219 - t * (219 - 37));
  const g = Math.round(234 - t * (234 - 99));
  const b = Math.round(254 - t * (254 - 235));
  return `rgb(${r},${g},${b})`;
}

function fmtInrShort(n: number) {
  if (!n) return "₹0";
  if (n >= 1e7) return `₹${(n / 1e7).toFixed(2)} Cr`;
  if (n >= 1e5) return `₹${(n / 1e5).toFixed(2)} L`;
  return `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

export default function IndiaSalesMap({ items }: { items: StateSales[] }) {
  const [hover, setHover] = useState<string | null>(null);

  const byState = useMemo(() => {
    const m = new Map<string, number>();
    for (const row of items) {
      const key = mapStateKey(row.state);
      m.set(key, (m.get(key) || 0) + row.sales_total);
    }
    return m;
  }, [items]);

  const max = useMemo(() => Math.max(0, ...byState.values()), [byState]);
  const tipSales = hover ? byState.get(hover) || 0 : 0;

  return (
    <div className="sd-india-map">
      <svg viewBox={INDIA_MAP_VIEWBOX} role="img" aria-label="India dealer sales by state">
        {INDIA_STATE_PATHS.map((st) => {
          const sales = byState.get(st.name) || 0;
          const active = hover === st.name;
          return (
            <path
              key={st.id}
              d={st.d}
              fill={fillForSales(sales, max)}
              stroke={active ? "#1e3a8a" : "#94a3b8"}
              strokeWidth={active ? 1.4 : 0.6}
              className="sd-india-state"
              onMouseEnter={() => setHover(st.name)}
              onMouseLeave={() => setHover(null)}
            >
              <title>
                {st.name}
                {sales ? ` · ${fmtInrShort(sales)}` : " · no sales"}
              </title>
            </path>
          );
        })}
      </svg>
      <div className="sd-india-tip" aria-live="polite">
        {hover ? (
          <>
            <b>{hover}</b>
            <span>{fmtInrShort(tipSales)}</span>
          </>
        ) : (
          <span className="muted">Hover a state</span>
        )}
      </div>
    </div>
  );
}
