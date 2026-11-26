import { useCallback, useEffect, useMemo, useState, type ReactNode, type SVGProps } from "react";
import { api } from "../api/client";

type UsageRow = {
  id: number;
  created_at: string | null;
  purpose: string;
  source?: "whatsapp" | "agent" | "other";
  provider: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  thoughts_tokens: number;
  estimated_cost_inr: number;
  duration_ms: number;
  success: boolean;
  employee_id: number | null;
  employee_name?: string | null;
  user_id?: number | null;
  user_name?: string | null;
  display_name?: string | null;
  conversation_id: number | null;
  error: string | null;
};

type SourceBucket = {
  requests: number;
  spend_inr: number;
  total_tokens: number;
};

type UsagePayload = {
  spent_today_inr: number;
  spent_month_inr: number;
  daily_budget_inr: number;
  monthly_budget_inr: number;
  remaining_monthly_inr: number;
  requests_today: number;
  requests_month: number;
  success_today?: number;
  failed_today?: number;
  success_month?: number;
  failed_month?: number;
  success_rate_today?: number;
  success_rate_month?: number;
  input_tokens_today?: number;
  output_tokens_today?: number;
  total_tokens_today?: number;
  thoughts_tokens_today?: number;
  input_tokens_month: number;
  output_tokens_month: number;
  total_tokens_month?: number;
  thoughts_tokens_month?: number;
  by_source_today?: Record<string, SourceBucket>;
  by_source_month?: Record<string, SourceBucket>;
  recent?: UsageRow[];
};

type AiSettingsPayload = {
  configured: boolean;
  status: string;
  status_label: string;
  provider: string;
  model: string;
  monthly_budget_inr: number;
  daily_budget_inr: number;
  usage: UsagePayload;
  google_places?: {
    configured: boolean;
    status_label: string;
    env_hint?: string;
  };
};

type Period = "month" | "today";
type FeatureFilter = "all" | "whatsapp" | "agent";
type StatusFilter = "all" | "ok" | "fail";
type RangeFilter = "7d" | "30d" | "all";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function Ico({ size = 16, children, ...rest }: IconProps & { children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      {...rest}
    >
      {children}
    </svg>
  );
}

function BarChartIcon(p: IconProps) {
  return (
    <Ico {...p}>
      <path d="M3 3v18h18" />
      <path d="M7 16v-5" />
      <path d="M12 16V8" />
      <path d="M17 16v-3" />
    </Ico>
  );
}

function MessageIcon(p: IconProps) {
  return (
    <Ico {...p}>
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </Ico>
  );
}

function CoinsIcon(p: IconProps) {
  return (
    <Ico {...p}>
      <ellipse cx="12" cy="6" rx="7" ry="3" />
      <path d="M5 6v4c0 1.7 3.1 3 7 3s7-1.3 7-3V6" />
      <path d="M5 10v4c0 1.7 3.1 3 7 3s7-1.3 7-3v-4" />
    </Ico>
  );
}

function RupeeIcon(p: IconProps) {
  return (
    <Ico {...p}>
      <circle cx="12" cy="12" r="9" />
      <path d="M8 8h8" />
      <path d="M8 12h8" />
      <path d="M10 8c2.5 0 4 1.2 4 3s-1.5 3-4 3" />
      <path d="m9 18 6-6" />
    </Ico>
  );
}

function PieIcon(p: IconProps) {
  return (
    <Ico {...p}>
      <path d="M21.21 15.89A10 10 0 1 1 8 2.83" />
      <path d="M22 12A10 10 0 0 0 12 2v10z" />
    </Ico>
  );
}

function ArrowRightIcon(p: IconProps) {
  return (
    <Ico {...p}>
      <path d="M5 12h14" />
      <path d="m12 5 7 7-7 7" />
    </Ico>
  );
}

function RefreshCwIcon(p: IconProps) {
  return (
    <Ico {...p}>
      <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" />
      <path d="M21 3v5h-5" />
      <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" />
      <path d="M8 16H3v5" />
    </Ico>
  );
}

