import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { INDIA_STATES, districtsForState } from "../data/indiaGeo";

type Meta = {
  states: string[];
  eligible_states?: string[];
  excluded_states?: string[];
  all_india_gates?: Record<string, unknown>;
  categories: string[];
  search_depths: { id: string; label: string }[];
  google_attribution: string;
  expansion_path: string;
};

type AllIndiaTile = {
  state: string;
  job_status: string | null;
  job_public_id: string | null;
  places_new: number;
  progress_pct: number;
};

type AllIndiaRun = {
  public_id: string;
  mode: string;
  status: string;
  logs: string[];
  active_job_public_id?: string;
};

type Job = {
  id: number;
  public_id: string;
  status: string;
  state: string;
  total_units: number;
  completed_units: number;
  skipped_units: number;
  failed_units: number;
  places_found: number;
  places_new: number;
  api_requests: number;
  progress_pct: number;
  error?: string;
  search_depth?: string;
  results_ready?: boolean;
  estimated_cost_usd?: number;
};

type PlaceRow = {
  id: number;
  place_id: string;
  name: string;
  whatsapp: string;
  website: string;
  website_label: string;
  city: string;
  district: string;
  state: string;
  category: string;
  maps_url: string;
  lead_source: string;
  crm_status: string;
  eligible_import: boolean;
};

type HistoryRow = {
  id: number;
  state: string;
  district: string;
  city: string;
  category: string;
  status: string;
  results_count: number;
  with_phone_count: number;
  with_website_count: number;
  searched_at: string | null;
  previously_searched: boolean;
  place_ids: string[];
};

type StatusReport = {
  job_id?: number;
  public_id?: string;
  state?: string;
  status?: string;
  results_ready?: boolean;
  searched_units?: number;
  total_units?: number;
  skipped_units_history?: number;
  failed_units?: number;
  api_requests?: number;
  estimated_cost_usd?: number;
  unique_places?: number;
  existing_dealers?: number;
  existing_leads?: number;
  eligible?: number;
  with_website?: number;
  with_phone?: number;
  imported?: number;
  failed_or_skipped?: number;
  import_received?: number;
  error?: string;
};

type Owner = { id: number; name: string };

type TabId = "search" | "all_india" | "history" | "settings";

const RUNNING = new Set(["Pending", "Running"]);

