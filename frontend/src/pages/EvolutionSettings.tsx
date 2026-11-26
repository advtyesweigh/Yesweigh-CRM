import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";

type Config = {
  server_url: string;
  api_version: string;
  enabled: boolean;
  connection_status: string;
  last_health_check?: string | null;
  webhook_callback_url: string;
  api_key_set: boolean;
};

type Instance = {
  id: string;
  display_name: string;
  employee_code?: string;
  employee_name?: string;
  department: string;
  branch: string;
  purpose: string;
  connected_phone_number: string;
  expected_phone_number: string;
  connection_status: string;
  profile_photo?: string;
  assignees: { employee_id: number; name: string; is_primary_owner: boolean }[];
  last_connected_at?: string | null;
  history_sync_status?: string;
  history_sync_at?: string | null;
  history_sync_error?: string;
};

type EligibleEmployee = {
  employee_id: number;
  name: string;
  employee_code: string;
  department: string;
  branch: string;
  photo_url: string;
  already_linked: boolean;
  selectable: boolean;
};

type Stats = { total: number; connected: number; disconnected: number; unassigned: number };

function statusClass(s: string) {
  const v = s.toLowerCase();
  if (v.includes("connected") && !v.includes("dis")) return "ok";
  if (v.includes("qr") || v.includes("connecting")) return "warn";
  return "bad";
}

function initials(name: string) {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return `${parts[0][0] || ""}${parts[1][0] || ""}`.toUpperCase();
}