function SparklesIcon(p: IconProps) {
  return (
    <Ico {...p}>
      <path d="M12 3v3" />
      <path d="M12 18v3" />
      <path d="M3 12h3" />
      <path d="M18 12h3" />
      <path d="m5.6 5.6 2.1 2.1" />
      <path d="m16.3 16.3 2.1 2.1" />
      <path d="m16.3 5.6-2.1 2.1" />
      <path d="m5.6 16.3 2.1-2.1" />
      <circle cx="12" cy="12" r="2.5" />
    </Ico>
  );
}

function AlertIcon(p: IconProps) {
  return (
    <Ico {...p}>
      <circle cx="12" cy="12" r="10" />
      <path d="M12 8v4" />
      <path d="M12 16h.01" />
    </Ico>
  );
}

function ClockIcon(p: IconProps) {
  return (
    <Ico {...p}>
      <circle cx="12" cy="12" r="10" />
      <path d="M12 6v6l4 2" />
    </Ico>
  );
}

function CalendarIcon(p: IconProps) {
  return (
    <Ico {...p}>
      <rect width="18" height="18" x="3" y="4" rx="2" />
      <path d="M16 2v4" />
      <path d="M8 2v4" />
      <path d="M3 10h18" />
    </Ico>
  );
}

function UsersIcon(p: IconProps) {
  return (
    <Ico {...p}>
      <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M22 21v-2a4 4 0 0 0-3-3.87" />
      <path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </Ico>
  );
}

function BookIcon(p: IconProps) {
  return (
    <Ico {...p}>
      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
      <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
    </Ico>
  );
}

function inr(n: number | undefined, digits = 4): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `₹${n.toLocaleString("en-IN", { maximumFractionDigits: digits })}`;
}

