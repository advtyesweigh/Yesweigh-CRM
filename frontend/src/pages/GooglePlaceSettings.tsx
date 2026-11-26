import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api/client";

type Usage = {
  today_stage1?: number;
  today_stage2?: number;
  today_requests?: number;
  month_stage1?: number;
  month_stage2?: number;
  month_requests?: number;
  month_estimated_usd?: number;
  month_billable_estimated_usd?: number;
  month_billable_estimated_inr?: number;
  month_stage1_free_cap?: number;
  month_stage2_free_cap?: number;
  month_stage1_free_remaining?: number;
  month_stage2_free_remaining?: number;
  month_stage1_billable?: number;
  month_stage2_billable?: number;
  month_paid_estimated_usd?: number;
  month_gross_estimated_usd?: number;
  month_gross_india_list_usd?: number;
  month_gross_india_list_inr?: number;
  month_console_gross_estimated_inr?: number;
  month_console_other_savings_inr?: number;
  month_console_net_subtotal_inr?: number;
  month_console_net_estimated_inr?: number;
  month_console_sku_id?: string;
  month_console_sku_usage_count?: number;
  month_console_inr_per_request_gross?: number;
  month_console_inr_per_request_net?: number;
  month_console_billable_gross_inr?: number;
  recommended_ops_cap_all_india_usd?: number;
  max_monthly_spend_usd?: number;
};

type Job = {
  public_id: string;
  status: string;
  state: string;
  api_requests: number;
  places_found: number;
  places_new: number;
  total_units: number;
  completed_units: number;
  progress_pct: number;
  started_at?: string | null;
  created_at?: string | null;
};

type SettingsPayload = {
  max_monthly_spend_usd?: number;
  usage?: Usage;
  recent_jobs?: Job[];
  cost_model?: { source_of_truth?: string };
};

type Me = { role: string };

function fmtN(n: number) {
  return n.toLocaleString("en-US");
}

function fmtUsd(n: number, approx = false) {
  const p = `$${n.toFixed(2)}`;
  return approx && n > 0 ? `~${p}` : p;
}

function fmtInr(n: number, approx = false) {
  const p = `₹${n.toFixed(2)}`;
  return approx && n > 0 ? `~${p}` : p;
}

function fmtInrSigned(n: number) {
  const abs = Math.abs(n);
  const body = `₹${abs.toFixed(2)}`;
  return n < 0 ? `-${body}` : body;
}

function fmtUtc(iso?: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = String(d.getUTCDate()).padStart(2, "0");
  const h = String(d.getUTCHours()).padStart(2, "0");
  const min = String(d.getUTCMinutes()).padStart(2, "0");
  return `${y}-${m}-${day} ${h}:${min} UTC`;
}

function meterPct(used: number, cap: number) {
  if (cap <= 0) return 0;
  return Math.max(0, Math.min(100, (used / cap) * 100));
}

function meterTone(pct: number): "ok" | "warn" | "crit" {
  if (pct >= 100) return "crit";
  if (pct >= 85) return "warn";
  return "ok";
}

