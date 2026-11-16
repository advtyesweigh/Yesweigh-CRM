import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { INDIA_STATES, districtsForState } from "../data/indiaGeo";

type Lead = {
  id: number;
  name: string;
  phone: string;
  email: string;
  company: string;
  city: string;
  district: string;
  state: string;
  status: string;
  source: string;
  notes: string;
};

type LeadPage = {
  items: Lead[];
  total: number;
  page: number;
  page_size: number;
  status_counts?: Record<string, number>;
};

const PAGE_SIZE = 50;

const STATUS_TILES = [
  { key: "all", label: "All" },
  { key: "new", label: "New" },
  { key: "contacted", label: "Contacted" },
  { key: "qualified", label: "Qualified" },
  { key: "won", label: "Won" },
  { key: "lost", label: "Lost" },
] as const;

type LoadOpts = {
  page?: number;
  q?: string;
  status?: string;
  state?: string;
  district?: string;
};

export default function Leads() {
  const [rows, setRows] = useState<Lead[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [stateFilter, setStateFilter] = useState("");
  const [districtFilter, setDistrictFilter] = useState("");
  const [statusCounts, setStatusCounts] = useState<Record<string, number>>({});

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const districtOptions = useMemo(() => districtsForState(stateFilter), [stateFilter]);
  const hasGeoFilter = Boolean(stateFilter || districtFilter);

  async function load(opts: LoadOpts = {}) {
    const nextPage = opts.page ?? page;
    const q = opts.q ?? query;
    const status = opts.status ?? statusFilter;
    const state = opts.state ?? stateFilter;
    const district = opts.district ?? districtFilter;

    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams({
        page: String(nextPage),
        page_size: String(PAGE_SIZE),
      });
      const term = q.trim();
      if (term) params.set("q", term);
      if (status && status !== "all") params.set("status", status);
      if (state.trim()) params.set("state", state.trim());
      if (district.trim()) params.set("district", district.trim());
      const data = await api<LeadPage>(`/api/leads?${params}`);
      setRows(data.items);
      setTotal(data.total);
      setPage(data.page);
      setStatusCounts(data.status_counts || {});
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load leads");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load({ page: 1, q: "", status: "all", state: "", district: "" }).catch(console.error);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const t = window.setTimeout(() => {
      const next = searchInput.trim();
      if (next === query) return;
      setQuery(next);
      load({ page: 1, q: next }).catch(console.error);
    }, 300);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchInput]);

  function selectStatus(key: string) {
    if (key === statusFilter) return;
    setStatusFilter(key);
    load({ page: 1, status: key }).catch(console.error);
  }

  function onStateChange(value: string) {
    setStateFilter(value);
    setDistrictFilter("");
    load({ page: 1, state: value, district: "" }).catch(console.error);
  }

  function onDistrictChange(value: string) {
    setDistrictFilter(value);
    load({ page: 1, district: value }).catch(console.error);
  }

  function clearFilters() {
    setSearchInput("");
    setQuery("");
    setStatusFilter("all");
    setStateFilter("");
    setDistrictFilter("");
    load({ page: 1, q: "", status: "all", state: "", district: "" }).catch(console.error);
  }

  const from = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const to = Math.min(page * PAGE_SIZE, total);
  const allCount = Object.values(statusCounts).reduce((a, b) => a + b, 0);
  const filtersActive = Boolean(query || statusFilter !== "all" || hasGeoFilter);

  return (
    <>
      {error ? <p style={{ color: "#b91c1c", marginBottom: 12 }}>{error}</p> : null}

      <div className="lead-status-tiles">
        {STATUS_TILES.map((t) => {
          const count = t.key === "all" ? allCount : statusCounts[t.key] ?? 0;
          const active = statusFilter === t.key;
          return (
            <button
              key={t.key}
              type="button"
              className={`lead-status-tile${active ? " active" : ""}`}
              onClick={() => selectStatus(t.key)}
            >
              <span className="lead-status-label">{t.label}</span>
              <span className="lead-status-count">{count}</span>
            </button>
          );
        })}
      </div>

      <div className="panel">
        <div className="lead-toolbar">
          <input
            className="lead-search"
            placeholder="Search name, phone, city, district, state…"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            aria-label="Search leads"
          />
          <select
            className="lead-select"
            value={stateFilter}
            onChange={(e) => onStateChange(e.target.value)}
            aria-label="Filter by state"
          >
            <option value="">All states</option>
            {INDIA_STATES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <select
            className="lead-select"
            value={districtFilter}
            onChange={(e) => onDistrictChange(e.target.value)}
            aria-label="Filter by district"
            disabled={!stateFilter}
          >
            <option value="">{stateFilter ? "All districts" : "Select state first"}</option>
            {districtOptions.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
          <button type="button" className="lead-clear" disabled={!filtersActive && !loading} onClick={clearFilters}>
            Clear
          </button>
          <strong className="lead-range">{loading ? "Loading…" : `${from}–${to} of ${total}`}</strong>
          <div className="lead-pager">
            <button
              type="button"
              disabled={loading || page <= 1}
              onClick={() => load({ page: page - 1 })}
            >
              Prev
            </button>
            <span>
              Page {page} / {pageCount}
            </span>
            <button
              type="button"
              disabled={loading || page >= pageCount}
              onClick={() => load({ page: page + 1 })}
            >
              Next
            </button>
          </div>
        </div>
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Phone</th>
              <th>City</th>
              <th>District</th>
              <th>State</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.name || r.company || "—"}</td>
                <td>{r.phone || "—"}</td>
                <td>{r.city || "—"}</td>
                <td>{r.district || "—"}</td>
                <td>{r.state || "—"}</td>
              </tr>
            ))}
            {!loading && rows.length === 0 ? (
              <tr>
                <td colSpan={5} style={{ color: "#64748b" }}>
                  {filtersActive ? "No leads match your filters." : "No leads yet."}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </>
  );
}
