import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "../api/client";

type AudienceSource = "dealers" | "leads";

type AudienceItem = {
  id: number;
  uid?: number | null;
  name: string;
  source: AudienceSource | string;
  description: string;
  filters: Record<string, string>;
  filter_summary: string;
  count: number;
  last_count?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
};

function fmtAudienceUid(uid?: number | null): string {
  if (uid == null || !Number.isFinite(uid)) return "—";
  return String(Math.trunc(uid)).padStart(2, "0");
}

type AudienceMember = {
  id: number;
  name: string;
  phone: string;
  company: string;
  city: string;
  district: string;
  state: string;
};

type Preview = {
  audience_id: number;
  uid?: number | null;
  name: string;
  source: string;
  filters: Record<string, string>;
  filter_summary: string;
  total: number;
  with_phone: number;
  items: AudienceMember[];
  truncated: boolean;
};

type EditForm = {
  name: string;
  source: AudienceSource;
  description: string;
  state: string;
  city: string;
  district: string;
};

function emptyForm(): EditForm {
  return { name: "", source: "dealers", description: "", state: "", city: "", district: "" };
}

function formFromAudience(a: AudienceItem): EditForm {
  return {
    name: a.name || "",
    source: a.source === "leads" ? "leads" : "dealers",
    description: a.description || "",
    state: a.filters?.state || "",
    city: a.filters?.city || "",
    district: a.filters?.district || "",
  };
}

function sourceLabel(source: string): string {
  return source === "leads" ? "Leads" : "Dealers";
}