function ProgressMeter({
  label,
  pct,
  caption,
}: {
  label: string;
  pct: number;
  caption: string;
}) {
  const tone = meterTone(pct);
  return (
    <div className="gp-meter" aria-label={label}>
      <div
        className="gp-meter-track"
        role="progressbar"
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div className={`gp-meter-fill gp-${tone}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="gp-meter-caption">{caption}</span>
    </div>
  );
}

function KvTable({ rows }: { rows: { k: string; v: ReactNode; muted?: boolean }[] }) {
  return (
    <table className="gp-kv">
      <tbody>
        {rows.map((r) => (
          <tr key={r.k} className={r.muted ? "gp-kv-muted" : undefined}>
            <th scope="row">{r.k}</th>
            <td>{r.v}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const RUNNING = new Set(["Pending", "Running"]);
const POLL_MS = 12_000;

const S1_FREE_DEFAULT = 35_000;
const S2_FREE_DEFAULT = 7_000;

export default function GooglePlaceSettings() {
  const [data, setData] = useState<SettingsPayload | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [isAdmin, setIsAdmin] = useState(false);
  const [capDraft, setCapDraft] = useState("");
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [saving, setSaving] = useState(false);

  const monthLabel = useMemo(() => {
    return new Date().toLocaleString("en-US", { month: "short", year: "numeric", timeZone: "UTC" });
  }, []);

  const load = useCallback(async () => {
    const [s, j, me] = await Promise.all([
      api<SettingsPayload>("/api/dealer-finder/settings"),
      api<{ items: Job[] }>("/api/dealer-finder/jobs?limit=10"),
      api<Me>("/api/me").catch(() => ({ role: "" })),
    ]);
    const role = (me.role || "").toLowerCase();
    setIsAdmin(
      role === "admin" || role === "super_admin" || role === "super admin" || role === "management"
    );
    setData(s);
    setJobs(j.items?.length ? j.items : s.recent_jobs || []);
    if (s.max_monthly_spend_usd != null) {
      setCapDraft(String(s.max_monthly_spend_usd));
    }
  }, []);

  useEffect(() => {
    load().catch((e) => setErr(e instanceof Error ? e.message : "Failed to load Google Places usage"));
  }, [load]);

  const hasRunning = jobs.some((j) => RUNNING.has(j.status));

  useEffect(() => {
    if (!hasRunning) return;
    const t = window.setInterval(() => {
      load().catch(() => null);
    }, POLL_MS);
    return () => window.clearInterval(t);
  }, [hasRunning, load]);

  const usage = data?.usage;
  const s1Cap = Number(usage?.month_stage1_free_cap ?? S1_FREE_DEFAULT);
  const s2Cap = Number(usage?.month_stage2_free_cap ?? S2_FREE_DEFAULT);
  const s1 = Number(usage?.month_stage1 ?? 0);
  const s2 = Number(usage?.month_stage2 ?? 0);
  const monthReq = Number(usage?.month_requests ?? s1 + s2);
  const billableUsd = Number(
    usage?.month_billable_estimated_usd ?? usage?.month_paid_estimated_usd ?? usage?.month_estimated_usd ?? 0
  );
  const billableInr = Number(usage?.month_billable_estimated_inr ?? 0);
  const spendCap = Number(usage?.max_monthly_spend_usd ?? data?.max_monthly_spend_usd ?? 100);
  const spendRemain = Math.max(0, spendCap - billableUsd);
  const usListRef = Number(usage?.month_gross_estimated_usd ?? 0);
  const consoleGrossInr = Number(usage?.month_console_gross_estimated_inr ?? 0);
  const consoleSavingsInr = Number(usage?.month_console_other_savings_inr ?? 0);
  const consoleNetInr = Number(
    usage?.month_console_net_subtotal_inr ?? usage?.month_console_net_estimated_inr ?? 0
  );
  const consoleSkuId = usage?.month_console_sku_id ?? "448A-2967-E6BF";
  const consoleSkuUsage = Number(usage?.month_console_sku_usage_count ?? s2);
  const consolePerReqGross = Number(usage?.month_console_inr_per_request_gross ?? 0.516);
  const consolePerReqNet = Number(usage?.month_console_inr_per_request_net ?? 0.123);
  const consoleBillableGrossInr = Number(usage?.month_console_billable_gross_inr ?? 0);
  const recommendedCap = Number(usage?.recommended_ops_cap_all_india_usd ?? 2000);
  const grossIndiaInr = Number(
    usage?.month_gross_india_list_inr ??
      (usage?.month_gross_india_list_usd != null
        ? usage.month_gross_india_list_usd * 84
        : 0)
  );
  const grossIndiaUsd = Number(
    usage?.month_gross_india_list_usd ?? (grossIndiaInr > 0 ? grossIndiaInr / 84 : 0)
  );

  async function saveCap() {
    if (!isAdmin) return;
    const v = Number(capDraft);
    if (!Number.isFinite(v) || v < 0) {
      setErr("Enter a valid monthly cap (USD).");
      return;
    }
    setSaving(true);
    setErr("");
    try {
      await api("/api/dealer-finder/settings", {
        method: "PATCH",
        body: JSON.stringify({ max_monthly_spend_usd: v }),
      });
      setMsg("Monthly ops cap saved.");
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="gp-page">
      <header className="gp-head">
        <motionDiv />
        <div>
          <h2>Google Place</h2>
          <p>
            Monthly Places API usage for <strong>yesweigh</strong> — India SKU free caps, estimated
            billable spend, and dealer-finder jobs. {monthLabel} (UTC).
          </p>
        </div>
      </header>

      {err ? <div className="gp-banner err">{err}</div> : null}
      {msg ? <div className="gp-banner ok">{msg}</div> : null}

      {!data || !usage ? (
        <section className="panel gp-section">Loading…</section>
      ) : (
        <>
          <section className="panel gp-section gp-callout">
            <h3>Console vs CRM numbers</h3>
            <p className="gp-note">
              <strong>Google Cloud Console</strong> (invoice): SKU{" "}
              <code>{consoleSkuId}</code> — <em>usage cost</em> (gross),{" "}
              <em>Other savings</em> (credits), <strong>subtotal</strong> (net you pay). Calibrated
              from ₹37,522.52 gross / 72,670 requests (≈ ₹0.516 req gross, ≈ ₹0.123 net).
            </p>
            <p className="gp-note">
              <strong>CRM</strong> applies those Console rates to your live stage-2 count.{" "}
              <code>max_monthly_spend_usd</code> is an internal <strong>ops throttle</strong> only —
              not the Console bill.
            </p>
          </section>

          <section className="panel gp-section">
            <h3>1. India free SKUs (per month)</h3>
            <KvTable
              rows={[
                {
                  k: "Stage 1 — Text Search Pro (India)",
                  v: (
                    <strong>
                      {fmtN(s1)} / {fmtN(s1Cap)} free
                      <span className="gp-inline-muted">
                        {" "}
                        ({fmtN(usage.month_stage1_free_remaining ?? Math.max(0, s1Cap - s1))} left)
                      </span>
                    </strong>
                  ),
                },
                {
                  k: "Stage 2 — Place Details Enterprise (India)",
                  v: (
                    <strong>
                      {fmtN(s2)} / {fmtN(s2Cap)} free
                      <span className="gp-inline-muted">
                        {" "}
                        ({fmtN(usage.month_stage2_free_remaining ?? Math.max(0, s2Cap - s2))} left)
                      </span>
                    </strong>
                  ),
                },
                {
                  k: "Billable events (after SKU caps)",
                  v: (
                    <strong>
                      S1 {fmtN(usage.month_stage1_billable ?? Math.max(0, s1 - s1Cap))} · S2{" "}
                      {fmtN(usage.month_stage2_billable ?? Math.max(0, s2 - s2Cap))}
                    </strong>
                  ),
                },
              ]}
            />
            <ProgressMeter
              label="Text Search Pro free"
              pct={meterPct(s1, s1Cap)}
              caption={`${fmtN(s1)} of ${fmtN(s1Cap)} Text Search Pro free events`}
            />
            <ProgressMeter
              label="Place Details Enterprise free"
              pct={meterPct(s2, s2Cap)}
              caption={`${fmtN(s2)} of ${fmtN(s2Cap)} Place Details Enterprise free events`}
            />
          </section>

          <section className="panel gp-section gp-console-bill">
            <h3>2. Console bill — Place Details Enterprise (INR)</h3>
            <p className="gp-note">
              Matches Cloud Console row for SKU <code>{consoleSkuId}</code> (stage-2 requests). India
              published list price is shown below for comparison only — it is <strong>not</strong>{" "}
              the invoice.
            </p>
            <KvTable
              rows={[
                {
                  k: "SKU",
                  v: (
                    <strong>
                      <code>{consoleSkuId}</code>
                    </strong>
                  ),
                },
                {
                  k: "Usage (stage 2 count)",
                  v: <strong>{fmtN(consoleSkuUsage)}</strong>,
                },
                {
                  k: "Usage cost (gross)",
                  v: <strong>{fmtInr(consoleGrossInr, true)}</strong>,
                },
                {
                  k: "Other savings",
                  v: (
                    <strong>
                      {consoleSavingsInr !== 0 ? fmtInrSigned(consoleSavingsInr) : "—"}
                    </strong>
                  ),
                },
                {
                  k: "Subtotal (net)",
                  v: <strong className="gp-net-primary">{fmtInr(consoleNetInr, true)}</strong>,
                },
                {
                  k: "Effective rate (gross / net per req.)",
                  v: (
                    <strong>
                      {fmtInr(consolePerReqGross, false)} / {fmtInr(consolePerReqNet, false)}
                    </strong>
                  ),
                },
                {
                  k: "Gross on billable S2 only (after 7k free)",
                  v: <strong>{fmtInr(consoleBillableGrossInr, true)}</strong>,
                  muted: true,
                },
                {
                  k: "India list reference — S1+S2 (not invoice)",
                  v: <strong>{fmtInr(grossIndiaInr, true)}</strong>,
                  muted: true,
                },
              ]}
            />
          </section>

          <section className="panel gp-section">
            <h3>3. Ops throttle (billable after free caps)</h3>
            <p className="gp-note">
              Used for the <code>max_monthly_spend_usd</code> cap only — events beyond India per-SKU
              monthly free caps at India list price. <strong>Not your Console invoice.</strong>
            </p>
            <KvTable
              rows={[
                {
                  k: "CRM billable estimate (INR)",
                  v: <strong>{fmtInr(billableInr, true)}</strong>,
                },
                {
                  k: "CRM billable estimate (USD)",
                  v: <strong>{fmtUsd(billableUsd, true)}</strong>,
                },
                {
                  k: "Monthly ops cap (USD)",
                  v: isAdmin ? (
                    <span className="gp-cap-edit">
                      <span>$</span>
                      <input
                        type="number"
                        min={0}
                        step={0.01}
                        value={capDraft}
                        onChange={(e) => setCapDraft(e.target.value)}
                        aria-label="Monthly paid cap USD"
                      />
                      <button type="button" className="btn" disabled={saving} onClick={() => saveCap()}>
                        Save
                      </button>
                    </span>
                  ) : (
                    <strong>{fmtUsd(spendCap)}</strong>
                  ),
                },
                {
                  k: "Remaining before ops cap",
                  v: <strong>{fmtUsd(spendRemain, true)}</strong>,
                },
              ]}
            />
            <ProgressMeter
              label="Ops spend cap"
              pct={meterPct(billableUsd, spendCap)}
              caption={`${fmtUsd(billableUsd, true)} of ${fmtUsd(spendCap)} ops cap (India billable model)`}
            />
            <p className="gp-foot-muted">
              All-India full runs: keep ops cap near {fmtUsd(recommendedCap)} so throttle does not stop
              scans while Console net is ₹0. US list reference (not billed): {fmtUsd(usListRef, true)}.
            </p>
          </section>

          <section className="panel gp-section">
            <h3>4. Total requests this month</h3>
            <KvTable
              rows={[
                {
                  k: "Month total",
                  v: <strong>{fmtN(monthReq)}</strong>,
                },
                {
                  k: "Stage 1 (text search)",
                  v: <strong>{fmtN(s1)}</strong>,
                },
                {
                  k: "Stage 2 (details)",
                  v: <strong>{fmtN(s2)}</strong>,
                },
                {
                  k: "Today",
                  v: (
                    <strong>
                      {fmtN(usage.today_requests ?? 0)}
                      <span className="gp-inline-muted">
                        {" "}
                        (S1 {fmtN(usage.today_stage1 ?? 0)} + S2 {fmtN(usage.today_stage2 ?? 0)})
                      </span>
                    </strong>
                  ),
                },
              ]}
            />
            <p className="gp-foot-muted">Source: <code>dealer_places_usage</code> for organization yesweigh.</p>
          </section>

          <section className="panel gp-section">
            <h3>5. Active / recent jobs</h3>
            <div className="table-wrap">
              <table className="gp-jobs">
                <thead>
                  <tr>
                    <th>Job</th>
                    <th>State</th>
                    <th>Status</th>
                    <th>API req.</th>
                    <th>Places found / new</th>
                    <th>Units</th>
                    <th>Started</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((j) => (
                    <tr key={j.public_id} className={RUNNING.has(j.status) ? "gp-job-live" : undefined}>
                      <td>
                        <code className="gp-mono">{j.public_id}</code>
                      </td>
                      <td>{j.state}</td>
                      <td>
                        <strong>{j.status}</strong>
                      </td>
                      <td>{fmtN(j.api_requests ?? 0)}</td>
                      <td>
                        {fmtN(j.places_found ?? 0)} / {fmtN(j.places_new ?? 0)}
                      </td>
                      <td>
                        {j.completed_units ?? 0}/{j.total_units ?? 0}
                        {j.total_units ? (
                          <span className="gp-inline-muted"> ({j.progress_pct ?? 0}%)</span>
                        ) : null}
                      </td>
                      <td>{fmtUtc(j.started_at || j.created_at)}</td>
                    </tr>
                  ))}
                  {!jobs.length ? (
                    <tr>
                      <td colSpan={7} className="gp-empty">
                        No dealer-finder jobs yet.
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </div>
  );
}

/** Decorative map pin — keeps header visually aligned with other Settings tabs */
function motionDiv() {
  return (
    <div className="gp-ico" aria-hidden>
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M12 21s7-4.5 7-11a7 7 0 1 0-14 0c0 6.5 7 11 7 11z" />
        <circle cx="12" cy="10" r="2.5" />
      </svg>
    </div>
  );
}