export default function EvolutionSettings() {
  const [sub, setSub] = useState<"config" | "instances">("instances");
  const [config, setConfig] = useState<Config | null>(null);
  const [serverUrl, setServerUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [items, setItems] = useState<Instance[]>([]);
  const [stats, setStats] = useState<Stats>({ total: 0, connected: 0, disconnected: 0, unassigned: 0 });
  const [instancesLoading, setInstancesLoading] = useState(true);
  const [instancesError, setInstancesError] = useState("");
  const [toast, setToast] = useState("");
  const [saving, setSaving] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [qrOpen, setQrOpen] = useState(false);
  const [qrData, setQrData] = useState<unknown>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [eligible, setEligible] = useState<EligibleEmployee[]>([]);
  const [eligibleLoading, setEligibleLoading] = useState(false);
  const [selectedEmployeeId, setSelectedEmployeeId] = useState<number | null>(null);
  const [qrLoading, setQrLoading] = useState(false);
  const [qrError, setQrError] = useState("");
  const [linkMode, setLinkMode] = useState<"qr" | "phone">("qr");
  const [pairPhone, setPairPhone] = useState("");
  const [pairingCode, setPairingCode] = useState("");
  const [pairLoading, setPairLoading] = useState(false);
  const [pairError, setPairError] = useState("");

  const selectedEmployee = useMemo(
    () => eligible.find((e) => e.employee_id === selectedEmployeeId) || null,
    [eligible, selectedEmployeeId],
  );

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(""), 3500);
  }

  async function loadConfig() {
    const c = await api<Config>("/api/settings/evolution");
    setConfig(c);
    setServerUrl(c.server_url);
    setEnabled(c.enabled);
  }

  async function loadInstances() {
    setInstancesLoading(true);
    setInstancesError("");
    try {
      const res = await api<{ items: Instance[]; stats: Stats }>("/api/whatsapp/instances");
      setItems(res.items);
      setStats(res.stats);
    } catch (e) {
      setInstancesError(e instanceof Error ? e.message : "Instances load failed");
      // Keep prior items if any; do not pretend the org has zero instances.
      throw e;
    } finally {
      setInstancesLoading(false);
    }
  }

  async function loadEligible() {
    setEligibleLoading(true);
    try {
      const res = await api<{ items: EligibleEmployee[] }>("/api/whatsapp/instances/eligible-employees");
      setEligible(res.items);
      const first = res.items.find((e) => e.selectable);
      setSelectedEmployeeId(first?.employee_id ?? null);
    } finally {
      setEligibleLoading(false);
    }
  }

  useEffect(() => {
    loadConfig().catch((e) => showToast(e instanceof Error ? e.message : "Config load failed"));
    loadInstances().catch((e) => showToast(e instanceof Error ? e.message : "Instances load failed"));
  }, []);

  useEffect(() => {
    if (!modalOpen) return;
    loadEligible().catch((e) => showToast(e instanceof Error ? e.message : "Employees load failed"));
  }, [modalOpen]);

  async function saveConfig() {
    setSaving(true);
    try {
      const c = await api<Config>("/api/settings/evolution", {
        method: "POST",
        body: JSON.stringify({
          server_url: serverUrl,
          api_key: apiKey,
          api_version: "evolution-go",
          enabled,
        }),
      });
      setConfig(c);
      setApiKey("");
      showToast("Configuration saved");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function testConnection() {
    try {
      const r = await api<{ ok: boolean; status: string; detail?: unknown }>("/api/settings/evolution/test", {
        method: "POST",
        body: "{}",
      });
      showToast(r.status);
      await loadConfig();
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Test failed");
    }
  }

  async function createInstance(e: React.FormEvent) {
    e.preventDefault();
    if (!selectedEmployee || !selectedEmployee.selectable) {
      showToast(selectedEmployee?.already_linked ? "That employee already has an instance" : "Select an employee");
      return;
    }
    setSaving(true);
    try {
      const created = await api<Instance>("/api/whatsapp/instances", {
        method: "POST",
        body: JSON.stringify({
          primary_employee_id: selectedEmployee.employee_id,
          purpose: "General Communication",
        }),
      });
      setModalOpen(false);
      setSelectedEmployeeId(null);
      await loadInstances();
      showToast(`${created.display_name} created — loading QR…`);
      await openQr(created.id);
    } catch (ex) {
      showToast(ex instanceof Error ? ex.message : "Create failed");
    } finally {
      setSaving(false);
    }
  }

  async function openQr(id: string) {
    setActiveId(id);
    setQrOpen(true);
    setQrLoading(true);
    setQrData(null);
    setQrError("");
    setLinkMode("qr");
    setPairingCode("");
    setPairError("");
    setPairPhone("");
    try {
      const qr = await api<Record<string, unknown>>(`/api/whatsapp/instances/${id}/qrcode`);
      setQrData(qr);
      // Keep summary/row status in sync with Evolution (LoggedIn vs QR Required)
      await loadInstances();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "QR failed";
      setQrError(msg);
      showToast(msg);
    } finally {
      setQrLoading(false);
    }
  }

  async function requestPairingCode() {
    if (!activeId) return;
    const phone = pairPhone.trim();
    if (!phone) {
      setPairError("Enter the WhatsApp phone number with country code");
      return;
    }
    setPairLoading(true);
    setPairError("");
    setPairingCode("");
    try {
      const res = await api<{
        connected?: boolean;
        pairing_code?: string | null;
        phone?: string;
        connection_status?: string;
      }>(`/api/whatsapp/instances/${activeId}/pairing-code`, {
        method: "POST",
        body: JSON.stringify({ phone }),
      });
      if (res.connected) {
        setQrData({ connected: true, phone: res.phone, connection_status: res.connection_status });
        showToast("WhatsApp is already connected");
        await loadInstances();
        return;
      }
      const code = String(res.pairing_code || "").trim();
      if (!code) {
        throw new Error("No pairing code returned");
      }
      setPairingCode(code);
      showToast("Pairing code ready — enter it on the phone");
      await loadInstances();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Pairing code failed";
      setPairError(msg);
      showToast(msg);
    } finally {
      setPairLoading(false);
    }
  }

  async function refreshStatus(id: string) {
    try {
      const r = await api<{ connection_status: string }>(`/api/whatsapp/instances/${id}/status`);
      showToast(r.connection_status);
      await loadInstances();
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Status failed");
    }
  }

  async function syncHistory(id: string) {
    try {
      await api(`/api/whatsapp/instances/${id}/sync-history`, { method: "POST", body: "{}" });
      showToast("History sync started");
      await loadInstances();
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Sync failed");
    }
  }

  async function act(id: string, action: "restart" | "disconnect" | "delete") {
    let deletePassword = "";
    if (action === "delete") {
      const entered = window.prompt(
        "Enter delete password to remove this YesWeigh WhatsApp instance. This cannot be undone.",
      );
      if (entered === null) return;
      deletePassword = entered.trim();
      if (deletePassword !== "1010") {
        showToast("Incorrect delete password");
        return;
      }
    }
    try {
      const method = action === "delete" ? "DELETE" : "POST";
      await api(`/api/whatsapp/instances/${id}${action === "delete" ? "" : `/${action}`}`, {
        method,
        body: action === "delete" ? JSON.stringify({ password: deletePassword }) : method === "POST" ? "{}" : undefined,
        headers: action === "delete" ? { "X-WhatsApp-Delete-Password": deletePassword } : undefined,
      });
      showToast(action === "delete" ? "Deleted" : `${action} done`);
      await loadInstances();
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Action failed");
    }
  }

  const qrConnected =
    !!qrData &&
    typeof qrData === "object" &&
    Boolean((qrData as Record<string, unknown>).connected);

  const qrImage = (() => {
    if (!qrData || typeof qrData !== "object") return null;
    const root = qrData as Record<string, unknown>;
    if (root.connected) return null;
    const q = root.qr;
    const candidates: unknown[] = [root.qrcode];
    if (typeof q === "string") candidates.push(q);
    if (q && typeof q === "object") {
      const d = q as Record<string, unknown>;
      candidates.push(d.qrcode, d.base64, d.code, d.qr);
      if (d.raw && typeof d.raw === "object") {
        const raw = d.raw as Record<string, unknown>;
        const data = (raw.data && typeof raw.data === "object" ? raw.data : raw) as Record<string, unknown>;
        candidates.push(data.qrcode, data.base64, data.code, data.qr);
      }
    }
    for (const c of candidates) {
      const raw = String(c || "").trim();
      if (!raw || raw === "null" || raw === "undefined") continue;
      if (raw.startsWith("data:")) return raw;
      if (raw.startsWith("http")) return raw;
      // Pairing-code style (short) is not an image
      if (raw.length < 80) continue;
      return `data:image/png;base64,${raw.replace(/^data:image\/\w+;base64,/, "")}`;
    }
    return null;
  })();

  return (
    <div>
      <div className="heading" style={{ marginBottom: 16 }}>
        <div>
          <h1 style={{ fontSize: 22, margin: 0 }}>WhatsApp</h1>
          <p>WhatsApp instance management for YesWeigh only.</p>
        </div>
        {sub === "instances" && (
          <button
            type="button"
            className="btn primary"
            onClick={() => {
              setSelectedEmployeeId(null);
              setModalOpen(true);
            }}
          >
            + Add Instance
          </button>
        )}
      </div>

      <div className="emp-profile-tabs" style={{ marginBottom: 16 }}>
        <button type="button" className={sub === "config" ? "active" : ""} onClick={() => setSub("config")}>API Configuration</button>
        <button type="button" className={sub === "instances" ? "active" : ""} onClick={() => setSub("instances")}>WhatsApp Instances</button>
      </div>

      {sub === "config" && (
        <section className="panel" style={{ padding: 18 }}>
          <div className="emp-fields">
            <div className="field full">
              <label>Evolution API server URL</label>
              <input value={serverUrl} onChange={(e) => setServerUrl(e.target.value)} placeholder="http://evolution-go:8080" />
            </div>
            <div className="field">
              <label>API key {config?.api_key_set ? "(saved — leave blank to keep)" : ""}</label>
              <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="••••••••" autoComplete="new-password" />
            </div>
            <div className="field">
              <label>API version</label>
              <input value={config?.api_version || "evolution-go"} disabled />
            </div>
            <div className="field full">
              <label>Webhook callback URL</label>
              <input value={config?.webhook_callback_url || ""} disabled />
            </div>
            <div className="field">
              <label>Integration status</label>
              <div><span className={`evo-pill ${statusClass(config?.connection_status || "")}`}>{config?.connection_status || "—"}</span></div>
            </div>
            <div className="field">
              <label>Last successful connection</label>
              <div>{config?.last_health_check ? new Date(config.last_health_check).toLocaleString() : "—"}</div>
            </div>
            <div className="field">
              <label>Enable integration</label>
              <label className="emp-switch" style={{ margin: 0 }}>
                <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
                {enabled ? "Enabled" : "Disabled"}
              </label>
            </div>
          </div>
          <div className="actions" style={{ marginTop: 16 }}>
            <button type="button" className="btn" onClick={testConnection}>Test Connection</button>
            <button type="button" className="btn primary" disabled={saving} onClick={saveConfig}>{saving ? "Saving…" : "Save Configuration"}</button>
          </div>
        </section>
      )}

      {sub === "instances" && (
        <>
          <div className="emp-stats" style={{ gridTemplateColumns: "repeat(4, minmax(0,1fr))" }}>
            {[
              ["Total Instances", stats.total],
              ["Connected", stats.connected],
              ["Disconnected", stats.disconnected],
              ["Unassigned", stats.unassigned],
            ].map(([label, value]) => (
              <div className="emp-stat" key={String(label)}>
                <small>{label}</small>
                <strong>{String(value).padStart(2, "0")}</strong>
              </div>
            ))}
          </div>

          <section className="panel">
            {instancesLoading ? (
              <div className="emp-empty">Loading WhatsApp instances…</div>
            ) : instancesError ? (
              <div className="emp-empty" style={{ color: "#b91c1c" }}>
                Could not load instances ({instancesError}).{" "}
                <button type="button" className="btn" onClick={() => loadInstances().catch((e) => showToast(e instanceof Error ? e.message : "Instances load failed"))}>
                  Retry
                </button>
              </div>
            ) : items.length === 0 ? (
              <div className="emp-empty">No YesWeigh WhatsApp instances yet. Create one to get started.</div>
            ) : (
              <div className="tablewrap">
                <table>
                  <thead>
                    <tr>
                      <th>Instance</th>
                      <th>WhatsApp</th>
                      <th>Assigned</th>
                      <th>Status</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((i) => (
                      <tr key={i.id}>
                        <td>
                          <div className="emp-person">
                            <div className="emp-avatar">{initials(i.display_name)}</div>
                            <div>
                              <b>{i.display_name}</b>
                              <small>
                                {i.employee_code
                                  ? `Code ${i.employee_code}`
                                  : i.branch || i.purpose}
                                {i.assignees[0]?.name && i.assignees[0].name !== i.display_name
                                  ? ` · ${i.assignees[0].name}`
                                  : ""}
                              </small>
                            </div>
                          </div>
                        </td>
                        <td>{i.connected_phone_number || i.expected_phone_number || "—"}</td>
                        <td>{i.assignees.length ? i.assignees.map((a) => a.name).join(", ") : "Unassigned"}</td>
                        <td>
                          <span className={`evo-pill ${statusClass(i.connection_status)}`}>{i.connection_status}</span>
                          {i.history_sync_status ? (
                            <div style={{ marginTop: 4, fontSize: 12, color: "#64748b" }}>
                              Sync: {i.history_sync_status}
                              {i.history_sync_at ? ` · ${new Date(i.history_sync_at).toLocaleString()}` : ""}
                            </div>
                          ) : null}
                        </td>
                        <td>
                          <div className="emp-actions">
                            <button type="button" className="btn" onClick={() => openQr(i.id)}>QR</button>
                            <button
                              type="button"
                              className="btn"
                              disabled={i.history_sync_status === "running"}
                              onClick={() => syncHistory(i.id)}
                              title="Download contacts and request chat history from Evolution/WhatsApp"
                            >
                              {i.history_sync_status === "running" ? "Syncing…" : "Sync history"}
                            </button>
                            <button type="button" className="btn" onClick={() => act(i.id, "disconnect")}>Disconnect</button>
                            <button type="button" className="btn" onClick={() => act(i.id, "delete")}>Delete</button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}

      {modalOpen && (
        <div className="emp-overlay" onClick={(e) => { if (e.target === e.currentTarget) setModalOpen(false); }}>
          <section className="emp-drawer evo-add-modal" role="dialog" aria-modal="true" aria-labelledby="evo-add-title">
            <div className="emp-drawer-head">
              <div>
                <h2 id="evo-add-title">Add WhatsApp Instance</h2>
                <p>Choose an employee, then scan the QR code on their phone.</p>
              </div>
              <button type="button" className="iconbtn" onClick={() => setModalOpen(false)} aria-label="Close">✕</button>
            </div>
            <form onSubmit={createInstance}>
              <div className="emp-formbody">
                <div className="field evo-emp-field">
                  <label htmlFor="evo-emp-select">Employee *</label>
                  <p className="evo-emp-hint">Each WhatsApp line is linked to one ADMS staff member. Linked employees stay listed but cannot be selected again.</p>
                  {eligibleLoading ? (
                    <div className="emp-empty" style={{ padding: 16 }}>Loading employees…</div>
                  ) : !eligible.length ? (
                    <div className="emp-empty" style={{ padding: 16 }}>No eligible employees found.</div>
                  ) : (
                    <select
                      id="evo-emp-select"
                      className="evo-emp-select"
                      value={selectedEmployeeId ?? ""}
                      onChange={(e) => {
                        const v = e.target.value;
                        setSelectedEmployeeId(v ? Number(v) : null);
                      }}
                      required
                      aria-required="true"
                    >
                      <option value="" disabled>
                        Select an employee…
                      </option>
                      {eligible.map((emp) => {
                        const code = emp.employee_code ? `Code ${emp.employee_code}` : "No code";
                        const label = emp.already_linked
                          ? `${emp.name} (${code}) — Already linked`
                          : `${emp.name} (${code})`;
                        return (
                          <option
                            key={emp.employee_id}
                            value={emp.employee_id}
                            disabled={!emp.selectable}
                          >
                            {label}
                          </option>
                        );
                      })}
                    </select>
                  )}
                </div>
              </div>
              <div className="emp-formfoot">
                <small className="evo-emp-footnote">
                  {selectedEmployee
                    ? `Instance will be named “${selectedEmployee.name}”.`
                    : "Select an available employee to continue."}
                </small>
                <div className="actions">
                  <button type="button" className="btn" onClick={() => setModalOpen(false)}>Cancel</button>
                  <button
                    type="submit"
                    className="btn primary"
                    disabled={saving || !selectedEmployee?.selectable}
                  >
                    {saving ? "Creating…" : "Create & show QR"}
                  </button>
                </div>
              </div>
            </form>
          </section>
        </div>
      )}

      {qrOpen && (
        <div className="emp-overlay" onClick={(e) => { if (e.target === e.currentTarget) setQrOpen(false); }}>
          <section className="emp-drawer evo-qr-modal" role="dialog" aria-modal="true" aria-labelledby="evo-qr-title">
            <div className="emp-drawer-head">
              <div>
                <h2 id="evo-qr-title">Link WhatsApp</h2>
                <p>
                  {linkMode === "qr"
                    ? "WhatsApp → Linked Devices → Link a Device → Scan QR"
                    : "WhatsApp → Linked Devices → Link a Device → Link with phone number"}
                </p>
              </div>
              <button type="button" className="iconbtn" onClick={() => setQrOpen(false)} aria-label="Close">✕</button>
            </div>
            <div className="emp-formbody" style={{ textAlign: "center" }}>
              {!qrConnected && (
                <div className="evo-link-tabs" role="tablist" aria-label="Link method">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={linkMode === "qr"}
                    className={linkMode === "qr" ? "active" : ""}
                    onClick={() => setLinkMode("qr")}
                  >
                    Scan QR
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={linkMode === "phone"}
                    className={linkMode === "phone" ? "active" : ""}
                    onClick={() => setLinkMode("phone")}
                  >
                    Link with phone number
                  </button>
                </div>
              )}

              {qrConnected && (
                <div className="emp-empty" style={{ color: "#087a56" }}>
                  WhatsApp is connected
                  {(qrData as Record<string, unknown>)?.phone
                    ? ` · ${String((qrData as Record<string, unknown>).phone)}`
                    : ""}
                </div>
              )}

              {!qrConnected && linkMode === "qr" && (
                <>
                  {qrLoading && <div className="emp-empty">Waiting for QR from Evolution…</div>}
                  {!qrLoading && qrImage && (
                    <img src={qrImage} alt="WhatsApp QR" style={{ width: 260, height: 260, objectFit: "contain" }} />
                  )}
                  {!qrLoading && !qrImage && (
                    <div className="emp-empty" style={{ whiteSpace: "pre-wrap", color: qrError ? "#bd2438" : undefined }}>
                      {qrError || "QR not ready yet. Click Refresh QR."}
                    </div>
                  )}
                </>
              )}

              {!qrConnected && linkMode === "phone" && (
                <div className="evo-pair-panel">
                  <p className="evo-pair-hint">
                    Use this when QR scan fails (“Couldn&apos;t link device”). Enter the staff WhatsApp number with country code.
                  </p>
                  <div className="field evo-pair-field">
                    <label htmlFor="evo-pair-phone">Phone number</label>
                    <input
                      id="evo-pair-phone"
                      type="tel"
                      inputMode="tel"
                      placeholder=""
                      value={pairPhone}
                      onChange={(e) => setPairPhone(e.target.value)}
                      autoComplete="tel"
                    />
                  </div>
                  <button
                    type="button"
                    className="btn primary"
                    disabled={pairLoading}
                    onClick={() => requestPairingCode()}
                  >
                    {pairLoading ? "Getting code…" : "Get pairing code"}
                  </button>
                  {pairError && (
                    <div className="emp-empty" style={{ color: "#bd2438", whiteSpace: "pre-wrap", marginTop: 12 }}>
                      {pairError}
                    </div>
                  )}
                  {pairingCode && (
                    <div className="evo-pair-code" aria-live="polite">
                      <small>Enter this code on the phone</small>
                      <strong>{pairingCode}</strong>
                    </div>
                  )}
                </div>
              )}

              <div className="actions" style={{ justifyContent: "center", marginTop: 16 }}>
                {linkMode === "qr" && !qrConnected && (
                  <button type="button" className="btn" disabled={qrLoading} onClick={() => activeId && openQr(activeId)}>
                    Refresh QR
                  </button>
                )}
                <button type="button" className="btn" onClick={() => activeId && refreshStatus(activeId)}>Check status</button>
                <button type="button" className="btn primary" onClick={() => { setQrOpen(false); loadInstances(); }}>Done</button>
              </div>
            </div>
          </section>
        </div>
      )}

      {toast && <div className="emp-toast" role="status">{toast}</div>}
    </div>
  );
}
