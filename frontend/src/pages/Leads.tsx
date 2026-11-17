import { FormEvent, useEffect, useMemo, useState } from "react";
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
  pin: string;
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

type PincodeLookup = {
  pin: string;
  city: string;
  district: string;
  state: string;
  office?: string;
};

type EditForm = {
  name: string;
  phone: string;
  pin: string;
  city: string;
  district: string;
  state: string;
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

function emptyForm(): EditForm {
  return { name: "", phone: "", pin: "", city: "", district: "", state: "" };
}

function formFromLead(lead: Lead): EditForm {
  return {
    name: lead.name || "",
    phone: lead.phone || "",
    pin: lead.pin || "",
    city: lead.city || "",
    district: lead.district || "",
    state: lead.state || "",
  };
}

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

  const [editing, setEditing] = useState<Lead | null>(null);
  const [form, setForm] = useState<EditForm>(emptyForm);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [pinHint, setPinHint] = useState("");
  const [pinLoading, setPinLoading] = useState(false);

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

  useEffect(() => {
    if (!editing) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !saving) closeEdit();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [editing, saving]);

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

  function openEdit(lead: Lead) {
    setEditing(lead);
    setForm(formFromLead(lead));
    setSaveError("");
    setPinHint("");
  }

  function closeEdit(force = false) {
    if (saving && !force) return;
    setEditing(null);
    setForm(emptyForm());
    setSaveError("");
    setPinHint("");
    setPinLoading(false);
  }

  function setField<K extends keyof EditForm>(key: K, value: EditForm[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  async function lookupPin(raw: string) {
    const pin = raw.replace(/\D/g, "").slice(0, 6);
    if (pin.length !== 6) {
      setPinHint("");
      return;
    }
    setPinLoading(true);
    setPinHint("Looking up PIN…");
    try {
      const data = await api<PincodeLookup>(`/api/pincode/${pin}`);
      setForm((prev) => ({
        ...prev,
        pin,
        city: data.city || prev.city,
        district: data.district || prev.district,
        state: data.state || prev.state,
      }));
      const bits = [data.city, data.district, data.state].filter(Boolean);
      setPinHint(bits.length ? `Filled from PIN: ${bits.join(", ")}` : "PIN found");
    } catch (e) {
      setPinHint(e instanceof Error ? e.message : "PIN lookup failed");
    } finally {
      setPinLoading(false);
    }
  }

  function onPinChange(value: string) {
    const pin = value.replace(/\D/g, "").slice(0, 6);
    setField("pin", pin);
    setPinHint("");
    if (pin.length === 6) {
      lookupPin(pin).catch(console.error);
    }
  }

  async function saveEdit(e: FormEvent) {
    e.preventDefault();
    if (!editing) return;
    const name = form.name.trim();
    if (!name && !(editing.company || "").trim()) {
      setSaveError("Name is required");
      return;
    }
    setSaving(true);
    setSaveError("");
    try {
      const updated = await api<Lead>(`/api/leads/${editing.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          name,
          phone: form.phone.trim(),
          pin: form.pin.trim(),
          city: form.city.trim(),
          district: form.district.trim(),
          state: form.state.trim(),
        }),
      });
      setRows((prev) => prev.map((r) => (r.id === updated.id ? { ...r, ...updated } : r)));
      setSaving(false);
      closeEdit(true);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Save failed");
      setSaving(false);
    }
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
              <tr
                key={r.id}
                className="lead-row"
                title="Double-click to edit"
                onDoubleClick={() => openEdit(r)}
              >
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

      {editing ? (
        <div
          className="emp-overlay"
          onClick={(e) => {
            if (e.target === e.currentTarget) closeEdit();
          }}
        >
          <section
            className="emp-drawer lead-edit-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="lead-edit-title"
          >
            <div className="emp-drawer-head">
              <div>
                <h2 id="lead-edit-title">Edit lead</h2>
                <p>Update contact details. Enter a 6-digit PIN to autofill location.</p>
              </div>
              <button type="button" className="iconbtn" onClick={() => closeEdit()} aria-label="Close" disabled={saving}>
                ✕
              </button>
            </div>
            <form className="emp-form" onSubmit={saveEdit}>
              <div className="emp-formbody">
                <div className="emp-fields lead-edit-fields">
                  <div className="field full">
                    <label htmlFor="lead-edit-name">Name</label>
                    <input
                      id="lead-edit-name"
                      value={form.name}
                      onChange={(e) => setField("name", e.target.value)}
                      autoFocus
                      required
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="lead-edit-phone">Phone</label>
                    <input
                      id="lead-edit-phone"
                      value={form.phone}
                      onChange={(e) => setField("phone", e.target.value)}
                      inputMode="tel"
                      autoComplete="tel"
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="lead-edit-pin">PIN</label>
                    <input
                      id="lead-edit-pin"
                      value={form.pin}
                      onChange={(e) => onPinChange(e.target.value)}
                      onBlur={() => {
                        if (form.pin.replace(/\D/g, "").length === 6) {
                          lookupPin(form.pin).catch(console.error);
                        }
                      }}
                      inputMode="numeric"
                      maxLength={6}
                      placeholder="6-digit PIN"
                      aria-describedby="lead-edit-pin-hint"
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="lead-edit-city">City</label>
                    <input
                      id="lead-edit-city"
                      value={form.city}
                      onChange={(e) => setField("city", e.target.value)}
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="lead-edit-district">District</label>
                    <input
                      id="lead-edit-district"
                      value={form.district}
                      onChange={(e) => setField("district", e.target.value)}
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="lead-edit-state">State</label>
                    <input
                      id="lead-edit-state"
                      value={form.state}
                      onChange={(e) => setField("state", e.target.value)}
                      list="lead-edit-states"
                    />
                    <datalist id="lead-edit-states">
                      {INDIA_STATES.map((s) => (
                        <option key={s} value={s} />
                      ))}
                    </datalist>
                  </div>
                </div>
                <p id="lead-edit-pin-hint" className={`lead-pin-hint${pinLoading ? " loading" : ""}`}>
                  {pinHint || "City, district, and state can be filled manually or from PIN."}
                </p>
                {saveError ? <p className="lead-edit-error">{saveError}</p> : null}
              </div>
              <div className="emp-formfoot">
                <small className="lead-edit-footnote">Double-click any row to open this editor.</small>
                <div className="actions">
                  <button type="button" className="btn" onClick={() => closeEdit()} disabled={saving}>
                    Cancel
                  </button>
                  <button type="submit" className="btn primary" disabled={saving || pinLoading}>
                    {saving ? "Saving…" : "Save"}
                  </button>
                </div>
              </div>
            </form>
          </section>
        </div>
      ) : null}
    </>
  );
}
