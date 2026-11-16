import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";

type Device = {
  serial_number: string;
  name?: string;
  online?: boolean;
  status?: string;
  model?: string;
  last_heartbeat_at?: string;
  last_userinfo_at?: string;
  userinfo_supported?: string;
  employee_count?: number;
  named_count?: number;
};

type PreviewRow = {
  employee_code: string;
  name: string;
  device_serial?: string;
  linked: boolean;
  crm_employee_id?: number | null;
  crm_name?: string | null;
  crm_code?: string | null;
  is_placeholder?: boolean;
  source?: string;
};

type CrmEmp = {
  id: number;
  code: string;
  name: string;
  adms_employee_code?: string;
  adms_device?: string;
};

type Preview = {
  ok?: boolean;
  devices: Device[];
  selected_device?: string;
  last_synchronization_time?: string;
  userinfo_status?: Record<string, unknown>;
  count: number;
  named_count: number;
  linked_count: number;
  employees: PreviewRow[];
  crm_employees: CrmEmp[];
  csv_fallback_recommended?: boolean;
  auto_sync_users?: boolean;
};

type ImportResult = {
  created_count: number;
  updated_count: number;
  skipped_count: number;
  failed_count?: number;
  needs_confirm_count?: number;
  named_count?: number;
  unnamed_count?: number;
  created?: { employee_code: string; name: string; id?: string }[];
  updated?: { employee_code: string; name: string }[];
  failed?: { employee_code: string; reason: string }[];
  needs_confirm?: {
    employee_code: string;
    crm_name: string;
    device_name: string;
    crm_id?: string;
  }[];
  last_synchronization_time?: string;
  note?: string;
};