export default function Audience() {
  const [rows, setRows] = useState<AudienceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<AudienceItem | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<EditForm>(emptyForm());
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await api<AudienceItem[]>("/api/audiences");
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load audiences");
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load().catch(console.error);
  }, [load]);

  function openCreate() {
    setCreating(true);
    setEditing(null);
    setForm(emptyForm());
    setSaveError("");
    setPreview(null);
  }

  function openEdit(row: AudienceItem) {
    setEditing(row);
    setCreating(false);
    setForm(formFromAudience(row));
    setSaveError("");
    setPreview(null);
  }

  function closeDrawer() {
    setCreating(false);
    setEditing(null);
    setSaveError("");
  }

  function setField<K extends keyof EditForm>(key: K, value: EditForm[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    const name = form.name.trim();
    if (!name) {
      setSaveError("Name is required");
      return;
    }
    const filters: Record<string, string> = {};
    if (form.state.trim()) filters.state = form.state.trim();
    if (form.city.trim()) filters.city = form.city.trim();
    if (form.district.trim()) filters.district = form.district.trim();
    // Preserve static phone-list filters when editing via geo-only UI
    if (editing?.filters?.phones) {
      filters.phones = editing.filters.phones;
    }

    setSaving(true);
    setSaveError("");
    try {
      if (editing) {
        await api<AudienceItem>(`/api/audiences/${editing.id}`, {
          method: "PATCH",
          body: JSON.stringify({
            name,
            source: form.source,
            description: form.description.trim(),
            filters,
          }),
        });
      } else {
        await api<AudienceItem>("/api/audiences", {
          method: "POST",
          body: JSON.stringify({
            name,
            source: form.source,
            description: form.description.trim(),
            filters,
          }),
        });
      }
      closeDrawer();
      await load();
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function remove(row: AudienceItem) {
    if (!window.confirm(`Delete audience “${row.name}”?`)) return;
    try {
      await api(`/api/audiences/${row.id}`, { method: "DELETE" });
      if (editing?.id === row.id) closeDrawer();
      if (preview?.audience_id === row.id) setPreview(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    }
  }

  async function showMembers(row: AudienceItem) {
    setPreviewLoading(true);
    setError("");
    try {
      const data = await api<Preview>(`/api/audiences/${row.id}/members?limit=50`);
      setPreview(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Preview failed");
    } finally {
      setPreviewLoading(false);
    }
  }

  const drawerOpen = creating || editing != null;

  return (
    <>
      <div className="heading">
        <div>
          <h1>Audience</h1>
          <p>Custom audiences from Dealers and Leads · live counts</p>
        </div>
        <button type="button" className="btn primary" onClick={openCreate}>
          New audience
        </button>
      </div>

      {error ? <p style={{ color: "#b91c1c", marginBottom: 12 }}>{error}</p> : null}

      <div className="panel">
        <div className="panelhead">
          <div>
            <h2>Custom audiences</h2>
            <p>
              Filters resolve against left-menu Dealer or Leads at read time — not a frozen snapshot.
              Create and edit via this page or AI Agent.
            </p>
          </div>
          <button type="button" className="btn" onClick={() => load()} disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </button>
        </div>

        {loading && rows.length === 0 ? (
          <div style={{ padding: 20, color: "var(--muted)" }}>Loading audiences…</div>
        ) : rows.length === 0 ? (
          <div style={{ padding: 20, color: "var(--muted)", lineHeight: 1.6 }}>
            No custom audiences yet. Create one (e.g. <strong>Dealer Kerala</strong> = Dealers where
            state is Kerala) or ask the AI Agent.
          </div>
        ) : (
          <div className="adms-table-wrap">
            <table className="adms-att-table">
              <thead>
                <tr>
                  <th style={{ width: 64 }}>ID</th>
                  <th>Name</th>
                  <th>Source</th>
                  <th>Filters</th>
                  <th style={{ textAlign: "right" }}>Count</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td
                      style={{
                        fontVariantNumeric: "tabular-nums",
                        fontWeight: 700,
                        letterSpacing: "0.02em",
                      }}
                    >
                      {fmtAudienceUid(row.uid)}
                    </td>
                    <td>
                      <div style={{ fontWeight: 650 }}>{row.name}</div>
                      {row.description ? (
                        <div style={{ color: "var(--muted)", fontSize: 13, marginTop: 2 }}>
                          {row.description}
                        </div>
                      ) : null}
                    </td>
                    <td>
                      <span className="aud-source-pill">{sourceLabel(row.source)}</span>
                    </td>
                    <td style={{ color: "var(--muted)", fontSize: 13 }}>
                      {row.filter_summary || "—"}
                    </td>
                    <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums", fontWeight: 650 }}>
                      {row.count.toLocaleString("en-IN")}
                    </td>
                    <td>
                      <div className="aud-actions">
                        <button type="button" className="btn" onClick={() => showMembers(row)} disabled={previewLoading}>
                          Preview
                        </button>
                        <button type="button" className="btn" onClick={() => openEdit(row)}>
                          Edit
                        </button>
                        <button type="button" className="btn" onClick={() => remove(row)}>
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {preview ? (
        <div className="panel">
          <div className="panelhead">
            <div>
              <h2>
                Preview · {preview.uid != null ? `${fmtAudienceUid(preview.uid)} · ` : ""}
                {preview.name}
              </h2>
              <p>
                {sourceLabel(preview.source)} · {preview.filter_summary} ·{" "}
                {preview.total.toLocaleString("en-IN")} members
                {preview.with_phone != null
                  ? ` · ${preview.with_phone.toLocaleString("en-IN")} with phone`
                  : ""}
                {preview.truncated ? " · showing first page" : ""}
              </p>
            </div>
            <button type="button" className="btn" onClick={() => setPreview(null)}>
              Close
            </button>
          </div>
          <div className="adms-table-wrap">
            <table className="adms-att-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Phone</th>
                  <th>City</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {preview.items.map((m) => (
                  <tr key={`${preview.audience_id}-${m.id}`}>
                    <td>{m.name}</td>
                    <td>
                      <code>{m.phone || "—"}</code>
                    </td>
                    <td>{m.city || m.district || "—"}</td>
                    <td>{m.state || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {drawerOpen ? (
        <div className="aud-drawer-backdrop" onClick={closeDrawer} role="presentation">
          <aside
            className="aud-drawer"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-label={editing ? "Edit audience" : "New audience"}
          >
            <div className="aud-drawer-head">
              <h2>{editing ? "Edit audience" : "New audience"}</h2>
              <button type="button" className="btn" onClick={closeDrawer}>
                Close
              </button>
            </div>
            <form className="aud-drawer-form" onSubmit={save}>
              {editing?.uid != null ? (
                <label>
                  ID
                  <input value={fmtAudienceUid(editing.uid)} readOnly disabled />
                </label>
              ) : null}
              <label>
                Name
                <input
                  value={form.name}
                  onChange={(e) => setField("name", e.target.value)}
                  placeholder="e.g. Dealer Kerala"
                  required
                />
              </label>
              <label>
                Source
                <select
                  value={form.source}
                  onChange={(e) => setField("source", e.target.value as AudienceSource)}
                >
                  <option value="dealers">Dealers (left menu)</option>
                  <option value="leads">Leads (left menu)</option>
                </select>
              </label>
              <label>
                State
                <input
                  value={form.state}
                  onChange={(e) => setField("state", e.target.value)}
                  placeholder="e.g. Kerala"
                />
              </label>
              <label>
                City <span className="aud-optional">(optional)</span>
                <input
                  value={form.city}
                  onChange={(e) => setField("city", e.target.value)}
                  placeholder="Optional"
                />
              </label>
              <label>
                District <span className="aud-optional">(optional)</span>
                <input
                  value={form.district}
                  onChange={(e) => setField("district", e.target.value)}
                  placeholder="Optional"
                />
              </label>
              <label>
                Description
                <textarea
                  value={form.description}
                  onChange={(e) => setField("description", e.target.value)}
                  rows={3}
                  placeholder="Optional note"
                />
              </label>
              {saveError ? <p className="aud-save-error">{saveError}</p> : null}
              <div className="aud-drawer-actions">
                <button type="button" className="btn" onClick={closeDrawer} disabled={saving}>
                  Cancel
                </button>
                <button type="submit" className="btn primary" disabled={saving}>
                  {saving ? "Saving…" : editing ? "Save changes" : "Create"}
                </button>
              </div>
            </form>
          </aside>
        </div>
      ) : null}
    </>
  );
}
