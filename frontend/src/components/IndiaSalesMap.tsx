import { useMemo, useState } from "react";
import { INDIA_MAP_VIEWBOX, INDIA_STATE_PATHS, mapStateKey } from "../data/indiaStatePaths";

export type StateSales = { state: string; sales_total?: number; lead_count?: number };

function metric(row: StateSales): number {
  return row.sales_total ?? row.lead_count ?? 0;
}

function fillForMetric(value: number, max: number): string {
  if (!value || max <= 0) return "#e8eef8";
  const t = Math.min(1, Math.max(0.12, value / max));
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

function fmtCount(n: number) {
  return n.toLocaleString("en-IN");
}

type Props = {
  items: StateSales[];
  mode?: "sales" | "leads";
  selectedMapState?: string | null;
  /** Highlight all member states when hovering a merged lead tile. */
  highlightMapStates?: string[];
  onSelectState?: (mapStateName: string) => void;
  /** Larger map for map-only overview (e.g. Leads landing). */
  layout?: "default" | "hero";
};

export default function IndiaSalesMap({
  items,
  mode = "sales",
  selectedMapState = null,
  highlightMapStates,
  onSelectState,
  layout = "default",
}: Props) {
  const highlightSet = useMemo(
    () => new Set(highlightMapStates ?? []),
    [highlightMapStates]
  );
  const [hover, setHover] = useState<string | null>(null);
  const interactive = Boolean(onSelectState);

  const byState = useMemo(() => {
    const m = new Map<string, number>();
    for (const row of items) {
      const key = mapStateKey(row.state);
      m.set(key, (m.get(key) || 0) + metric(row));
    }
    return m;
  }, [items]);

  const max = useMemo(() => Math.max(0, ...byState.values()), [byState]);
  const tipState = hover ?? selectedMapState;
  const tipVal = tipState ? byState.get(tipState) || 0 : 0;

  const format = mode === "leads" ? fmtCount : fmtInrShort;

  return (
    <div className={`sd-india-map${layout === "hero" ? " sd-india-map--hero" : ""}`}>
      <svg
        viewBox={INDIA_MAP_VIEWBOX}
        role="img"
        aria-label={mode === "leads" ? "India leads by state" : "India dealer sales by state"}
      >
        {INDIA_STATE_PATHS.map((st) => {
          const value = byState.get(st.name) || 0;
          const active =
            hover === st.name ||
            selectedMapState === st.name ||
            highlightSet.has(st.name);
          return (
            <path
              key={st.id}
              d={st.d}
              fill={fillForMetric(value, max)}
              stroke={active ? "#1e3a8a" : "#94a3b8"}
              strokeWidth={active ? 1.4 : 0.6}
              className={`sd-india-state${interactive ? " sd-india-state--click" : ""}`}
              onMouseEnter={() => setHover(st.name)}
              onMouseLeave={() => setHover(null)}
              onClick={
                interactive
                  ? () => {
                      onSelectState?.(st.name);
                    }
                  : undefined
              }
            >
              <title>
                {st.name}
                {value
                  ? ` · ${format(value)} ${mode === "leads" ? "leads" : ""}`.trim()
                  : mode === "leads"
                    ? " · no leads"
                    : " · no sales"}
              </title>
            </path>
          );
        })}
      </svg>
      <div className="sd-india-tip" aria-live="polite">
        {tipState ? (
          <>
            <b>{tipState}</b>
            <span>
              {format(tipVal)}
              {mode === "leads" ? " leads" : ""}
            </span>
          </>
        ) : (
          <span className={layout === "hero" && interactive ? "sd-india-tip-hint" : "muted"}>
            {layout === "hero" && interactive ? (
              <>
                <span className="sd-india-tip-info" aria-hidden>
                  i
                </span>
                Hover for counts · click a state to open leads
              </>
            ) : interactive ? (
              "Hover or click a state"
            ) : (
              "Hover a state"
            )}
          </span>
        )}
      </div>
    </div>
  );
}