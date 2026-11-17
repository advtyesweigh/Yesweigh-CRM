import { useEffect, useState } from "react";
import { api } from "../api/client";

type ZohoOrg = {
  organization_id: string;
  name: string;
  contact_name?: string;
  plan_name?: string;
};

type ZohoStatus = {
  connected: boolean;
  credentials_configured: boolean;
  credentials_source: string;
  credentials_locked_by_env: boolean;
  client_id: string;
  client_id_hint: string;
  connection_status: string;
  api_product: string;
  zoho_org_id: string;
  zoho_org_name: string;
  zoho_org_number: string;
  accounts_url: string;
  api_base_url: string;
  data_center: string;
  oauth_callback_url: string;
  webhook_url: string;
  webhook_secret_set: boolean;
  webhook_secret_hint: string;
  last_sync_at?: string | null;
  last_sync_status: string;
  last_sync_error: string;
  last_error: string;
  pending_organizations: ZohoOrg[];
  setup_hint: string;
};

function statusClass(s: string) {
  const v = (s || "").toLowerCase();
  if (v === "connected" || v === "ok") return "ok";
  if (v.includes("pending") || v.includes("sync")) return "warn";
  if (v.includes("error") || v.includes("disconnect")) return "bad";
  return "warn";
}

function orgDisplay(status: ZohoStatus) {
  const name = (status.zoho_org_name || "").trim();
  const id = (status.zoho_org_number || status.zoho_org_id || "").trim();
  if (name && id) return `${name} · ${id}`;
  if (name) return name;
  if (id) return id;
  return "";
}

async function copyText(value: string) {
  await navigator.clipboard.writeText(value);
}

function openZohoPopup(authUrl: string): Window | null {
  const w = 520;
  const h = 720;
  const left = Math.max(0, Math.round(window.screenX + (window.outerWidth - w) / 2));
  const top = Math.max(0, Math.round(window.screenY + (window.outerHeight - h) / 2));
  return window.open(
    authUrl,
    "zoho-oauth",
    `popup=yes,width=${w},height=${h},left=${left},top=${top},noopener=no,noreferrer=no`,
  );
}

function PlugIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M9 7V3M15 7V3M8 11h8v2a4 4 0 0 1-4 4h0a4 4 0 0 1-4-4v-2Zm4 6v4M6 7h12"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export default function ZohoSettings() {
  const [status, setStatus] = useState<ZohoStatus | null>(null);
  const [toast, setToast] = useState("");
  const [busy, setBusy] = useState(false);
  const [secret, setSecret] = useState("");
  const [selectedOrg, setSelectedOrg] = useState("");
  const [endpointsOpen, setEndpointsOpen] = useState(false);
  const [signingIn, setSigningIn] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(""), 4000);
  }

  async function load() {
    const s = await api<ZohoStatus>("/api/integrations/zoho");
    setStatus(s);
    if (s.pending_organizations.length && !selectedOrg) {
      setSelectedOrg(s.pending_organizations[0].organization_id);
    }
    return s;
  }

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const err = params.get("error");
    const oauth = params.get("oauth");
    load()
      .then((s) => {
        if (err) {
          showToast(decodeURIComponent(err));
          setModalOpen(true);
        } else if (oauth) {
          showToast("Signed in — select an organization");
          setModalOpen(true);
        }
        if (s && (s.connection_status === "pending_org" || s.pending_organizations.length > 0) && !s.connected) {
          setModalOpen(true);
        }
      })
      .catch((e) => showToast(e instanceof Error ? e.message : "Failed to load Zoho status"));

    function onMessage(ev: MessageEvent) {
      const data = ev.data;
      if (!data || data.type !== "zoho-oauth") return;
      setSigningIn(false);
      setBusy(false);
      setModalOpen(true);
      if (data.ok) {
        showToast("Signed in with Zoho — select an organization");
        load().catch(() => undefined);
      } else {
        showToast(data.error || "Zoho sign-in failed");
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function signIn() {
    setBusy(true);
    setSigningIn(true);
    try {
      const r = await api<{ auth_url: string }>("/api/integrations/zoho/connect?popup=true");
      const popup = openZohoPopup(r.auth_url);
      if (!popup) {
        const full = await api<{ auth_url: string }>("/api/integrations/zoho/connect?popup=false");
        window.location.href = full.auth_url;
        return;
      }
      const timer = window.setInterval(() => {
        if (popup.closed) {
          window.clearInterval(timer);
          setSigningIn(false);
          setBusy(false);
          load().catch(() => undefined);
        }
      }, 600);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Sign-in failed");
      setSigningIn(false);
      setBusy(false);
    }
  }

  async function disconnect() {
    if (!confirm("Disconnect Zoho? Webhook endpoint will keep working with the shared secret.")) return;
    setBusy(true);
    try {
      const s = await api<ZohoStatus>("/api/integrations/zoho/disconnect", {
        method: "POST",
        body: "{}",
      });
      setStatus(s);
      setSecret("");
      setSelectedOrg("");
      showToast("Disconnected");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Disconnect failed");
    } finally {
      setBusy(false);
    }
  }

  async function saveOrg() {
    if (!selectedOrg || !status) return;
    const org = status.pending_organizations.find((o) => o.organization_id === selectedOrg);
    setBusy(true);
    try {
      const s = await api<ZohoStatus>("/api/integrations/zoho/select-org", {
        method: "POST",
        body: JSON.stringify({
          organization_id: selectedOrg,
          name: org?.name || "",
          organization_number: selectedOrg,
        }),
      });
      setStatus(s);
      showToast("Organization saved");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Save org failed");
    } finally {
      setBusy(false);
    }
  }

  async function syncNow() {
    setBusy(true);
    try {
      const r = await api<{ message: string; last_sync_at?: string }>(
        "/api/integrations/zoho/sync?full_customers=true&date_from=2025-04-01",
        { method: "POST", body: "{}" },
      );
      showToast(r.message || "Sync complete");
      await load();
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Sync failed");
      await load().catch(() => undefined);
    } finally {
      setBusy(false);
    }
  }

  async function revealSecret() {
    setBusy(true);
    try {
      const r = await api<{ webhook_secret: string }>("/api/integrations/zoho/webhook-secret");
      setSecret(r.webhook_secret);
      showToast("Webhook secret revealed");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Could not load secret");
    } finally {
      setBusy(false);
    }
  }

  async function rotateSecret() {
    if (!confirm("Rotate webhook secret? Update Zoho / Zoho Flow with the new value.")) return;
    setBusy(true);
    try {
      const r = await api<{ webhook_secret: string }>("/api/integrations/zoho/webhook-secret/rotate", {
        method: "POST",
        body: "{}",
      });
      setSecret(r.webhook_secret);
      await load();
      showToast("Webhook secret rotated");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Rotate failed");
    } finally {
      setBusy(false);
    }
  }

  async function onCopy(label: string, value: string) {
    try {
      await copyText(value);
      showToast(`${label} copied`);
    } catch {
      showToast("Copy failed");
    }
  }

  const pendingOrgs = status?.pending_organizations || [];
  const needsOrg =
    !!status &&
    !status.connected &&
    (status.connection_status === "pending_org" || pendingOrgs.length > 0) &&
    !!status.credentials_configured;
  const canSignIn = !!status?.credentials_configured && !needsOrg;
  const orgLine = status?.connected ? orgDisplay(status) : "";
  const cardStatus = !status
    ? { label: "Loading…", cls: "blue" }
    : status.connected
      ? { label: "Connected", cls: "ok" }
      : needsOrg
        ? { label: "Select org", cls: "warn" }
        : { label: "Not connected", cls: "off" };

  return (
    <div className="zoho-embed">
      <div className="heading" style={{ marginBottom: 16 }}>
        <div>
          <h1 style={{ fontSize: 22, margin: 0 }}>Zoho</h1>
          <p>External connectors for accounting sync.</p>
        </div>
      </div>

      <div className="zoho-panel-h">
        <h2>External connectors</h2>
      </div>

      <div className="zoho-intgrid">
        <article className="zoho-intcard">
          <div className="top">
            <div className="logo" aria-hidden="true">
              <img src="/assets/zoho-books-logo.svg" alt="" width={38} height={38} />
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <h3>Zoho Books</h3>
              <p>Accounting sync — invoices, credit notes, and dealer contacts.</p>
            </div>
          </div>
          <div className="foot">
            <div style={{ minWidth: 0, flex: 1 }}>
              <span className={`zoho-badge ${cardStatus.cls}`}>{cardStatus.label}</span>
              {orgLine ? <span className="zoho-org-detail">{orgLine}</span> : null}
              {status && !status.credentials_configured ? (
                <span className="zoho-org-detail" style={{ color: "#b45309" }}>
                  Zoho app not configured on server
                </span>
              ) : null}
            </div>
            <button type="button" className="btn primary zoho-configure-btn" onClick={() => setModalOpen(true)}>
              <PlugIcon /> Configure
            </button>
          </div>
        </article>
      </div>

      {modalOpen && status && (
        <div
          className="zoho-modal"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget) setModalOpen(false);
          }}
        >
          <div className="zoho-dialog" role="dialog" aria-modal="true" aria-label="Zoho Books">
            <div className="zoho-dialog-head">
              <div>
                <h2>Zoho Books</h2>
                <p>Sign in with Zoho, pick an organization, then sync sales and dealers.</p>
              </div>
              <button type="button" className="btn" onClick={() => setModalOpen(false)} aria-label="Close">
                ✕
              </button>
            </div>

            {!status.credentials_configured ? (
              <p className="zoho-muted" style={{ color: "#b45309" }}>
                Zoho app not configured on server
              </p>
            ) : status.connected ? (
              <>
                <div className="zoho-connected-box">
                  <p className="zoho-connected-label">Connected</p>
                  <p className="zoho-connected-name">{status.zoho_org_name || "Organization"}</p>
                  <p className="zoho-connected-id">
                    Organization ID · {status.zoho_org_number || status.zoho_org_id || "—"}
                  </p>
                  <p className="zoho-muted" style={{ marginTop: 8 }}>
                    Last sync:{" "}
                    {status.last_sync_at ? new Date(status.last_sync_at).toLocaleString() : "Never"}
                    {status.last_sync_status ? (
                      <span className={`evo-pill ${statusClass(status.last_sync_status)}`} style={{ marginLeft: 8 }}>
                        {status.last_sync_status}
                      </span>
                    ) : null}
                  </p>
                  {(status.last_sync_error || status.last_error) && (
                    <p style={{ color: "#b82d40", margin: "8px 0 0", fontSize: 13 }}>
                      {status.last_sync_error || status.last_error}
                    </p>
                  )}
                </div>
                <div className="zoho-dialog-actions">
                  <button type="button" className="btn" disabled={busy} onClick={disconnect}>
                    Disconnect
                  </button>
                  <button type="button" className="btn" disabled={busy || signingIn} onClick={signIn}>
                    {signingIn ? "Opening Zoho…" : "Reconnect"}
                  </button>
                  <button type="button" className="btn primary" disabled={busy} onClick={syncNow}>
                    {busy ? "Working…" : "Sync now"}
                  </button>
                </div>
              </>
            ) : needsOrg ? (
              <>
                <p className="zoho-muted" style={{ marginBottom: 12 }}>
                  Signed in — choose your organization.
                </p>
                {pendingOrgs.length === 0 ? (
                  <p className="zoho-muted">
                    No organizations returned.{" "}
                    <button type="button" className="linkish" disabled={busy || signingIn} onClick={signIn}>
                      Sign in again
                    </button>
                  </p>
                ) : (
                  <div className="zoho-org-list">
                    {pendingOrgs.map((o) => {
                      const active = selectedOrg === o.organization_id;
                      return (
                        <button
                          key={o.organization_id}
                          type="button"
                          className={`zoho-org-option${active ? " active" : ""}`}
                          onClick={() => setSelectedOrg(o.organization_id)}
                        >
                          <span className="zoho-org-option-name">{o.name}</span>
                          <span className="zoho-org-option-id">{o.organization_id}</span>
                          {o.plan_name ? <span className="zoho-org-option-plan">{o.plan_name}</span> : null}
                        </button>
                      );
                    })}
                    <button type="button" className="btn primary" disabled={busy || !selectedOrg} onClick={saveOrg}>
                      Save organization
                    </button>
                  </div>
                )}
              </>
            ) : (
              <>
                <p className="zoho-muted" style={{ marginBottom: 14 }}>
                  Opens a secure Zoho login window, then returns here to pick an org. App credentials come from the
                  server environment.
                </p>
                <div className="zoho-dialog-actions">
                  <button type="button" className="btn" onClick={() => setModalOpen(false)}>
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="btn primary"
                    disabled={busy || signingIn || !canSignIn}
                    onClick={signIn}
                  >
                    <PlugIcon /> {signingIn ? "Waiting for Zoho…" : "Sign in with Zoho"}
                  </button>
                </div>
                {(status.last_error || status.connection_status === "error") && (
                  <p style={{ color: "#b82d40", marginTop: 12, fontSize: 13 }}>
                    {status.last_error || "Previous connection error — try signing in again."}
                  </p>
                )}
              </>
            )}

            <div className="zoho-secondary" style={{ marginTop: 18 }}>
              <button
                type="button"
                className="zoho-collapse-btn"
                onClick={() => setEndpointsOpen((v) => !v)}
                aria-expanded={endpointsOpen}
              >
                <span>Webhook &amp; callback URLs</span>
                <span className="zoho-chevron">{endpointsOpen ? "▾" : "▸"}</span>
              </button>
              {endpointsOpen && (
                <div style={{ padding: "0 0 4px" }}>
                  <EndpointsBlock
                    status={status}
                    secret={secret}
                    busy={busy}
                    onCopy={onCopy}
                    revealSecret={revealSecret}
                    rotateSecret={rotateSecret}
                  />
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {toast && <div className="emp-toast" role="status">{toast}</div>}
    </div>
  );
}

function EndpointsBlock({
  status,
  secret,
  busy,
  onCopy,
  revealSecret,
  rotateSecret,
}: {
  status: ZohoStatus;
  secret: string;
  busy: boolean;
  onCopy: (label: string, value: string) => void;
  revealSecret: () => void;
  rotateSecret: () => void;
}) {
  return (
    <div className="emp-fields">
      <div className="field full">
        <label>OAuth callback URL</label>
        <div className="zoho-copy-row">
          <input readOnly value={status.oauth_callback_url} />
          <button type="button" className="btn" onClick={() => onCopy("Callback URL", status.oauth_callback_url)}>
            Copy
          </button>
        </div>
      </div>
      <div className="field full">
        <label>Webhook URL (Zoho → CRM)</label>
        <div className="zoho-copy-row">
          <input readOnly value={status.webhook_url} />
          <button type="button" className="btn" onClick={() => onCopy("Webhook URL", status.webhook_url)}>
            Copy
          </button>
        </div>
      </div>
      <div className="field full">
        <label>Webhook secret header</label>
        <p style={{ margin: "0 0 8px", color: "var(--muted)", fontSize: 13 }}>
          Send as <code>X-Zoho-Webhook-Secret</code>. Hint: {status.webhook_secret_hint}
        </p>
        <div className="zoho-copy-row">
          <input readOnly type={secret ? "text" : "password"} value={secret || "••••••••••••••••••••••••"} />
          <button type="button" className="btn" disabled={busy} onClick={revealSecret}>
            Reveal
          </button>
          <button type="button" className="btn" disabled={!secret} onClick={() => onCopy("Webhook secret", secret)}>
            Copy
          </button>
          <button type="button" className="btn" disabled={busy} onClick={rotateSecret}>
            Rotate
          </button>
        </div>
      </div>
      <div className="field full">
        <label>Zoho Books webhook setup</label>
        <div style={{ fontSize: 13, color: "var(--muted)", lineHeight: 1.55 }}>
          <p style={{ margin: "0 0 8px" }}>
            In Zoho Books → Settings → Automation → Workflow Rules / Webhooks (org{" "}
            {status.zoho_org_number || status.zoho_org_id || "60001225303"}), create push notifications to the
            webhook URL above and include header <code>X-Zoho-Webhook-Secret</code> with the revealed secret.
          </p>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            <li>
              <strong>Sales (live):</strong> Invoice — created, edited, voided/cancelled
            </li>
            <li>
              <strong>Customers (required for live dealers):</strong> Contact/Customer — created, edited,
              deleted. CRM upserts into <code>dealers</code> / <code>zoho_dealers</code>. If only invoices
              fire today, add a separate Contact workflow in the Zoho portal — CRM is already ready.
            </li>
          </ul>
        </div>
      </div>
      <div className="field">
        <label>Accounts URL</label>
        <div style={{ fontSize: 13 }}>{status.accounts_url}</div>
      </div>
      <div className="field">
        <label>API base URL</label>
        <div style={{ fontSize: 13 }}>{status.api_base_url}</div>
      </div>
    </div>
  );
}
