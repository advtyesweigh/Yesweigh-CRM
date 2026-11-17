import {
  ActivityIcon,
  AlertCircleIcon,
  CalendarIcon,
  FlagIcon,
  HashIcon,
  InfoIcon,
  MessageSquareIcon,
  PackageIcon,
  RefreshCwIcon,
  SparklesIcon,
  TargetIcon,
  ZapIcon,
} from "./icons";

export type AiSummary = {
  topic?: string;
  customer_intent?: string;
  current_issue?: string;
  conversation_status?: string;
  product_interest?: string[];
  quantity_requirement?: string | null;
  important_information?: string[];
  recommended_action?: string;
  followup_priority?: string;
  suggested_followup_at?: string | null;
  language?: string;
  updated_at?: string;
};

function display(value?: string | null) {
  const v = (value || "").trim();
  return v || "Not identified";
}

function statusClass(status?: string) {
  const s = (status || "").toLowerCase();
  if (s.includes("resolved") || s.includes("confirmed")) return "ok";
  if (s.includes("payment") || s.includes("renewal") || s.includes("service") || s.includes("required")) return "warn";
  if (s.includes("not interested")) return "bad";
  return "";
}

function priorityClass(p?: string) {
  const v = (p || "").toLowerCase();
  if (v === "high") return "high";
  if (v === "medium") return "med";
  if (v === "low") return "low";
  return "";
}

function fmtUpdated(v?: string | null) {
  if (!v) return "";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return v;
  return d.toLocaleString("en-IN", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

type Props = {
  summary: AiSummary | null;
  loading: boolean;
  error?: string;
  stale?: boolean;
  generatedAt?: string | null;
  onRegenerate: () => void;
};

export default function AiSummaryCard({ summary, loading, error, stale, generatedAt, onRegenerate }: Props) {
  const products = summary?.product_interest?.filter(Boolean) || [];
  const info = summary?.important_information?.filter(Boolean) || [];
  const hasStructured = Boolean(
    summary &&
      (summary.topic ||
        summary.customer_intent ||
        summary.current_issue ||
        summary.recommended_action ||
        products.length ||
        info.length),
  );

  return (
    <section className="wa-ai-card">
      <header className="wa-ai-head">
        <div className="wa-ai-title">
          <span className="wa-ai-spark">
            <SparklesIcon size={15} />
          </span>
          <div>
            <h3>AI Assistant Summary</h3>
            <p className="wa-ai-meta">
              {loading ? "Analysing…" : stale ? "Out of date — regenerate for latest" : hasStructured ? "Ready" : "No summary yet"}
              {(generatedAt || summary?.updated_at) && !loading ? (
                <> · Updated {fmtUpdated(generatedAt || summary?.updated_at)}</>
              ) : null}
            </p>
          </div>
        </div>
        <button type="button" className="wa-ai-regen" disabled={loading} onClick={onRegenerate} title="Regenerate">
          <RefreshCwIcon size={14} />
          {loading ? "…" : "Regenerate"}
        </button>
      </header>

      {error ? <p className="wa-hint danger">{error}</p> : null}

      {loading && !hasStructured ? (
        <p className="wa-hint">Analysing recent text and voice transcripts…</p>
      ) : null}

      {!loading && !hasStructured && !error ? (
        <p className="wa-hint">AI summary of this chat (text + voice STT). Click Regenerate to generate.</p>
      ) : null}

      {hasStructured ? (
        <div className="wa-ai-rows">
          <div className="wa-ai-row">
            <div className="wa-ai-label">
              <span className="wa-ai-ico"><MessageSquareIcon /></span>
              Topic
            </div>
            <div className="wa-ai-value">{display(summary?.topic)}</div>
          </div>
          <div className="wa-ai-row">
            <div className="wa-ai-label">
              <span className="wa-ai-ico"><TargetIcon /></span>
              Customer Intent
            </div>
            <div className="wa-ai-value">{display(summary?.customer_intent)}</div>
          </div>
          <div className="wa-ai-row">
            <div className="wa-ai-label">
              <span className="wa-ai-ico"><AlertCircleIcon /></span>
              Current Issue
            </div>
            <div className="wa-ai-value">{display(summary?.current_issue)}</div>
          </div>
          <div className="wa-ai-row">
            <div className="wa-ai-label">
              <span className="wa-ai-ico"><ActivityIcon /></span>
              Conversation Status
            </div>
            <div className="wa-ai-value">
              {summary?.conversation_status ? (
                <span className={`wa-status-pill ${statusClass(summary.conversation_status)}`}>
                  {summary.conversation_status}
                </span>
              ) : (
                "Not identified"
              )}
            </div>
          </div>
          <div className="wa-ai-row">
            <div className="wa-ai-label">
              <span className="wa-ai-ico"><PackageIcon /></span>
              Product Interest
            </div>
            <div className="wa-ai-value">
              {products.length ? products.join(", ") : "Not identified"}
            </div>
          </div>
          <div className="wa-ai-row">
            <div className="wa-ai-label">
              <span className="wa-ai-ico"><HashIcon /></span>
              Quantity / Requirement
            </div>
            <div className="wa-ai-value">{display(summary?.quantity_requirement)}</div>
          </div>
          <div className="wa-ai-row">
            <div className="wa-ai-label">
              <span className="wa-ai-ico"><InfoIcon /></span>
              Important Information
            </div>
            <div className="wa-ai-value">
              {info.length ? (
                <ul className="wa-ai-bullets">
                  {info.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              ) : (
                "Not identified"
              )}
            </div>
          </div>
          <div className="wa-ai-row wa-ai-action-row">
            <div className="wa-ai-label">
              <span className="wa-ai-ico"><ZapIcon /></span>
              Recommended Next Action
            </div>
            <div className="wa-ai-value">
              <div className="wa-ai-action">
                {display(summary?.recommended_action)}
              </div>
            </div>
          </div>
          <div className="wa-ai-row">
            <div className="wa-ai-label">
              <span className="wa-ai-ico"><FlagIcon /></span>
              Follow-up Priority
            </div>
            <div className="wa-ai-value">
              {summary?.followup_priority ? (
                <span className={`wa-prio ${priorityClass(summary.followup_priority)}`}>
                  {summary.followup_priority}
                </span>
              ) : (
                "Not identified"
              )}
            </div>
          </div>
          <div className="wa-ai-row">
            <div className="wa-ai-label">
              <span className="wa-ai-ico"><CalendarIcon /></span>
              Follow-up Date
            </div>
            <div className="wa-ai-value">{display(summary?.suggested_followup_at)}</div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