export default function DealerFinder() {
  const [tab, setTab] = useState<TabId>("search");
  const [meta, setMeta] = useState<Meta | null>(null);
  const [owners, setOwners] = useState<Owner[]>([]);
  const [state, setState] = useState("Rajasthan");
  const [aiDashboard, setAiDashboard] = useState<{
    tiles: AllIndiaTile[];
    active_run: AllIndiaRun | null;
    gates?: Record<string, unknown>;
  } | null>(null);
  const [pilotState, setPilotState] = useState("Rajasthan");
  const [pilotDistrict, setPilotDistrict] = useState("Jaipur");
  const [pilotPreflight, setPilotPreflight] = useState<string>("");
  const [district, setDistrict] = useState("");
  const [city, setCity] = useState("");
  const [category, setCategory] = useState("");
  const [searchDepth, setSearchDepth] = useState("full");
  const [leadOwnerId, setLeadOwnerId] = useState<number | "">("");
  const [job, setJob] = useState<Job | null>(null);
  const [results, setResults] = useState<PlaceRow[]>([]);
  const [summary, setSummary] = useState<Record<string, number>>({});
  const [statusReport, setStatusReport] = useState<StatusReport | null>(null);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [settings, setSettings] = useState<Record<string, unknown> | null>(null);
  const [planInfo, setPlanInfo] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [confirmStart, setConfirmStart] = useState(false);
  const [confirmImport, setConfirmImport] = useState(false);
  const [importPreview, setImportPreview] = useState<string>("");
  const [otherJobHint, setOtherJobHint] = useState<string>("");

  const searchStates = useMemo(
    () => (meta?.eligible_states?.length ? meta.eligible_states : INDIA_STATES).filter(Boolean),
    [meta]
  );
  const districts = useMemo(() => districtsForState(state), [state]);
  const pilotDistricts = useMemo(() => districtsForState(pilotState), [pilotState]);
  const categories = meta?.categories || [];
  const resultsReady = Boolean(job?.results_ready) || (job ? !RUNNING.has(job.status) : false);
  const jobRunning = Boolean(job && RUNNING.has(job.status));

  const loadMeta = useCallback(async () => {
    const [m, leadMeta, s] = await Promise.all([
      api<Meta>("/api/dealer-finder/meta"),
      api<{ owners: Owner[] }>("/api/leads/meta"),
      api<Record<string, unknown>>("/api/dealer-finder/settings"),
    ]);
    setMeta(m);
    setOwners(leadMeta.owners || []);
    setSettings(s);
    if (!category && m.categories?.[0]) setCategory(m.categories[0]);
    const gates = m.all_india_gates as { pilot_defaults?: { state?: string; district?: string } };
    if (gates?.pilot_defaults?.state) setPilotState(gates.pilot_defaults.state);
    if (gates?.pilot_defaults?.district) setPilotDistrict(gates.pilot_defaults.district);
  }, [category]);

  const loadAllIndiaDashboard = useCallback(async () => {
    const d = await api<{
      tiles: AllIndiaTile[];
      active_run: AllIndiaRun | null;
      gates: Record<string, unknown>;
    }>("/api/dealer-finder/all-india/dashboard");
    setAiDashboard({ tiles: d.tiles || [], active_run: d.active_run, gates: d.gates });
  }, []);

  async function preflightPilot() {
    setBusy(true);
    setErr("");
    try {
      const r = await api<{ estimate: { total_units: number; estimated_marginal_usd: number } }>(
        `/api/dealer-finder/all-india/preflight?mode=pilot&state=${encodeURIComponent(pilotState)}&district=${encodeURIComponent(pilotDistrict)}`,
        { method: "POST" }
      );
      setPilotPreflight(
        `~${r.estimate.total_units} units · est. +$${r.estimate.estimated_marginal_usd} marginal`
      );
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function startPilot(confirmed: boolean) {
    setBusy(true);
    setErr("");
    try {
      const r = await api<{ ok: boolean; needs_confirm?: boolean; run?: AllIndiaRun; estimate?: { total_units: number } }>(
        "/api/dealer-finder/all-india/pilot",
        {
          method: "POST",
          body: JSON.stringify({
            state: pilotState,
            district: pilotDistrict,
            confirm: confirmed,
            acknowledge_cost: confirmed,
          }),
        }
      );
      if (r.needs_confirm) {
        setMsg(`Pilot preflight: ~${r.estimate?.total_units ?? "?"} units. Confirm to start.`);
        return;
      }
      setMsg(`Pilot started (${r.run?.public_id || ""})`);
      await loadAllIndiaDashboard();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  const refreshJob = useCallback(async (publicId: string) => {
    const r = await api<{ ok: boolean; job: Job }>(`/api/dealer-finder/jobs/${publicId}`);
    setJob(r.job);
    const ready = Boolean(r.job.results_ready) || !RUNNING.has(r.job.status);
    if (!ready) {
      // Full job in progress — progress tiles only; do not drip partial place lists.
      setResults([]);
      setSummary({});
      setStatusReport({
        status: r.job.status,
        searched_units: r.job.completed_units,
        total_units: r.job.total_units,
        api_requests: r.job.api_requests,
        estimated_cost_usd: r.job.estimated_cost_usd,
        skipped_units_history: r.job.skipped_units,
        failed_units: r.job.failed_units,
      });
      return;
    }
    const res = await api<{
      items: PlaceRow[];
      summary: Record<string, number>;
      status_report?: StatusReport;
      results_ready?: boolean;
      total?: number;
    }>(`/api/dealer-finder/jobs/${publicId}/results?filter=all&limit=2000`);
    setResults(res.items || []);
    setSummary(res.summary || {});
    setStatusReport(res.status_report || null);
  }, []);

  const loadLatestJobForState = useCallback(
    async (forState: string) => {
      const [forStateJobs, allJobs] = await Promise.all([
        api<{ items: Job[] }>(
          `/api/dealer-finder/jobs?state=${encodeURIComponent(forState)}&limit=5`
        ),
        api<{ items: Job[] }>("/api/dealer-finder/jobs?limit=10"),
      ]);
      const latest = forStateJobs.items?.[0];
      const other = (allJobs.items || []).find(
        (j) =>
          j.state !== forState &&
          (RUNNING.has(j.status) || j.status === "Paused" || j.status === "Partial")
      );
      if (other) {
        setOtherJobHint(
          `${other.state} job ${other.status}: ${other.completed_units}/${other.total_units} units` +
            (other.error ? ` — ${other.error}` : "") +
            `. Switch State to ${other.state} to view/resume results.`
        );
      } else {
        setOtherJobHint("");
      }
      if (!latest) {
        setJob(null);
        setResults([]);
        setSummary({});
        setStatusReport(null);
        return;
      }
      await refreshJob(latest.public_id);
      if (latest.status === "Paused" && latest.error) {
        setMsg(
          `Loaded latest ${forState} job (${latest.status}): ${latest.error}. ` +
            `Raise Cost controls budget then Resume, or review partial results below.`
        );
      } else if (!RUNNING.has(latest.status)) {
        setMsg(`Loaded latest ${forState} job · ${latest.status} · ${latest.places_found} places.`);
      } else {
        setMsg(
          `Loaded active ${forState} job · ${latest.completed_units}/${latest.total_units} units.`
        );
      }
    },
    [refreshJob]
  );

  useEffect(() => {
    document.title = "Dealer Finder · YesWeigh CRM";
    loadMeta().catch((e) => setErr(e instanceof Error ? e.message : "Failed to load"));
  }, [loadMeta]);

  useEffect(() => {
    loadLatestJobForState(state).catch((e) =>
      setErr(e instanceof Error ? e.message : "Failed to load jobs")
    );
  }, [state, loadLatestJobForState]);

  useEffect(() => {
    if (!job?.public_id) return;
    if (!RUNNING.has(job.status)) return;
    const t = window.setInterval(() => {
      refreshJob(job.public_id).catch(() => null);
    }, 2500);
    return () => window.clearInterval(t);
  }, [job?.public_id, job?.status, refreshJob]);

  async function ensurePlan() {
    setBusy(true);
    setErr("");
    try {
      const r = await api<{ ok: boolean; plan: { district_count: number; city_count: number; source: string } }>(
        "/api/dealer-finder/plans",
        {
          method: "POST",
          body: JSON.stringify({ state, force_refresh: false }),
        }
      );
      setPlanInfo(
        `${r.plan.district_count} districts · ${r.plan.city_count} cities/towns (${r.plan.source || "cached"})`
      );
      setMsg("Location plan ready (districts → cities cached).");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Plan failed");
    } finally {
      setBusy(false);
    }
  }

  async function startSearch(forceRefresh = false) {
    setBusy(true);
    setErr("");
    setMsg("");
    setConfirmImport(false);
    setImportPreview("");
    try {
      if (!confirmStart) {
        const preview = await api<{
          needs_confirm?: boolean;
          summary?: string;
          warning?: string;
        }>("/api/dealer-finder/jobs", {
          method: "POST",
          body: JSON.stringify({
            state,
            district,
            city,
            categories: category ? [category] : [],
            search_depth: searchDepth,
            lead_owner_id: leadOwnerId || null,
            force_refresh: forceRefresh,
            confirm: false,
          }),
        });
        setConfirmStart(true);
        setMsg(
          `${preview.summary || ""}\n${preview.warning || ""}\nClick Confirm & Search to start the FULL job.`
        );
        return;
      }
      const r = await api<{ ok: boolean; job: Job }>("/api/dealer-finder/jobs", {
        method: "POST",
        body: JSON.stringify({
          state,
          district,
          city,
          categories: category ? [category] : [],
          search_depth: searchDepth,
          lead_owner_id: leadOwnerId || null,
          force_refresh: forceRefresh,
          confirm: true,
        }),
      });
      setConfirmStart(false);
      setJob(r.job);
      setResults([]);
      setSummary({});
      setStatusReport(null);
      setMsg(
        `Full job started · ${r.job.total_units} units. Results appear when the batch completes.`
      );
      await refreshJob(r.job.public_id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Search failed");
    } finally {
      setBusy(false);
    }
  }

  async function jobAction(action: "pause" | "resume" | "cancel") {
    if (!job?.public_id) return;
    setBusy(true);
    try {
      const r = await api<{ job: Job }>(`/api/dealer-finder/jobs/${job.public_id}/${action}`, {
        method: "POST",
        body: "{}",
      });
      setJob(r.job);
      await refreshJob(r.job.public_id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Action failed");
    } finally {
      setBusy(false);
    }
  }

  async function importEligible() {
    if (!job) return;
    setBusy(true);
    setErr("");
    try {
      if (!confirmImport) {
        const preview = await api<{
          needs_confirm?: boolean;
          summary?: string;
          eligible?: number;
          status_report?: StatusReport;
        }>("/api/dealer-finder/import", {
          method: "POST",
          body: JSON.stringify({ job_id: job.id, confirm: false }),
        });
        setConfirmImport(true);
        setImportPreview(preview.summary || "");
        if (preview.status_report) setStatusReport(preview.status_report);
        setMsg(
          `${preview.summary || "Confirm import of eligible NEW places as Leads."}\nClick Confirm import to upload.`
        );
        return;
      }
      const r = await api<{
        inserted: number;
        skipped_duplicates: number;
        status_report?: StatusReport;
      }>("/api/dealer-finder/import", {
        method: "POST",
        body: JSON.stringify({ job_id: job.id, confirm: true }),
      });
      setConfirmImport(false);
      setImportPreview("");
      if (r.status_report) setStatusReport(r.status_report);
      setMsg(
        `Imported ${r.inserted} leads · skipped ${r.skipped_duplicates} duplicates. See job status report below.`
      );
      await refreshJob(job.public_id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Import failed");
    } finally {
      setBusy(false);
    }
  }

  async function loadHistory() {
    setBusy(true);
    try {
      const r = await api<{ items: HistoryRow[] }>(
        `/api/dealer-finder/history?state=${encodeURIComponent(state)}&limit=80`
      );
      setHistory(r.items || []);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "History failed");
    } finally {
      setBusy(false);
    }
  }

  async function refreshHistoryRow(id: number) {
    if (
      !window.confirm(
        "Refresh will call Google Places again for this location×category (API cost). Continue?"
      )
    )
      return;
    setBusy(true);
    try {
      const r = await api<{ ok: boolean; job?: Job; needs_confirm?: boolean }>(
        `/api/dealer-finder/history/${id}/refresh`,
        { method: "POST", body: JSON.stringify({ confirm_cost: true }) }
      );
      if (r.job) {
        setJob(r.job);
        setResults([]);
        setConfirmImport(false);
        setTab("search");
        setMsg("Refresh job started — full results when complete.");
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Refresh failed");
    } finally {
      setBusy(false);
    }
  }

  async function saveSettings(patch: Record<string, unknown>) {
    setBusy(true);
    try {
      const r = await api<Record<string, unknown>>("/api/dealer-finder/settings", {
        method: "PATCH",
        body: JSON.stringify(patch),
      });
      setSettings(r);
      setMsg("Settings saved.");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Settings save failed");
    } finally {
      setBusy(false);
    }
  }

  const eligibleCount = summary.eligible_import ?? statusReport?.eligible ?? 0;

  return (
    <div className="df-page">
      <header className="df-head">
        <div>
          <h1>Dealer Finder</h1>
          <p>
            {meta?.expansion_path ||
              "Gemini: all districts → cities per district → Places district + city × categories"}
          </p>
        </div>
        <div className="df-tabs" role="tablist">
          {(["search", "all_india", "history", "settings"] as TabId[]).map((id) => (
            <button
              key={id}
              type="button"
              className={`df-tab ${tab === id ? "active" : ""}`}
              onClick={() => {
                setTab(id);
                if (id === "history") loadHistory().catch(() => null);
                if (id === "all_india") loadAllIndiaDashboard().catch(() => null);
              }}
            >
              {id === "search"
                ? "Search"
                : id === "all_india"
                  ? "All India"
                  : id === "history"
                    ? "Search History"
                    : "Cost controls"}
            </button>
          ))}
        </div>
      </header>

      {err ? <div className="df-banner err">{err}</div> : null}
      {msg ? <div className="df-banner ok" style={{ whiteSpace: "pre-wrap" }}>{msg}</div> : null}
      {otherJobHint ? (
        <div className="df-banner ok" style={{ whiteSpace: "pre-wrap" }}>
          Other state: {otherJobHint}
        </div>
      ) : null}

      {meta?.excluded_states?.length ? (
        <p className="df-muted" style={{ margin: "0 0 12px" }}>
          Excluded from automatic discovery: {meta.excluded_states.join(", ")} (use existing CRM /
          manual jobs).
        </p>
      ) : null}

      {tab === "all_india" ? (
        <section className="panel df-all-india">
          <h2>All India Dealer Search</h2>
          <p>
            Sequential statewide jobs (one state at a time). Pilot one district first; admin enables
            full country after verification.
          </p>
          {aiDashboard?.active_run ? (
            <div className="df-banner ok">
              Active run {aiDashboard.active_run.mode} · {aiDashboard.active_run.status} · job{" "}
              {aiDashboard.active_run.active_job_public_id || "—"}
            </div>
          ) : null}
          <hr />
          <h3>Pilot district</h3>
          <div className="df-controls">
            <label>
              State
              <select value={pilotState} onChange={(e) => setPilotState(e.target.value)}>
                {searchStates.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </label>
            <label>
              District
              <select value={pilotDistrict} onChange={(e) => setPilotDistrict(e.target.value)}>
                {pilotDistricts.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {pilotPreflight ? <p className="df-muted">{pilotPreflight}</p> : null}
          <div className="df-actions">
            <button type="button" disabled={busy} onClick={() => preflightPilot()}>
              Preflight cost
            </button>
            <button type="button" disabled={busy} onClick={() => startPilot(false)}>
              Review pilot
            </button>
            <button type="button" className="primary" disabled={busy} onClick={() => startPilot(true)}>
              Start pilot (confirmed)
            </button>
          </div>
          {Boolean(
            (aiDashboard?.gates as { pilot_district_completed?: boolean })?.pilot_district_completed
          ) ? (
            <p className="df-banner ok">Pilot district completed — admin may enable full all-India.</p>
          ) : null}
          <h3 style={{ marginTop: 24 }}>State overview</h3>
          <div className="df-tiles" style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {(aiDashboard?.tiles || []).slice(0, 40).map((t) => (
              <button
                key={t.state}
                type="button"
                className="df-tile"
                style={{ minWidth: 140, textAlign: "left" }}
                onClick={() => {
                  setState(t.state);
                  setTab("search");
                }}
              >
                <strong>{t.state}</strong>
                <br />
                {t.job_status || "—"} · NEW {t.places_new}
              </button>
            ))}
          </div>
        </section>
      ) : null}

      {tab === "search" ? (
        <>
          <section className="df-controls panel">
            <label>
              State
              <select
                value={state}
                onChange={(e) => {
                  setState(e.target.value);
                  setDistrict("");
                  setCity("");
                  setConfirmStart(false);
                  setConfirmImport(false);
                  setPlanInfo("");
                }}
              >
                {searchStates.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </label>
            <label>
              District
              <select value={district} onChange={(e) => setDistrict(e.target.value)}>
                <option value="">All districts</option>
                {districts.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
            </label>
            <label>
              City
              <input
                value={city}
                onChange={(e) => setCity(e.target.value)}
                placeholder="Optional city / town"
              />
            </label>
            <label>
              Category
              <select value={category} onChange={(e) => setCategory(e.target.value)}>
                <option value="">All categories</option>
                {categories.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Search depth
              <select value={searchDepth} onChange={(e) => setSearchDepth(e.target.value)}>
                {(meta?.search_depths || []).map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Lead owner
              <select
                value={leadOwnerId}
                onChange={(e) => setLeadOwnerId(e.target.value ? Number(e.target.value) : "")}
              >
                <option value="">Me (default)</option>
                {owners.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.name}
                  </option>
                ))}
              </select>
            </label>
            <div className="df-actions">
              <button type="button" className="btn" disabled={busy} onClick={() => ensurePlan()}>
                Build location plan
              </button>
              <button
                type="button"
                className="btn primary"
                disabled={busy || jobRunning}
                onClick={() => startSearch(false)}
              >
                {confirmStart ? "Confirm & Search" : "Search"}
              </button>
              <button
                type="button"
                className="btn"
                disabled={busy || jobRunning}
                onClick={() => startSearch(true)}
              >
                Refresh (ignore cooldown)
              </button>
            </div>
            {planInfo ? <p className="df-plan-info">Plan: {planInfo}</p> : null}
          </section>

          {job ? (
            <section className="df-summary">
              <div className="df-tile">
                <span>Status</span>
                <strong>{job.status}</strong>
              </div>
              <div className="df-tile">
                <span>Progress</span>
                <strong>
                  {job.completed_units}/{job.total_units} ({job.progress_pct}%)
                </strong>
              </div>
              <div className="df-tile">
                <span>API requests</span>
                <strong>{job.api_requests}</strong>
              </div>
              <div className="df-tile">
                <span>Skipped (history)</span>
                <strong>{job.skipped_units}</strong>
              </div>
              <div className="df-tile">
                <span>Est. cost</span>
                <strong>~${(job.estimated_cost_usd ?? 0).toFixed(2)}</strong>
              </div>
              {resultsReady ? (
                <>
                  <div className="df-tile">
                    <span>Unique places</span>
                    <strong>{summary.unique_places ?? job.places_found}</strong>
                  </div>
                  <div className="df-tile">
                    <span>Eligible NEW</span>
                    <strong>{eligibleCount}</strong>
                  </div>
                </>
              ) : (
                <div className="df-tile">
                  <span>Results</span>
                  <strong>After full job</strong>
                </div>
              )}
              <div className="df-job-actions">
                <button type="button" className="btn" disabled={busy} onClick={() => jobAction("pause")}>
                  Pause
                </button>
                <button type="button" className="btn" disabled={busy} onClick={() => jobAction("resume")}>
                  Resume
                </button>
                <button type="button" className="btn" disabled={busy} onClick={() => jobAction("cancel")}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn primary"
                  disabled={busy || !resultsReady || eligibleCount < 1}
                  onClick={() => importEligible()}
                >
                  {confirmImport ? "Confirm import" : "Import eligible"}
                </button>
              </div>
              {importPreview && confirmImport ? (
                <p className="df-plan-info">{importPreview}</p>
              ) : null}
              {job.error ? <p className="df-job-err">{job.error}</p> : null}
            </section>
          ) : null}

          {statusReport && (resultsReady || statusReport.imported != null) ? (
            <section className="panel df-status-report">
              <h2>Job status report</h2>
              <ul className="df-report-list">
                <li>
                  Searched units: {statusReport.searched_units ?? 0}/{statusReport.total_units ?? 0}
                </li>
                <li>API calls: {statusReport.api_requests ?? 0}</li>
                <li>Unique places: {statusReport.unique_places ?? 0}</li>
                <li>Existing dealers: {statusReport.existing_dealers ?? 0}</li>
                <li>Existing leads: {statusReport.existing_leads ?? 0}</li>
                <li>Eligible: {statusReport.eligible ?? 0}</li>
                <li>Imported: {statusReport.imported ?? 0}</li>
                <li>Failed/skipped: {statusReport.failed_or_skipped ?? statusReport.failed_units ?? 0}</li>
                <li>
                  Est. cost: ~${Number(statusReport.estimated_cost_usd ?? 0).toFixed(2)}
                </li>
              </ul>
            </section>
          ) : null}

          <section className="panel df-results">
            <div className="df-results-head">
              <h2>{resultsReady ? "All results" : "Results"}</h2>
              {resultsReady ? (
                <p>
                  Unique: {summary.unique_places ?? results.length} · Eligible:{" "}
                  {summary.eligible_import ?? 0} · With website: {summary.with_website ?? 0} ·
                  With phone: {summary.with_phone ?? 0} · Dealers: {summary.already_dealer ?? 0} ·
                  Leads: {summary.already_lead ?? 0}
                </p>
              ) : (
                <p>
                  {jobRunning
                    ? "Full job running — place list withheld until every district/city×category unit finishes (or pause/fail)."
                    : "No completed job yet. Build a location plan, then Search."}
                </p>
              )}
            </div>
            <div className="table-wrap">
              <table className="df-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>WhatsApp</th>
                    <th>Website</th>
                    <th>City</th>
                    <th>District</th>
                    <th>State</th>
                    <th>Category</th>
                    <th>Maps</th>
                    <th>Place ID</th>
                    <th>Source</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {resultsReady
                    ? results.map((r) => (
                        <tr key={r.id}>
                          <td>{r.name || "—"}</td>
                          <td>{r.whatsapp || "—"}</td>
                          <td>
                            {r.website ? (
                              <a href={r.website} target="_blank" rel="noreferrer">
                                Visit Website
                              </a>
                            ) : (
                              <span className="df-muted">Not available</span>
                            )}
                          </td>
                          <td>{r.city || "—"}</td>
                          <td>{r.district || "—"}</td>
                          <td>{r.state || "—"}</td>
                          <td>{r.category || "—"}</td>
                          <td>
                            {r.maps_url ? (
                              <a href={r.maps_url} target="_blank" rel="noreferrer">
                                Maps
                              </a>
                            ) : (
                              "—"
                            )}
                          </td>
                          <td className="df-mono">{r.place_id || "—"}</td>
                          <td>{r.lead_source || "Google"}</td>
                          <td>
                            <span className={`df-status ${r.crm_status}`}>{r.crm_status}</span>
                          </td>
                        </tr>
                      ))
                    : null}
                  {!resultsReady || !results.length ? (
                    <tr>
                      <td colSpan={11} className="df-empty">
                        {jobRunning
                          ? "Searching… full unique-place list appears when the job completes."
                          : resultsReady
                            ? "No places found for this job."
                            : "No results yet. Build a location plan, then Confirm & Search."}
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
            <p className="df-attr">{meta?.google_attribution}</p>
          </section>
        </>
      ) : null}

      {tab === "history" ? (
        <section className="panel df-history">
          <div className="df-results-head">
            <h2>Search History</h2>
            <button type="button" className="btn" disabled={busy} onClick={() => loadHistory()}>
              Reload
            </button>
          </div>
          <p className="df-hint">
            District/city/category tracked independently. Statewide ≠ all cities covered. Refresh
            requires cost confirmation.
          </p>
          <div className="table-wrap">
            <table className="df-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>State</th>
                  <th>District</th>
                  <th>City</th>
                  <th>Category</th>
                  <th>Status</th>
                  <th>Results</th>
                  <th>Phone</th>
                  <th>Website</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {history.map((h) => (
                  <tr key={h.id}>
                    <td>{h.searched_at ? new Date(h.searched_at).toLocaleString() : "—"}</td>
                    <td>{h.state}</td>
                    <td>{h.district || "—"}</td>
                    <td>{h.city || "—"}</td>
                    <td>{h.category}</td>
                    <td>{h.status}</td>
                    <td>{h.results_count}</td>
                    <td>{h.with_phone_count}</td>
                    <td>{h.with_website_count}</td>
                    <td>
                      <button type="button" className="btn" onClick={() => refreshHistoryRow(h.id)}>
                        Refresh
                      </button>
                    </td>
                  </tr>
                ))}
                {!history.length ? (
                  <tr>
                    <td colSpan={10} className="df-empty">
                      No history for this state yet.
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {tab === "settings" ? (
        <section className="panel df-settings">
          <h2>Cost controls</h2>
          {settings ? (
            <div className="df-settings-grid">
              <p className="df-usage" style={{ gridColumn: "1 / -1" }}>
                India Places (New) per-SKU free caps (Text Search Pro{" "}
                <strong>35k</strong>, Place Details Enterprise <strong>7k</strong>/mo).{" "}
                <strong>Console</strong> shows INR usage cost; savings can make subtotal ₹0.{" "}
                <strong>CRM ops cap</strong> ($
                {Number(settings.max_monthly_spend_usd ?? 100).toFixed(0)}) uses billable-after-caps
                estimate only — not your invoice. See Settings → Google Place for gross vs billable.
              </p>
              <label>
                Cooldown days
                <input
                  type="number"
                  defaultValue={Number(settings.cooldown_days || 30)}
                  onBlur={(e) => saveSettings({ cooldown_days: Number(e.target.value) })}
                />
              </label>
              <label>
                Free requests / month
                <input
                  type="number"
                  defaultValue={Number(
                    (settings as { free_requests_per_month?: number }).free_requests_per_month ??
                      settings.max_requests_per_month ??
                      7000
                  )}
                  onBlur={(e) =>
                    saveSettings({ free_requests_per_month: Number(e.target.value) })
                  }
                />
              </label>
              <label>
                Max paid spend / month ($)
                <input
                  type="number"
                  step="0.01"
                  defaultValue={Number(settings.max_monthly_spend_usd || 100)}
                  onBlur={(e) => saveSettings({ max_monthly_spend_usd: Number(e.target.value) })}
                />
              </label>
              <label>
                Max requests / job (ops)
                <input
                  type="number"
                  defaultValue={Number(settings.max_requests_per_job || 100000)}
                  onBlur={(e) => saveSettings({ max_requests_per_job: Number(e.target.value) })}
                />
              </label>
              <label>
                Max requests / day (ops)
                <input
                  type="number"
                  defaultValue={Number(settings.max_requests_per_day || 100000)}
                  onBlur={(e) => saveSettings({ max_requests_per_day: Number(e.target.value) })}
                />
              </label>
              <label>
                Expensive fields
                <select
                  defaultValue={String(settings.expensive_field_policy || "when_needed")}
                  onChange={(e) => saveSettings({ expensive_field_policy: e.target.value })}
                >
                  <option value="when_needed">When needed (Stage-2 phone/website)</option>
                  <option value="always">Always</option>
                  <option value="never">Never (discovery only)</option>
                </select>
              </label>
              <label className="df-check">
                <input
                  type="checkbox"
                  defaultChecked={Boolean(settings.persist_website_on_import)}
                  onChange={(e) => saveSettings({ persist_website_on_import: e.target.checked })}
                />
                Persist website on lead import
              </label>
              <label className="df-check">
                <input
                  type="checkbox"
                  defaultChecked={Boolean(
                    (settings as { all_india_enabled?: boolean }).all_india_enabled
                  )}
                  onChange={(e) => saveSettings({ all_india_enabled: e.target.checked })}
                />
                Enable full all-India discovery (admin, after pilot)
              </label>
              {settings.usage ? (
                <p className="df-usage">
                  Month{" "}
                  {(settings.usage as { month_requests?: number }).month_requests ?? 0} API req (S1{" "}
                  {(settings.usage as { month_stage1?: number }).month_stage1 ?? 0} · S2{" "}
                  {(settings.usage as { month_stage2?: number }).month_stage2 ?? 0}) · gross list ~
                  {(
                    settings.usage as { month_gross_india_list_inr?: number }
                  ).month_gross_india_list_inr?.toFixed?.(0) ?? "—"}{" "}
                  INR (≈ Console usage cost) · ops throttle ~
                  {(
                    settings.usage as { month_billable_estimated_usd?: number }
                  ).month_billable_estimated_usd?.toFixed?.(0) ??
                    (
                      settings.usage as { month_paid_estimated_usd?: number }
                    ).month_paid_estimated_usd?.toFixed?.(0) ??
                    "0"}
                  /$
                  {Number(settings.max_monthly_spend_usd ?? 100).toFixed(0)} billable USD cap
                </p>
              ) : null}
            </div>
          ) : (
            <p>Loading…</p>
          )}
        </section>
      ) : null}
    </div>
  );
}