function fmtNum(n: number | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-IN");
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function fmtDuration(ms: number | undefined): string {
  if (ms == null || Number.isNaN(ms) || ms <= 0) return "—";
  const s = ms / 1000;
  return `${s.toLocaleString("en-IN", { maximumFractionDigits: 1 })} s`;
}

function purposeMeta(purpose: string): { label: string; icon: ReactNode; source: FeatureFilter } {
  const p = (purpose || "").toLowerCase();
  if (p === "whatsapp_outline") {
    return { label: "AI Agent - Outline", icon: <SparklesIcon size={14} />, source: "whatsapp" };
  }
  if (p === "auto_reply") {
    return { label: "Auto Reply", icon: <MessageIcon size={14} />, source: "whatsapp" };
  }
  if (p === "lead_summary") {
    return { label: "Lead Summary", icon: <UsersIcon size={14} />, source: "whatsapp" };
  }
  if (p === "knowledge_search") {
    return { label: "Knowledge Search", icon: <BookIcon size={14} />, source: "whatsapp" };
  }
  if (p === "knowledge_learning") {
    return { label: "KB Learning", icon: <BookIcon size={14} />, source: "whatsapp" };
  }
  if (p === "command" || p === "agent" || p.startsWith("agent")) {
    return { label: "AI Agent", icon: <SparklesIcon size={14} />, source: "agent" };
  }
  if (p.startsWith("whatsapp_")) {
    return { label: purpose.replace(/_/g, " "), icon: <MessageIcon size={14} />, source: "whatsapp" };
  }
  return { label: purpose || "—", icon: <SparklesIcon size={14} />, source: "all" };
}

function rowSource(r: UsageRow): FeatureFilter {
  if (r.source === "whatsapp" || r.source === "agent") return r.source;
  return purposeMeta(r.purpose).source === "all" ? "whatsapp" : purposeMeta(r.purpose).source;
}

export default function GeminiSettings() {
  const [data, setData] = useState<UsagePayload | null>(null);
  const [aiSettings, setAiSettings] = useState<AiSettingsPayload | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [period, setPeriod] = useState<Period>("month");
  const [feature, setFeature] = useState<FeatureFilter>("all");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [range, setRange] = useState<RangeFilter>("7d");

  const load = useCallback(async () => {
    setErr("");
    const [res, settings] = await Promise.all([
      api<UsagePayload>("/api/ai/usage?recent_limit=200"),
      api<AiSettingsPayload>("/api/ai/settings"),
    ]);
    setData(res);
    setAiSettings(settings);
  }, []);

  useEffect(() => {
    setLoading(true);
    load()
      .catch((e) => setErr(e instanceof Error ? e.message : "Failed to load Gemini usage"))
      .finally(() => setLoading(false));
  }, [load]);

  const overview = useMemo(() => {
    if (!data) return null;
    const isMonth = period === "month";
    const requests = isMonth ? data.requests_month : data.requests_today;
    const spend = isMonth ? data.spent_month_inr : data.spent_today_inr;
    const prompt = isMonth ? data.input_tokens_month : data.input_tokens_today || 0;
    const completion = isMonth ? data.output_tokens_month : data.output_tokens_today || 0;
    const thoughts = isMonth ? data.thoughts_tokens_month || 0 : data.thoughts_tokens_today || 0;
    const total =
      (isMonth ? data.total_tokens_month : data.total_tokens_today) &&
      (isMonth ? data.total_tokens_month! : data.total_tokens_today!) > 0
        ? isMonth
          ? data.total_tokens_month!
          : data.total_tokens_today!
        : prompt + completion + thoughts;
    const failed = isMonth ? data.failed_month ?? 0 : data.failed_today ?? 0;
    const successRate =
      (isMonth ? data.success_rate_month : data.success_rate_today) ??
      (requests > 0 ? Math.round(((requests - failed) / requests) * 1000) / 10 : 100);
    const bySource = isMonth ? data.by_source_month : data.by_source_today;
    return {
      requests,
      spend,
      prompt,
      completion,
      thoughts,
      total,
      failed,
      successRate,
      bySource,
      budget: data.monthly_budget_inr,
    };
  }, [data, period]);

  const recent = useMemo(() => {
    const rows = data?.recent || [];
    const now = Date.now();
    const cutoff =
      range === "7d"
        ? now - 7 * 24 * 60 * 60 * 1000
        : range === "30d"
          ? now - 30 * 24 * 60 * 60 * 1000
          : 0;
    return rows.filter((r) => {
      if (feature !== "all" && rowSource(r) !== feature) return false;
      if (status === "ok" && !r.success) return false;
      if (status === "fail" && r.success) return false;
      if (cutoff > 0) {
        if (!r.created_at) return false;
        const t = new Date(r.created_at).getTime();
        if (Number.isNaN(t) || t < cutoff) return false;
      }
      return true;
    });
  }, [data?.recent, feature, status, range]);

  const refresh = () => {
    setLoading(true);
    load()
      .catch((e) => setErr(e instanceof Error ? e.message : "Refresh failed"))
      .finally(() => setLoading(false));
  };

  const dashUsage = aiSettings?.usage || data;
  const configured = aiSettings ? Boolean(aiSettings.configured) : null;

  return (
    <div className="gemini-settings">
      {err ? <p className="err" style={{ marginBottom: 12 }}>{err}</p> : null}
      {loading && !data ? <p style={{ color: "var(--muted)", marginBottom: 12 }}>Loading…</p> : null}

      <section className="panel gemini-agent-dash">
        <div className="gemini-agent-dash-head">
          <h2>AI Agent</h2>
          <p>Usage, model, and budget for Gemini commands.</p>
        </div>
        <div className="ai-kpis">
          <div className="ai-kpi">
            <div className="ai-kpi-label">Status</div>
            <div className="ai-kpi-value">{aiSettings?.status_label || "—"}</div>
            <div className="ai-kpi-sub">
              {configured == null
                ? "Checking key…"
                : configured
                  ? "Gemini key present"
                  : "GEMINI_API_KEY missing"}
            </div>
          </div>
          <div className="ai-kpi">
            <div className="ai-kpi-label">Model</div>
            <div className="ai-kpi-value ai-kpi-model">{aiSettings?.model || "—"}</div>
            <div className="ai-kpi-sub">{aiSettings?.provider || "gemini"}</div>
          </div>
          <div className="ai-kpi">
            <div className="ai-kpi-label">Spent this month</div>
            <div className="ai-kpi-value">{inr(dashUsage?.spent_month_inr || 0)}</div>
            <div className="ai-kpi-sub">Budget {inr(dashUsage?.monthly_budget_inr || 0, 0)}</div>
          </div>
          <div className="ai-kpi">
            <div className="ai-kpi-label">Remaining</div>
            <div className="ai-kpi-value">{inr(dashUsage?.remaining_monthly_inr || 0)}</div>
            <div className="ai-kpi-sub">
              Today {inr(dashUsage?.spent_today_inr || 0)} / {inr(dashUsage?.daily_budget_inr || 0, 0)}
            </div>
          </div>
          <div className="ai-kpi">
            <div className="ai-kpi-label">Requests</div>
            <div className="ai-kpi-value">{dashUsage?.requests_month ?? "—"}</div>
            <div className="ai-kpi-sub">{dashUsage?.requests_today ?? 0} today</div>
          </div>
          <div className="ai-kpi">
            <div className="ai-kpi-label">Google Places</div>
            <div className="ai-kpi-value">
              {aiSettings?.google_places?.status_label || "—"}
            </div>
            <div className="ai-kpi-sub">
              {aiSettings?.google_places?.configured
                ? "Dealer discovery tools ready"
                : "Set GOOGLE_MAPS_API_KEY on server"}
            </div>
          </div>
          <div className="ai-kpi">
            <div className="ai-kpi-label">Automations</div>
            <div className="ai-kpi-value">0</div>
            <div className="ai-kpi-sub">Phase 2 worker</div>
          </div>
        </div>
      </section>

      {overview ? (
        <section className="panel gemini-overview">
          <div className="gemini-overview-head">
            <div className="gemini-overview-title">
              <span className="gemini-ico"><BarChartIcon size={16} /></span>
              <strong>Usage Overview ({period === "month" ? "This Month" : "Today"})</strong>
            </div>
            <select
              className="gemini-select"
              value={period}
              onChange={(e) => setPeriod(e.target.value as Period)}
              aria-label="Usage period"
            >
              <option value="month">This Month</option>
              <option value="today">Today</option>
            </select>
          </div>

          <div className="gemini-source-split">
            <div className="gemini-source-chip whatsapp">
              <span className="gemini-ico sm"><MessageIcon size={14} /></span>
              <div>
                <b>WhatsApp AI</b>
                <small>
                  {fmtNum(overview.bySource?.whatsapp?.requests || 0)} requests ·{" "}
                  {inr(Number(overview.bySource?.whatsapp?.spend_inr || 0))}
                </small>
              </div>
            </div>
            <div className="gemini-source-chip agent">
              <span className="gemini-ico sm"><SparklesIcon size={14} /></span>
              <div>
                <b>AI Agent</b>
                <small>
                  {fmtNum(overview.bySource?.agent?.requests || 0)} requests ·{" "}
                  {inr(Number(overview.bySource?.agent?.spend_inr || 0))}
                </small>
              </div>
            </div>
            {period === "month" ? (
              <div className="gemini-source-chip budget">
                <span className="gemini-ico sm"><RupeeIcon size={14} /></span>
                <div>
                  <b>Budget</b>
                  <small>
                    {inr(overview.spend)} / {inr(overview.budget, 0)}
                  </small>
                </div>
              </div>
            ) : null}
          </div>

          <div className="gemini-stat-grid">
            <div className="gemini-stat">
              <span className="gemini-ico"><MessageIcon size={15} /></span>
              <div>
                <span>Requests</span>
                <b>{fmtNum(overview.requests)}</b>
              </div>
            </div>
            <div className="gemini-stat">
              <span className="gemini-ico"><CoinsIcon size={15} /></span>
              <div>
                <span>Total Tokens</span>
                <b>{fmtNum(overview.total)}</b>
              </div>
            </div>
            <div className="gemini-stat">
              <span className="gemini-ico"><RupeeIcon size={15} /></span>
              <div>
                <span>Total Spend</span>
                <b>{inr(overview.spend)}</b>
              </div>
            </div>
            <div className="gemini-stat">
              <span className="gemini-ico"><PieIcon size={15} /></span>
              <div>
                <span>Success Rate</span>
                <b className="ok">{overview.successRate.toFixed(1)}%</b>
              </div>
            </div>
            <div className="gemini-stat">
              <span className="gemini-ico"><ArrowRightIcon size={15} /></span>
              <div>
                <span>Prompt Tokens</span>
                <b>{fmtNum(overview.prompt)}</b>
              </div>
            </div>
            <div className="gemini-stat">
              <span className="gemini-ico"><RefreshCwIcon size={15} /></span>
              <div>
                <span>Completion Tokens</span>
                <b>{fmtNum(overview.completion)}</b>
              </div>
            </div>
            <div className="gemini-stat">
              <span className="gemini-ico"><SparklesIcon size={15} /></span>
              <div>
                <span>Thoughts Tokens</span>
                <b>{fmtNum(overview.thoughts)}</b>
              </div>
            </div>
            <div className="gemini-stat">
              <span className="gemini-ico bad"><AlertIcon size={15} /></span>
              <div>
                <span>Failed Requests</span>
                <b className={overview.failed > 0 ? "bad" : undefined}>{fmtNum(overview.failed)}</b>
              </div>
            </div>
          </div>
        </section>
      ) : null}

      {data ? (
        <section className="panel gemini-calls">
          <div className="gemini-calls-head">
            <div className="gemini-overview-title">
              <span className="gemini-ico"><ClockIcon size={16} /></span>
              <strong>Recent API Calls</strong>
            </div>
            <div className="gemini-calls-filters">
              <select
                className="gemini-select"
                value={feature}
                onChange={(e) => setFeature(e.target.value as FeatureFilter)}
                aria-label="Filter by feature"
              >
                <option value="all">All Features</option>
                <option value="whatsapp">WhatsApp AI</option>
                <option value="agent">AI Agent</option>
              </select>
              <select
                className="gemini-select"
                value={status}
                onChange={(e) => setStatus(e.target.value as StatusFilter)}
                aria-label="Filter by status"
              >
                <option value="all">All Status</option>
                <option value="ok">OK</option>
                <option value="fail">Failed</option>
              </select>
              <label className="gemini-range">
                <CalendarIcon size={14} />
                <select
                  className="gemini-select"
                  value={range}
                  onChange={(e) => setRange(e.target.value as RangeFilter)}
                  aria-label="Date range"
                >
                  <option value="7d">Last 7 Days</option>
                  <option value="30d">Last 30 Days</option>
                  <option value="all">All loaded</option>
                </select>
              </label>
              <button
                type="button"
                className="gemini-icon-btn"
                disabled={loading}
                onClick={refresh}
                title="Refresh"
                aria-label="Refresh"
              >
                <RefreshCwIcon size={15} />
              </button>
            </div>
          </div>

          <div className="adms-table-wrap">
            <table className="adms-att-table gemini-calls-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Feature</th>
                  <th>User</th>
                  <th className="num">Prompt Tokens</th>
                  <th className="num">Completion Tokens</th>
                  <th className="num">Total Tokens</th>
                  <th className="num">Cost (₹)</th>
                  <th className="num">Duration</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {recent.length === 0 ? (
                  <tr>
                    <td colSpan={9} style={{ color: "var(--muted)", padding: 16 }}>
                      No Gemini usage matches these filters.
                    </td>
                  </tr>
                ) : (
                  recent.map((r) => {
                    const meta = purposeMeta(r.purpose);
                    const who = (r.display_name || r.employee_name || r.user_name || "—").trim() || "—";
                    const total =
                      r.total_tokens > 0
                        ? r.total_tokens
                        : r.input_tokens + r.output_tokens + r.thoughts_tokens;
                    return (
                      <tr key={r.id}>
                        <td className="mono">{fmtTime(r.created_at)}</td>
                        <td>
                          <span className="gemini-feature">
                            <span className="gemini-feature-ico">{meta.icon}</span>
                            {meta.label}
                          </span>
                        </td>
                        <td>{who}</td>
                        <td className="mono num">{fmtNum(r.input_tokens)}</td>
                        <td className="mono num">{fmtNum(r.output_tokens)}</td>
                        <td className="mono num">{fmtNum(total)}</td>
                        <td className="mono num">{inr(r.estimated_cost_inr)}</td>
                        <td className="mono num">{fmtDuration(r.duration_ms)}</td>
                        <td>
                          <span className={`evo-pill ${r.success ? "ok" : "bad"}`}>
                            {r.success ? "OK" : "Fail"}
                          </span>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}
    </div>
  );
}