function fmtSync(s?: string) {
  if (!s) return "Never";
  try {
    return new Date(s).toLocaleString("en-GB", {
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return s;
  }
}

export default function ImportDeviceModal({
  open,
  onClose,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  onDone: () => void;
}) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [serial, setSerial] = useState("");
  const [loading, setLoading] = useState(false);
  const [phase, setPhase] = useState<"idle" | "querying" | "importing" | "done">("idle");
  const [progressMsg, setProgressMsg] = useState("");
  const [err, setErr] = useState("");
  const [result, setResult] = useState<ImportResult | null>(null);
  const [confirmOverwrite, setConfirmOverwrite] = useState(false);
  const [updateNames, setUpdateNames] = useState(true);
  const [mapFor, setMapFor] = useState<string | null>(null);
  const [mapEmployeeId, setMapEmployeeId] = useState("");
  const [editName, setEditName] = useState<Record<string, string>>({});
  const [csvText, setCsvText] = useState("");
  const [showCsv, setShowCsv] = useState(false);

  const loadPreview = useCallback(async (deviceSerial?: string) => {
    setLoading(true);
    setErr("");
    try {
      const qs = deviceSerial ? `?device_serial=${encodeURIComponent(deviceSerial)}` : "";
      const res = await api<Preview>(`/api/integrations/adms/import-preview${qs}`);
      setPreview(res);
      if (!deviceSerial && res.devices?.length && !serial) {
        const first = res.devices.find((d) => d.status === "approved") || res.devices[0];
        if (first?.serial_number) {
          setSerial(first.serial_number);
          await loadPreview(first.serial_number);
          return;
        }
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load devices");
    } finally {
      setLoading(false);
    }
  }, [serial]);

  useEffect(() => {
    if (!open) return;
    setResult(null);
    setPhase("idle");
    setProgressMsg("");
    setConfirmOverwrite(false);
    setShowCsv(false);
    void loadPreview(serial || undefined);
  }, [open]);

  useEffect(() => {
    if (!open || !serial) return;
    void loadPreview(serial);
  }, [serial]);

  const rows = preview?.employees || [];
  const devices = preview?.devices || [];
  const selected = devices.find((d) => d.serial_number === serial);

  const stats = useMemo(() => {
    const imported = rows.filter((r) => r.linked).length;
    const pending = rows.length - imported;
    const named = rows.filter((r) => (r.name || "").trim()).length;
    return { imported, pending, named, total: rows.length };
  }, [rows]);

  async function queryDevice() {
    if (!serial || phase === "querying") return;
    setPhase("querying");
    setProgressMsg("Queuing DATA QUERY USERINFO on device…");
    setErr("");
    try {
      await api("/api/integrations/adms/sync-device-users", {
        method: "POST",
        body: JSON.stringify({ device_serial: serial }),
      });
      setProgressMsg("Queued. Waiting briefly, then refreshing preview…");
      await new Promise((r) => setTimeout(r, 2500));
      await loadPreview(serial);
      setProgressMsg("Preview refreshed. Import when names appear, or use CSV fallback.");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "USERINFO query failed");
    } finally {
      setPhase("idle");
    }
  }

  async function runImport() {
    if (!serial || phase === "importing") return;
    setPhase("importing");
    setProgressMsg("Importing enrollment IDs + names into CRM…");
    setErr("");
    try {
      const res = await api<ImportResult>("/api/integrations/adms/import-employees", {
        method: "POST",
        body: JSON.stringify({
          device_serial: serial,
          update_names: updateNames,
          confirm_overwrite: confirmOverwrite,
          named_only: false,
        }),
      });
      setResult(res);
      setPhase("done");
      setProgressMsg(
        `Done — created ${res.created_count}, updated ${res.updated_count}, failed ${res.failed_count ?? 0}.`
      );
      await loadPreview(serial);
      onDone();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Import failed");
      setPhase("idle");
    }
  }

  async function saveMap(enrollmentId: string) {
    const id = Number(mapEmployeeId);
    if (!id) return;
    const name = (editName[enrollmentId] || "").trim();
    try {
      const res = await api<{ ok: boolean; error?: string; needs_confirm?: boolean }>(
        "/api/integrations/adms/map-employee",
        {
          method: "POST",
          body: JSON.stringify({
            employee_id: id,
            enrollment_id: enrollmentId,
            device_serial: serial,
            overwrite_name: name || undefined,
            confirm_overwrite: confirmOverwrite,
          }),
        }
      );
      if (!res.ok) {
        setErr(res.error || "Map failed");
        if (res.needs_confirm) setConfirmOverwrite(true);
        return;
      }
      setMapFor(null);
      setMapEmployeeId("");
      await loadPreview(serial);
      onDone();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Map failed");
    }
  }

  async function saveInlineName(enrollmentId: string) {
    const name = (editName[enrollmentId] || "").trim();
    const row = rows.find((r) => r.employee_code === enrollmentId);
    if (!row?.crm_employee_id || !name) return;
    try {
      await api(`/api/employees/${row.crm_employee_id}`, {
        method: "PATCH",
        body: JSON.stringify({ name }),
      });
      await loadPreview(serial);
      onDone();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Name update failed");
    }
  }

  async function runCsvImport() {
    if (!csvText.trim()) return;
    setPhase("importing");
    setProgressMsg("Importing CSV…");
    try {
      const res = await api<ImportResult>("/api/integrations/adms/import-employees-csv", {
        method: "POST",
        body: JSON.stringify({
          csv: csvText,
          device_serial: serial,
          confirm_overwrite: confirmOverwrite,
        }),
      });
      setResult(res);
      setPhase("done");
      setProgressMsg(`CSV import — created ${res.created_count}, updated ${res.updated_count}.`);
      await loadPreview(serial);
      onDone();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "CSV import failed");
      setPhase("idle");
    }
  }

  if (!open) return null;

  return (
    <div className="emp-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="imp-modal" role="dialog" aria-modal="true" aria-labelledby="imp-title">
        <div className="imp-modal-head">
          <div>
            <h2 id="imp-title">Import from Device</h2>
            <p>Pull enrollment ID + name from a registered ZKTeco machine via ADMS.</p>
          </div>
          <button type="button" className="iconbtn" aria-label="Close" onClick={onClose}>✕</button>
        </div>

        <div className="imp-modal-body">
          {err && <div className="err" style={{ marginBottom: 12 }}>{err}</div>}

          <div className="imp-toolbar">
            <label className="imp-field">
              <span>ZKTeco device</span>
              <select
                value={serial}
                onChange={(e) => setSerial(e.target.value)}
                disabled={loading || phase === "importing"}
              >
                <option value="">Select registered device…</option>
                {devices.map((d) => (
                  <option key={d.serial_number} value={d.serial_number}>
                    {(d.name && d.name !== d.serial_number ? `${d.name} · ` : "") + d.serial_number}
                    {d.online ? " · Online" : " · Offline"}
                    {d.model ? ` · ${d.model}` : ""}
                  </option>
                ))}
              </select>
            </label>
            <div className="imp-sync-meta">
              <div>
                <small>Last synchronization</small>
                <strong>{fmtSync(preview?.last_synchronization_time)}</strong>
              </div>
              <div>
                <small>USERINFO</small>
                <strong>
                  {String(preview?.userinfo_status?.userinfo_supported || selected?.userinfo_supported || "unknown")}
                </strong>
              </div>
              <div>
                <small>Auto-sync</small>
                <strong>{preview?.auto_sync_users ? "On" : "Off"}</strong>
              </div>
            </div>
          </div>

          <div className="imp-stats">
            <div><small>On device</small><strong>{stats.total}</strong></div>
            <div><small>Named</small><strong>{stats.named}</strong></div>
            <div><small>Linked in CRM</small><strong>{stats.imported}</strong></div>
            <div><small>Pending</small><strong>{stats.pending}</strong></div>
          </div>

          {(phase !== "idle" || progressMsg) && (
            <div className={`imp-progress ${phase === "done" ? "done" : ""}`}>
              <div className="imp-progress-bar" data-phase={phase} />
              <span>{progressMsg || (phase === "importing" ? "Importing…" : "Working…")}</span>
            </div>
          )}

          {preview?.csv_fallback_recommended && (
            <div className="imp-banner">
              Device has no names yet (USERINFO unverified / offline). Use <b>Query device</b> when online, or CSV fallback.
            </div>
          )}

          <div className="imp-actions">
            <button type="button" className="btn" disabled={!serial || phase === "querying"} onClick={() => void queryDevice()}>
              {phase === "querying" ? "Querying…" : "Query device"}
            </button>
            <button type="button" className="btn primary" disabled={!serial || phase === "importing"} onClick={() => void runImport()}>
              {phase === "importing" ? "Importing…" : "Import employees"}
            </button>
            <button type="button" className="btn" onClick={() => setShowCsv((v) => !v)}>
              {showCsv ? "Hide CSV" : "CSV fallback"}
            </button>
            <label className="imp-check">
              <input type="checkbox" checked={updateNames} onChange={(e) => setUpdateNames(e.target.checked)} />
              Update placeholder names
            </label>
            <label className="imp-check">
              <input type="checkbox" checked={confirmOverwrite} onChange={(e) => setConfirmOverwrite(e.target.checked)} />
              Confirm overwrite of maintained names
            </label>
          </div>

          {showCsv && (
            <div className="imp-csv">
              <p>
                Columns: <code>enrollment_id,name</code> (optional <code>device_serial,phone,branch</code>).
                Use this when the device is offline or USERINFO names are empty.
              </p>
              <div className="imp-actions" style={{ marginBottom: 8 }}>
                <button
                  type="button"
                  className="btn"
                  onClick={() => {
                    const header = "enrollment_id,name,device_serial\n";
                    const body = rows.length
                      ? rows.map((r) => `${r.employee_code},"${(editName[r.employee_code] ?? r.name ?? "").split('"').join('""')}",${serial}`).join("\n")
                      : "1,Anita Sharma," + (serial || "") + "\n2,Ravi Kumar," + (serial || "");
                    const text = header + body + "\n";
                    setCsvText(text);
                    const a = document.createElement("a");
                    a.href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
                    a.download = `adms-employees-${serial || "template"}.csv`;
                    a.click();
                    URL.revokeObjectURL(a.href);
                  }}
                >
                  Download template
                </button>
              </div>
              <textarea
                rows={6}
                placeholder={"enrollment_id,name\n1,Anita Sharma\n2,Ravi Kumar"}
                value={csvText}
                onChange={(e) => setCsvText(e.target.value)}
              />
              <button type="button" className="btn dark" disabled={!csvText.trim()} onClick={() => void runCsvImport()}>
                Import CSV
              </button>
            </div>
          )}

          {result && (
            <div className="imp-result">
              <h3>Import result</h3>
              <p>
                Created {result.created_count} · Updated {result.updated_count} · Failed {result.failed_count ?? 0}
                {(result.needs_confirm_count ?? 0) > 0 ? ` · Needs confirm ${result.needs_confirm_count}` : ""}
              </p>
              {result.note && <p className="imp-note">{result.note}</p>}
              {(result.failed || []).length > 0 && (
                <ul className="imp-failed">
                  {result.failed!.map((f) => (
                    <li key={f.employee_code}>{f.employee_code}: {f.reason}</li>
                  ))}
                </ul>
              )}
              {(result.needs_confirm || []).length > 0 && (
                <ul className="imp-failed">
                  {result.needs_confirm!.map((n) => (
                    <li key={n.employee_code}>
                      {n.employee_code}: CRM “{n.crm_name}” vs device “{n.device_name}” — enable overwrite confirm
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <div className="imp-table-wrap">
            <table className="imp-table">
              <thead>
                <tr>
                  <th>Enrollment ID</th>
                  <th>Device name</th>
                  <th>CRM mapping</th>
                  <th>Edit / map</th>
                </tr>
              </thead>
              <tbody>
                {loading && !rows.length ? (
                  <tr><td colSpan={4} className="imp-empty">Loading…</td></tr>
                ) : null}
                {!loading && serial && rows.length === 0 ? (
                  <tr><td colSpan={4} className="imp-empty">No enrollments for this device yet.</td></tr>
                ) : null}
                {!serial ? (
                  <tr><td colSpan={4} className="imp-empty">Select a device to preview enrollments.</td></tr>
                ) : null}
                {rows.map((r) => (
                  <tr key={`${r.device_serial || serial}-${r.employee_code}`}>
                    <td><code>{r.employee_code}</code></td>
                    <td>
                      <input
                        className="imp-inline"
                        value={editName[r.employee_code] ?? r.name ?? ""}
                        placeholder="Name"
                        onChange={(e) => setEditName((m) => ({ ...m, [r.employee_code]: e.target.value }))}
                      />
                    </td>
                    <td>
                      {r.linked ? (
                        <span className="imp-linked">{r.crm_name || r.crm_code}</span>
                      ) : (
                        <span className="imp-unlinked">Not linked</span>
                      )}
                    </td>
                    <td>
                      <div className="imp-row-actions">
                        {r.linked && r.crm_employee_id ? (
                          <button type="button" className="btn" onClick={() => void saveInlineName(r.employee_code)}>
                            Save name
                          </button>
                        ) : null}
                        {mapFor === r.employee_code ? (
                          <>
                            <select value={mapEmployeeId} onChange={(e) => setMapEmployeeId(e.target.value)}>
                              <option value="">CRM employee…</option>
                              {(preview?.crm_employees || []).map((c) => (
                                <option key={c.id} value={String(c.id)}>
                                  {c.name} ({c.code})
                                </option>
                              ))}
                            </select>
                            <button type="button" className="btn primary" onClick={() => void saveMap(r.employee_code)}>
                              Link
                            </button>
                            <button type="button" className="btn" onClick={() => setMapFor(null)}>Cancel</button>
                          </>
                        ) : (
                          <button type="button" className="btn" onClick={() => { setMapFor(r.employee_code); setMapEmployeeId(r.crm_employee_id ? String(r.crm_employee_id) : ""); }}>
                            Map existing
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </div>
  );
}
