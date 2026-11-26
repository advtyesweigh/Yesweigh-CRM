import {
  FormEvent,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { api } from "../api/client";
import AiMarkdown from "../components/AiMarkdown";
import BusinessReportCard, {
  BusinessReportData,
  businessReportFromDealerFinderApi,
  dealerFinderCardNeedsRefresh,
  extractBusinessReport,
  extractDealerFinderJobId,
  hasDealerFinderResultsCard,
  mergeDealerFinderCardExclusions,
} from "../components/BusinessReportCard";
import {
  clearActiveSession,
  clearAiPending,
  readActiveSession,
  readAiPending,
  sleep,
  writeActiveSession,
  writeAiPending,
} from "../lib/aiPending";

type AiSettings = {
  enabled: boolean;
  configured: boolean;
  status: string;
  status_label: string;
  provider: string;
  model: string;
  monthly_budget_inr: number;
  daily_budget_inr: number;
  max_output_tokens: number;
};

type AiWorkReport = {
  id: string;
  run_id?: string | null;
  tool_name?: string;
  task_name: string;
  agent_name: string;
  task_details: string;
  status: string;
  occurred_at?: string | null;
  created_at?: string | null;
};

type AiEvent = {
  at: string;
  kind: string;
  message: string;
  tool?: string;
  status?: string;
};

type AiRun = {
  id: string;
  session_id?: string;
  command: string;
  status: string;
  model?: string;
  result_summary?: string;
  error?: string | null;
  events?: AiEvent[];
  created_at?: string | null;
  finished_at?: string | null;
  tool_results?: unknown[];
  usage?: { input_tokens: number; output_tokens: number; estimated_cost_inr: number; duration_ms: number };
  factory?: { ok?: boolean; tool_name?: string; error?: string; duration_ms?: number };
  thinking_label?: string;
  /** True when the model could not answer (tool gap) — UI shows Failed + Retry. */
  tool_gap?: boolean;
  /** Soft-hidden via per-message ⋮ Delete. */
  user_deleted?: boolean;
  assistant_deleted?: boolean;
};

/** One left-list row = one AiSession (not one run). */
type AiConversation = {
  id: string;
  session_id: string;
  title: string;
  command: string;
  status: string;
  run_count?: number;
  created_at?: string | null;
  updated_at?: string | null;
  last_activity_at?: string | null;
  latest_run_id?: string | null;
  owner_user_id?: number;
  owner_name?: string;
};

type MeOut = { id: number; name: string; role: string };

type AiSessionDetail = {
  id: string;
  session_id: string;
  title: string;
  status: string;
  created_at?: string | null;
  updated_at?: string | null;
  runs: AiRun[];
};

type MainTab = "conversations" | "reports";

const PAGE_SIZE = 8;
const LIST_WIDTH_KEY = "yw-ai-list-width";
const LIST_WIDTH_DEFAULT = 260;
const LIST_WIDTH_MIN = 220;
const LIST_WIDTH_MAX = 480;

function readListWidth(): number {
  try {
    const raw = localStorage.getItem(LIST_WIDTH_KEY);
    if (!raw) return LIST_WIDTH_DEFAULT;
    const n = Number(raw);
    if (!Number.isFinite(n)) return LIST_WIDTH_DEFAULT;
    return Math.min(LIST_WIDTH_MAX, Math.max(LIST_WIDTH_MIN, Math.round(n)));
  } catch {
    return LIST_WIDTH_DEFAULT;
  }
}

function statusTone(status: string): string {
  if (status === "ready") return "ok";
  if (status === "not_configured") return "warn";
  return "muted";
}

function formatWhen(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  });
}

function formatWorkStatus(status: string): string {
  const s = (status || "").toLowerCase();
  if (s === "completed") return "Completed";
  if (s === "failed") return "Failed";
  if (s === "stopped" || s === "cancelled") return "Stopped";
  if (s === "running" || s === "queued") return "Running";
  return status || "—";
}

function workStatusTone(status: string): string {
  const s = (status || "").toLowerCase();
  if (s === "completed") return "ok";
  if (s === "failed") return "bad";
  if (s === "stopped" || s === "cancelled") return "muted";
  if (s === "running" || s === "queued") return "run";
  return "muted";
}

/** Match backend looks_like_failed_reply — hide long tool-gap / max-rounds text. */
const FAILED_REPLY_RES = [
  /don'?t have (a |any )?(tool|access)/i,
  /do not have (a |any )?(tool|access)/i,
  /no tool (available|for|to|that)/i,
  /I(?:'m| am) (not |un)?able to (access|query|retrieve|look up|get)/i,
  /cannot (access|query|retrieve|look up|find data)/i,
  /outside (of )?my (current )?capabilities/i,
  /would need (a |an )?(new )?tool/i,
  /not (currently )?equipped/i,
  /tools? (I have|available) (do not|don't|cannot)/i,
  /stopped after maximum tool rounds/i,
  /max(?:imum)? tool rounds/i,
  /see tool results below/i,
  /tried to build a new tool/i,
  /cursor poll failed/i,
  /tool factory failed/i,
  /502\s*bad gateway/i,
];

function looksLikeFailedReply(text: string): boolean {
  const blob = (text || "").trim();
  if (!blob) return false;
  if (/^Failed\.?$/i.test(blob)) return true;
  // Never show raw gateway HTML in the chat bubble.
  if (/<\s*html[\s>]|<\s*head[\s>]|bad gateway/i.test(blob)) return true;
  return FAILED_REPLY_RES.some((rx) => rx.test(blob));
}

function isRetryableFailedRun(run: AiRun): boolean {
  if (run.status === "cancelled") return false;
  if (run.tool_gap) return true;
  if (run.status === "failed") return true;
  if (run.factory && run.factory.ok === false) return true;
  if (run.events?.some((e) => e.kind === "tool_gap" || (e.kind === "factory" && e.status === "error"))) {
    return true;
  }
  return looksLikeFailedReply(run.error || run.result_summary || "");
}

function isStoppedRun(run: AiRun): boolean {
  return run.status === "cancelled" || /^Stopped\.?$/i.test((run.result_summary || "").trim());
}

/** Client-side ceiling so "Preparing answer…" cannot spin forever if the server stalls.
 *  Must exceed AI_RUN_TIMEOUT_S (900s) so sequential WhatsApp batches can finish. */
const POLL_MAX_MS = 16 * 60 * 1000;
const DEALER_FINDER_LIVE_MAX_MS = 45 * 60 * 1000;

/** Live-attach Dealer Finder progress/results into an existing chat turn. */
function DealerFinderLiveCard({
  toolResults,
  resultSummary,
  fallbackReport,
}: {
  toolResults?: unknown[];
  resultSummary?: string | null;
  /** Stored tool_results card — shown until live filtered refresh arrives. */
  fallbackReport?: BusinessReportData | null;
}) {
  const jobId = useMemo(
    () => extractDealerFinderJobId(toolResults, resultSummary),
    [toolResults, resultSummary],
  );
  const alreadyHasCard = useMemo(
    () => hasDealerFinderResultsCard(toolResults),
    [toolResults],
  );
  const needsRefresh = useMemo(
    () => dealerFinderCardNeedsRefresh(toolResults),
    [toolResults],
  );
  const [liveReport, setLiveReport] = useState<BusinessReportData | null>(null);
  const [progressNote, setProgressNote] = useState("");

  useEffect(() => {
    // Always re-fetch when job id is known and stored card is stale/partial/unfiltered.
    if (!jobId) return;
    if (alreadyHasCard && !needsRefresh) return;
    let cancelled = false;
    const started = Date.now();
    const tick = async () => {
      while (!cancelled) {
        try {
          const jobResp = await api<{
            ok?: boolean;
            job?: {
              public_id?: string;
              status?: string;
              state?: string;
              completed_units?: number;
              total_units?: number;
              results_ready?: boolean;
              error?: string;
            };
          }>(`/api/dealer-finder/jobs/${jobId}`);
          const job = jobResp.job;
          if (!job) break;
          const jobStatus = String(job.status || "");
          const running = ["Pending", "Running"].includes(jobStatus);
          const ready =
            Boolean(job.results_ready) || !running;
          const partialQs = running ? "&include_partial=true" : "";
          if (running) {
            try {
              const partial = await api<Record<string, unknown>>(
                `/api/dealer-finder/jobs/${jobId}/results?filter=eligible_new_with_phone&limit=10000${partialQs}`,
              );
              if (!cancelled) {
                const partialCard = businessReportFromDealerFinderApi(partial);
                if (partialCard) {
                  setLiveReport(mergeDealerFinderCardExclusions(partialCard, toolResults));
                  setProgressNote("");
                }
              }
            } catch {
              /* keep polling */
            }
          }
          if (!ready) {
            if (!cancelled && !fallbackReport && !liveReport) {
              setProgressNote(
                `Dealer Finder for ${job.state || "…"}: ${job.completed_units ?? 0}/${job.total_units ?? 0} units (${jobStatus}). Unique phone NEW list updates as the scan runs.`,
              );
            }
            if (Date.now() - started > DEALER_FINDER_LIVE_MAX_MS) {
              if (!cancelled && !fallbackReport && !liveReport) {
                setProgressNote(
                  `Still scanning ${job.state || ""} (${job.completed_units ?? 0}/${job.total_units ?? 0}). Ask “show dealer finder results” to refresh.`,
                );
              }
              break;
            }
            await sleep(2500);
            continue;
          }
          const res = await api<Record<string, unknown>>(
            `/api/dealer-finder/jobs/${jobId}/results?filter=eligible_new_with_phone&limit=10000${partialQs}`,
          );
          if (cancelled) break;
          let card = businessReportFromDealerFinderApi(res);
          if (card) {
            card = mergeDealerFinderCardExclusions(card, toolResults);
            setLiveReport(card);
            setProgressNote("");
          } else if (!fallbackReport) {
            setProgressNote(
              job.error
                ? `Dealer Finder ${job.status}: ${job.error}`
                : `Dealer Finder ${job.status}: no places found.`,
            );
          }
          break;
        } catch {
          if (cancelled) break;
          await sleep(3000);
          if (Date.now() - started > DEALER_FINDER_LIVE_MAX_MS) break;
        }
      }
    };
    void tick();
    return () => {
      cancelled = true;
    };
  }, [jobId, alreadyHasCard, needsRefresh, fallbackReport]);

  if (!jobId && !fallbackReport) return null;
  if (liveReport) {
    return (
      <>
        <AiMarkdown className="ai-bubble-text ai-bubble-blurb">{liveReport.shortText}</AiMarkdown>
        <BusinessReportCard data={liveReport} />
      </>
    );
  }
  if (fallbackReport) {
    return (
      <>
        <AiMarkdown className="ai-bubble-text ai-bubble-blurb">{fallbackReport.shortText}</AiMarkdown>
        <BusinessReportCard data={fallbackReport} />
      </>
    );
  }
  if (progressNote) {
    return <div className="ai-bubble-text ai-thinking-text">{progressNote}</div>;
  }
  return null;
}

function runIconKind(command: string): "search" | "megaphone" | "chart" | "download" | "zap" | "bot" {
  const c = command.toLowerCase();
  if (/dealer|find|search|locate/.test(c)) return "search";
  if (/campaign|promo|market/.test(c)) return "megaphone";
  if (/sales|analys|report|performance|insight/.test(c)) return "chart";
  if (/import|download|export|lead/.test(c)) return "download";
  if (/automat|reply|follow/.test(c)) return "zap";
  return "bot";
}

function Icon({ kind, size = 18 }: { kind: string; size?: number }) {
  const props = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 2,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true as const,
  };
  switch (kind) {
    case "search":
      return (
        <svg {...props}>
          <circle cx="11" cy="11" r="7" />
          <line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
      );
    case "megaphone":
      return (
        <svg {...props}>
          <path d="M3 11v2a4 4 0 0 0 4 4h1" />
          <path d="M14 5l7 4v6l-7 4V5z" />
          <path d="M14 9H7a4 4 0 0 0 0 6h7" />
        </svg>
      );
    case "chart":
      return (
        <svg {...props}>
          <line x1="4" y1="20" x2="20" y2="20" />
          <rect x="6" y="10" width="3" height="7" rx="1" />
          <rect x="11" y="6" width="3" height="11" rx="1" />
          <rect x="16" y="12" width="3" height="5" rx="1" />
        </svg>
      );
    case "download":
      return (
        <svg {...props}>
          <path d="M12 3v12" />
          <path d="M7 10l5 5 5-5" />
          <path d="M5 21h14" />
        </svg>
      );
    case "zap":
      return (
        <svg {...props}>
          <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
        </svg>
      );
    case "chat":
      return (
        <svg {...props}>
          <path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z" />
        </svg>
      );
    case "report":
      return (
        <svg {...props}>
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
          <line x1="8" y1="13" x2="16" y2="13" />
          <line x1="8" y1="17" x2="14" y2="17" />
        </svg>
      );
    case "send":
      return (
        <svg {...props}>
          <line x1="22" y1="2" x2="11" y2="13" />
          <polygon points="22 2 15 22 11 13 2 9 22 2" />
        </svg>
      );
    case "stop":
      return (
        <svg {...props}>
          <rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none" />
        </svg>
      );
    case "paperclip":
      return (
        <svg {...props}>
          <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
        </svg>
      );
    case "mic":
      return (
        <svg {...props}>
          <rect x="9" y="2" width="6" height="12" rx="3" />
          <path d="M5 11a7 7 0 0 0 14 0" />
          <line x1="12" y1="18" x2="12" y2="22" />
          <line x1="8" y1="22" x2="16" y2="22" />
        </svg>
      );
    case "mic-off":
      return (
        <svg {...props}>
          <line x1="1" y1="1" x2="23" y2="23" />
          <path d="M9 9v3a3 3 0 0 0 5.12 2.12" />
          <path d="M15 9.34V5a3 3 0 0 0-5.68-1.33" />
          <path d="M17 16.95A7 7 0 0 1 5 11" />
          <path d="M19 11a6.9 6.9 0 0 1-.4 2.3" />
          <line x1="12" y1="18" x2="12" y2="22" />
          <line x1="8" y1="22" x2="16" y2="22" />
        </svg>
      );
    case "spark":
      return (
        <svg {...props}>
          <path d="M12 3l1.5 5.5L19 10l-5.5 1.5L12 17l-1.5-5.5L5 10l5.5-1.5L12 3z" />
          <path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15z" />
        </svg>
      );
    case "plus":
      return (
        <svg {...props}>
          <line x1="12" y1="5" x2="12" y2="19" />
          <line x1="5" y1="12" x2="19" y2="12" />
        </svg>
      );
    case "bot":
    default:
      return (
        <svg {...props}>
          <rect x="5" y="8" width="14" height="11" rx="3" />
          <circle cx="9.5" cy="13" r="1.2" fill="currentColor" stroke="none" />
          <circle cx="14.5" cy="13" r="1.2" fill="currentColor" stroke="none" />
          <path d="M9 17h6" />
          <path d="M12 8V5" />
          <circle cx="12" cy="4" r="1" fill="currentColor" stroke="none" />
        </svg>
      );
  }
}

function RobotMark({ size = 64 }: { size?: number }) {
  return (
    <div className="ai-robot-mark" style={{ width: size, height: size }} aria-hidden>
      <Icon kind="bot" size={Math.round(size * 0.55)} />
    </div>
  );
}

type ComposerAttach = {
  id: string;
  name: string;
  mime: string;
  size: number;
  dataUrl: string;
};

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("Failed to read file"));
    reader.readAsDataURL(file);
  });
}

type MicPhase = "idle" | "recording" | "transcribing";

export default function Work() {
  const [mainTab, setMainTab] = useState<MainTab>("conversations");
  const [settings, setSettings] = useState<AiSettings | null>(null);
  const [settingsReady, setSettingsReady] = useState(false);
  const [workReports, setWorkReports] = useState<AiWorkReport[]>([]);
  const [workReportsLoading, setWorkReportsLoading] = useState(false);
  const [workReportsError, setWorkReportsError] = useState("");
  const [meName, setMeName] = useState("You");
  const [meUserId, setMeUserId] = useState<number | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [listUserFilter, setListUserFilter] = useState<"all" | "me" | string>("all");
  const [command, setCommand] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conversations, setConversations] = useState<AiConversation[]>([]);
  const [sessionId, setSessionIdState] = useState<string | undefined>(() => readActiveSession() || undefined);
  const [thread, setThread] = useState<AiSessionDetail | null>(null);
  const [composerMode, setComposerMode] = useState<"new" | "thread">(() =>
    readActiveSession() ? "thread" : "new",
  );
  /** Optimistic user turn shown immediately on Send/Enter; cleared when server thread loads. */
  const [pendingCommand, setPendingCommand] = useState<string | null>(null);
  /** Shown while waiting on /api/ai/command (factory can take ~1–3 min). */
  const [thinkingLabel, setThinkingLabel] = useState("Preparing answer…");
  const [attachments, setAttachments] = useState<ComposerAttach[]>([]);
  const [micPhase, setMicPhase] = useState<MicPhase>("idle");
  const [speechHint, setSpeechHint] = useState("");

  const [listSearch, setListSearch] = useState("");
  const [listWidth, setListWidth] = useState(readListWidth);
  const [page, setPage] = useState(1);
  const [menuOpenId, setMenuOpenId] = useState<string | null>(null);
  /** Per-bubble menu: `${runId}:user` | `${runId}:assistant` */
  const [bubbleMenuId, setBubbleMenuId] = useState<string | null>(null);
  const [renameTarget, setRenameTarget] = useState<AiConversation | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [renameBusy, setRenameBusy] = useState(false);
  const threadEndRef = useRef<HTMLDivElement | null>(null);
  const resizingRef = useRef(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const commandInputRef = useRef<HTMLTextAreaElement | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  /** Sync ref so send always reuses the latest session even before React re-renders. */
  const sessionIdRef = useRef<string | undefined>(readActiveSession() || undefined);
  /** In-flight run id for Stop (also mirrored in sessionStorage via aiPending). */
  const activeRunIdRef = useRef<string | undefined>(undefined);
  const [, setActiveRunId] = useState<string | undefined>(undefined);
  /** Bumped on unmount / Stop so in-component pollers stop updating state. */
  const pollGenRef = useRef(0);
  const recording = micPhase === "recording";
  const transcribing = micPhase === "transcribing";
  const micBusy = recording || transcribing;

  function setSessionId(next: string | undefined) {
    sessionIdRef.current = next;
    setSessionIdState(next);
    if (next) writeActiveSession(next);
    else clearActiveSession();
  }

  function setActiveRun(next: string | undefined) {
    activeRunIdRef.current = next;
    setActiveRunId(next);
  }

  function resizeComposer() {
    const el = commandInputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(160, Math.max(24, el.scrollHeight))}px`;
  }

  function focusComposer(selectEnd = true) {
    requestAnimationFrame(() => {
      const el = commandInputRef.current;
      if (!el) return;
      el.focus();
      if (selectEnd) {
        const n = el.value.length;
        try {
          el.setSelectionRange(n, n);
        } catch {
          /* ignore */
        }
      }
      resizeComposer();
    });
  }

  useEffect(() => {
    document.title = "AI Agent · YesWeigh CRM";
    return () => {
      document.title = "YesWeigh CRM";
      pollGenRef.current += 1;
      try {
        mediaRecorderRef.current?.state !== "inactive" && mediaRecorderRef.current?.stop();
      } catch {
        /* ignore */
      }
      mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
      mediaStreamRef.current = null;
      mediaRecorderRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (composerMode !== "thread" || (!thread && !pendingCommand)) return;
    threadEndRef.current?.scrollIntoView({ block: "end" });
  }, [composerMode, thread, busy, error, pendingCommand, thinkingLabel]);

  // Keep one coherent user-facing label while waiting (including long factory runs).
  useEffect(() => {
    setThinkingLabel("Preparing answer…");
  }, [busy]);

  useEffect(() => {
    resizeComposer();
  }, [command]);

  const loadConversationsOnly = useCallback(async () => {
    let path = "/api/ai/sessions?limit=100";
    if (isAdmin && listUserFilter !== "all") {
      const uid = listUserFilter === "me" ? meUserId : Number(listUserFilter);
      if (uid && uid > 0) path += `&user_id=${uid}`;
    }
    const convs = await api<AiConversation[]>(path);
    setConversations(convs);
    return convs;
  }, [isAdmin, listUserFilter, meUserId]);

  const load = useCallback(async () => {
    const [s, me] = await Promise.all([
      api<AiSettings>("/api/ai/settings"),
      api<MeOut>("/api/me").catch(() => ({ id: 0, name: "You", role: "" })),
    ]);
    setSettings(s);
    setSettingsReady(true);
    const role = (me.role || "").toLowerCase();
    const admin =
      role === "admin" || role === "super_admin" || role === "super admin" || role === "management";
    setIsAdmin(admin);
    if (me?.name) setMeName(me.name);
    if (me?.id) setMeUserId(me.id);
    let path = "/api/ai/sessions?limit=100";
    if (admin && listUserFilter !== "all") {
      const uid = listUserFilter === "me" ? me.id : Number(listUserFilter);
      if (uid > 0) path += `&user_id=${uid}`;
    }
    const convs = await api<AiConversation[]>(path);
    setConversations(convs);
  }, [listUserFilter]);

  const loadWorkReports = useCallback(async () => {
    setWorkReportsLoading(true);
    setWorkReportsError("");
    try {
      const rows = await api<AiWorkReport[]>("/api/ai/work-reports?limit=100");
      setWorkReports(rows);
    } catch (e) {
      setWorkReportsError(e instanceof Error ? e.message : "Failed to load work reports");
    } finally {
      setWorkReportsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (mainTab !== "reports") return;
    void loadWorkReports();
    const t = window.setInterval(() => {
      void loadWorkReports();
    }, 15_000);
    return () => window.clearInterval(t);
  }, [mainTab, loadWorkReports]);

  const loadThread = useCallback(async (sid: string) => {
    const detail = await api<AiSessionDetail>(`/api/ai/sessions/${sid}`);
    setThread(detail);
    setSessionId(detail.session_id);
    setComposerMode("thread");
    return detail;
  }, []);

  const pollUntilRunDone = useCallback(
    async (sid: string, runId: string, gen: number): Promise<AiRun | null> => {
      const started = Date.now();
      for (;;) {
        if (gen !== pollGenRef.current) return null;
        const detail = await api<AiSessionDetail>(`/api/ai/sessions/${sid}`);
        if (gen !== pollGenRef.current) return null;
        setThread(detail);
        setSessionId(detail.session_id);
        setComposerMode("thread");
        const run = (detail.runs || []).find((r) => r.id === runId);
        if (run && run.status !== "running") {
          return run;
        }
        if (Date.now() - started > POLL_MAX_MS) {
          // Server may still be working, but stop infinite Preparing answer… — show Failed + Retry.
          return {
            id: runId,
            command: run?.command || "",
            status: "failed",
            result_summary: "Failed.",
            error: "Timed out waiting for answer",
            tool_gap: true,
            events: run?.events,
          };
        }
        await sleep(1500);
      }
    },
    [],
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      let lastErr: unknown;
      for (let attempt = 1; attempt <= 3; attempt++) {
        try {
          await load();
          if (!cancelled) setError("");
          return;
        } catch (e) {
          lastErr = e;
          // API recreate / brief downtime — retry before showing a failure.
          if (attempt < 3) await sleep(800 * attempt);
        }
      }
      if (!cancelled) {
        setError(lastErr instanceof Error ? lastErr.message : "Failed to load AI Agent");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [load]);

  // Restore last active conversation after refresh/nav (until New Chat clears it).
  useEffect(() => {
    if (readAiPending()) return;
    const active = readActiveSession();
    if (!active) return;
    setComposerMode("thread");
    setSessionId(active);
    void loadThread(active).catch(() => {
      // Stale/deleted session — drop pin so next ask can start cleanly.
      clearActiveSession();
      sessionIdRef.current = undefined;
      setSessionIdState(undefined);
      setComposerMode("new");
      setThread(null);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only restore
  }, []);

  // Resume "Preparing answer…" after leaving /work (sidebar nav) — server kept running.
  useEffect(() => {
    const pending = readAiPending();
    if (!pending) return;
    const gen = pollGenRef.current;
    setError("");
    setBusy(true);
    setThinkingLabel("Preparing answer…");
    setPendingCommand(pending.command);
    setActiveRun(pending.runId);
    setSessionId(pending.sessionId);
    setComposerMode("thread");
    setMainTab("conversations");
    void (async () => {
      try {
        const detail = await loadThread(pending.sessionId);
        if (gen !== pollGenRef.current) return;
        const existing = (detail?.runs || []).find((r) => r.id === pending.runId);
        // User turn is already in the thread from the server.
        setPendingCommand(null);
        if (existing && existing.status !== "running") {
          clearAiPending();
          setActiveRun(undefined);
          setBusy(false);
          return;
        }
        const finished = await pollUntilRunDone(pending.sessionId, pending.runId, gen);
        if (gen !== pollGenRef.current) return;
        clearAiPending();
        setActiveRun(undefined);
        await load();
        if (finished?.status === "failed" && finished.tool_gap) {
          setThread((prev) => {
            if (!prev || prev.session_id !== pending.sessionId) return prev;
            const runs = (prev.runs || []).map((r) =>
              r.id === pending.runId
                ? { ...r, status: "failed", result_summary: "Failed.", error: finished.error, tool_gap: true }
                : r,
            );
            return { ...prev, status: "failed", runs };
          });
        } else if (!finished) {
          await loadThread(pending.sessionId);
        } else {
          await loadThread(pending.sessionId);
        }
      } catch (e) {
        if (gen !== pollGenRef.current) return;
        setError(e instanceof Error ? e.message : "Failed to resume AI reply");
        setPendingCommand(null);
        setActiveRun(undefined);
      } finally {
        if (gen === pollGenRef.current) {
          setBusy(false);
          setThinkingLabel("Preparing answer…");
        }
      }
    })();
    // Only on mount — pending job is in sessionStorage.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const sessionOwnerOptions = useMemo(() => {
    const byId = new Map<number, string>();
    for (const c of conversations) {
      if (c.owner_user_id && c.owner_name) byId.set(c.owner_user_id, c.owner_name);
    }
    return [...byId.entries()]
      .map(([id, name]) => ({ id, name }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [conversations]);

  const filteredConversations = useMemo(() => {
    const q = listSearch.trim().toLowerCase();
    return conversations.filter((c) => {
      const hay = `${c.title || ""} ${c.command || ""} ${c.owner_name || ""}`.toLowerCase();
      if (q && !hay.includes(q)) return false;
      return true;
    });
  }, [conversations, listSearch]);

  useEffect(() => {
    if (!isAdmin) return;
    void loadConversationsOnly().catch(() => {
      /* initial load() already ran */
    });
  }, [listUserFilter, isAdmin, loadConversationsOnly]);

  const totalPages = Math.max(1, Math.ceil(filteredConversations.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageConversations = filteredConversations.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);
  const showingFrom = filteredConversations.length === 0 ? 0 : (safePage - 1) * PAGE_SIZE + 1;
  const showingTo = Math.min(safePage * PAGE_SIZE, filteredConversations.length);

  useEffect(() => {
    setPage(1);
  }, [listSearch]);

  useEffect(() => {
    try {
      localStorage.setItem(LIST_WIDTH_KEY, String(listWidth));
    } catch {
      /* ignore */
    }
  }, [listWidth]);

  const onResizePointerDown = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    if (window.matchMedia("(max-width: 800px)").matches) return;
    e.preventDefault();
    const handle = e.currentTarget;
    const startX = e.clientX;
    const startW = listWidth;
    resizingRef.current = true;
    handle.setPointerCapture(e.pointerId);
    document.body.classList.add("ai-resizing");

    const onMove = (ev: PointerEvent) => {
      if (!resizingRef.current) return;
      const next = Math.min(
        LIST_WIDTH_MAX,
        Math.max(LIST_WIDTH_MIN, Math.round(startW + (ev.clientX - startX))),
      );
      setListWidth(next);
    };
    const onUp = (ev: PointerEvent) => {
      resizingRef.current = false;
      document.body.classList.remove("ai-resizing");
      try {
        handle.releasePointerCapture(ev.pointerId);
      } catch {
        /* ignore */
      }
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onUp);
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  }, [listWidth]);

  useEffect(() => {
    if (!menuOpenId) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null;
      if (t?.closest?.(".ai-conv-menu-wrap")) return;
      setMenuOpenId(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpenId(null);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpenId]);

  useEffect(() => {
    if (!bubbleMenuId) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null;
      if (t?.closest?.(".ai-bubble-menu-wrap")) return;
      setBubbleMenuId(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setBubbleMenuId(null);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [bubbleMenuId]);

  async function deleteBubble(run: AiRun, side: "user" | "assistant") {
    setBubbleMenuId(null);
    const sid = run.session_id || sessionId;
    if (!sid) return;
    const label = side === "user" ? "this question" : "this answer";
    if (!window.confirm(`Delete ${label}? This cannot be undone.`)) return;
    setError("");
    try {
      const updated = await api<AiSessionDetail>(
        `/api/ai/sessions/${sid}/runs/${run.id}/messages/${side}`,
        { method: "DELETE" },
      );
      setThread(updated);
      if (!(updated.runs || []).length) {
        await load();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete message");
    }
  }

  function editUserMessage(text: string) {
    setBubbleMenuId(null);
    setCommand(text);
    focusComposer(true);
  }

  async function stopGeneration() {
    const pending = readAiPending();
    const runId = activeRunIdRef.current || pending?.runId;
    const restoreCmd = pendingCommand || pending?.command || "";
    // Invalidate in-flight pollers / submit finally handlers.
    pollGenRef.current += 1;
    clearAiPending();
    setBusy(false);
    setPendingCommand(null);
    setActiveRun(undefined);
    setThinkingLabel("Preparing answer…");
    if (restoreCmd) setCommand(restoreCmd);
    focusComposer(true);
    if (!runId) return;
    setError("");
    try {
      await api(`/api/ai/runs/${runId}/cancel`, { method: "POST" });
      const sid = sessionIdRef.current;
      if (sid) {
        await loadThread(sid);
        await load();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to stop");
    }
  }

  function startNewChat() {
    // Only New Chat clears the active session — continuous asks must reuse it.
    clearAiPending();
    clearActiveSession();
    sessionIdRef.current = undefined;
    setThread(null);
    setSessionIdState(undefined);
    setComposerMode("new");
    setCommand("");
    setPendingCommand(null);
    setError("");
    setBusy(false);
    setActiveRun(undefined);
    setMainTab("conversations");
  }

  function selectConversation(conv: AiConversation) {
    setMenuOpenId(null);
    setError("");
    setPendingCommand(null);
    setBusy(false);
    setActiveRun(undefined);
    setMainTab("conversations");
    setComposerMode("thread");
    setSessionId(conv.session_id);
    void loadThread(conv.session_id).catch((e) =>
      setError(e instanceof Error ? e.message : "Failed to load conversation"),
    );
  }

  function openRename(conv: AiConversation) {
    setMenuOpenId(null);
    setRenameTarget(conv);
    setRenameValue((conv.title || conv.command || "").trim());
    setError("");
  }

  async function deleteConversation(conv: AiConversation) {
    setMenuOpenId(null);
    const label = (conv.title || conv.command || "this conversation").trim();
    if (!window.confirm(`Delete “${label}”? This cannot be undone.`)) return;
    setError("");
    try {
      await api(`/api/ai/sessions/${conv.session_id}`, { method: "DELETE" });
      setConversations((prev) => prev.filter((c) => c.session_id !== conv.session_id));
      if (sessionId === conv.session_id) {
        startNewChat();
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete conversation");
    }
  }

  function closeRename() {
    if (renameBusy) return;
    setRenameTarget(null);
    setRenameValue("");
  }

  async function saveRename(e?: FormEvent) {
    e?.preventDefault();
    if (!renameTarget || renameBusy) return;
    const next = renameValue.trim();
    if (!next) {
      setError("Title is required");
      return;
    }
    setRenameBusy(true);
    setError("");
    try {
      const updated = await api<AiConversation>(`/api/ai/sessions/${renameTarget.session_id}`, {
        method: "PATCH",
        body: JSON.stringify({ title: next }),
      });
      setConversations((prev) =>
        prev.map((c) =>
          c.session_id === renameTarget.session_id
            ? { ...c, title: updated.title || next, command: updated.command || c.command }
            : c,
        ),
      );
      setThread((prev) =>
        prev && prev.session_id === renameTarget.session_id
          ? { ...prev, title: updated.title || next }
          : prev,
      );
      setRenameTarget(null);
      setRenameValue("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to rename conversation");
    } finally {
      setRenameBusy(false);
    }
  }

  async function addFiles(files: FileList | File[] | null) {
    if (!files || busy) return;
    const list = Array.from(files);
    if (!list.length) return;
    const next: ComposerAttach[] = [...attachments];
    for (const file of list) {
      if (next.length >= 3) {
        setError("Max 3 attachments per message");
        break;
      }
      if (file.size > 2.5 * 1024 * 1024) {
        setError(`File too large (max 2.5MB): ${file.name}`);
        continue;
      }
      try {
        const dataUrl = await readFileAsDataUrl(file);
        next.push({
          id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          name: file.name,
          mime: file.type || "application/octet-stream",
          size: file.size,
          dataUrl,
        });
      } catch {
        setError(`Could not read ${file.name}`);
      }
    }
    setAttachments(next);
  }

  function removeAttachment(id: string) {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }

  function stopMicTracks() {
    mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
    mediaStreamRef.current = null;
  }

  function cancelRecording() {
    const recorder = mediaRecorderRef.current;
    audioChunksRef.current = [];
    if (recorder && recorder.state !== "inactive") {
      recorder.onstop = () => {
        stopMicTracks();
        mediaRecorderRef.current = null;
        setMicPhase("idle");
        setSpeechHint("");
      };
      try {
        recorder.stop();
      } catch {
        stopMicTracks();
        mediaRecorderRef.current = null;
        setMicPhase("idle");
        setSpeechHint("");
      }
      return;
    }
    stopMicTracks();
    mediaRecorderRef.current = null;
    setMicPhase("idle");
    setSpeechHint("");
  }

  async function uploadAndTranscribe(blob: Blob) {
    setMicPhase("transcribing");
    setSpeechHint("Transcribing…");
    setError("");
    try {
      const form = new FormData();
      const ext = (blob.type || "").includes("ogg") ? "ogg" : "webm";
      form.append("file", blob, `ai-voice-${Date.now()}.${ext}`);
      const res = await api<{ transcript: string; language_code?: string; translated?: boolean }>(
        "/api/ai/transcribe",
        { method: "POST", body: form },
      );
      const text = (res.transcript || "").trim();
      if (!text) {
        setError("No speech detected — try again");
        return;
      }
      setCommand((prev) => {
        const base = prev.trim();
        return base ? `${base} ${text}` : text;
      });
      setSpeechHint("Transcript ready — review and press Send");
      window.setTimeout(() => setSpeechHint(""), 3500);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Transcription failed");
      setSpeechHint("");
    } finally {
      setMicPhase("idle");
    }
  }

  async function startRecording() {
    if (busy || micBusy) return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setError("Voice recording is not supported in this browser — try Chrome/Edge");
      return;
    }
    setError("");
    setSpeechHint("Recording… tap mic to stop");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      mediaStreamRef.current = stream;
      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : MediaRecorder.isTypeSupported("audio/webm")
          ? "audio/webm"
          : "";
      const recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
      audioChunksRef.current = [];
      recorder.ondataavailable = (ev) => {
        if (ev.data.size > 0) audioChunksRef.current.push(ev.data);
      };
      mediaRecorderRef.current = recorder;
      recorder.start(250);
      setMicPhase("recording");
    } catch (err) {
      stopMicTracks();
      setMicPhase("idle");
      setSpeechHint("");
      setError(err instanceof Error ? err.message : "Microphone permission denied");
    }
  }

  function stopRecordingAndTranscribe() {
    const recorder = mediaRecorderRef.current;
    if (!recorder) {
      setMicPhase("idle");
      setSpeechHint("");
      return;
    }
    recorder.onstop = () => {
      stopMicTracks();
      mediaRecorderRef.current = null;
      const blob = new Blob(audioChunksRef.current, { type: recorder.mimeType || "audio/webm" });
      audioChunksRef.current = [];
      if (blob.size < 200) {
        setMicPhase("idle");
        setSpeechHint("");
        setError("Recording too short — hold a bit longer");
        return;
      }
      void uploadAndTranscribe(blob);
    };
    if (recorder.state !== "inactive") {
      try {
        recorder.stop();
      } catch {
        stopMicTracks();
        mediaRecorderRef.current = null;
        setMicPhase("idle");
        setSpeechHint("");
      }
    } else {
      setMicPhase("idle");
      setSpeechHint("");
    }
  }

  function toggleMic() {
    if (busy || transcribing) return;
    if (recording) {
      stopRecordingAndTranscribe();
      return;
    }
    void startRecording();
  }

  async function submit(text: string, opts?: { retryFromRunId?: string }) {
    const cmd = text.trim();
    const pendingAttaches = attachments;
    const retryFromRunId = opts?.retryFromRunId;
    if ((!cmd && !pendingAttaches.length && !retryFromRunId) || busy || micBusy) return;
    if (recording) cancelRecording();

    // Reuse active session unless New Chat cleared it — never force-null from composerMode.
    const sid = sessionIdRef.current || sessionId || readActiveSession() || null;
    const startingFresh = !sid;
    const gen = pollGenRef.current;
    const displayCmd =
      cmd ||
      (pendingAttaches.length
        ? `Attached: ${pendingAttaches.map((a) => a.name).join(", ")}`
        : "Retry");
    setCommand("");
    setAttachments([]);
    setError("");
    setPendingCommand(displayCmd);
    setBusy(true);
    setThinkingLabel("Preparing answer…");
    setComposerMode("thread");
    if (!thread || startingFresh) {
      setThread({
        id: sid || "pending",
        session_id: sid || "pending",
        title: displayCmd.slice(0, 80),
        status: "running",
        runs: [],
      });
    }

    try {
      const payloadAttaches = pendingAttaches.map((a) => {
        const raw = a.dataUrl.includes(",") ? a.dataUrl.split(",")[1] || "" : a.dataUrl;
        return { name: a.name, mime: a.mime, data_base64: raw };
      });
      // Returns quickly with status=running; work continues on the server.
      const ack = await api<AiRun>("/api/ai/command", {
        method: "POST",
        body: JSON.stringify({
          command: cmd || "Please review the attached file(s).",
          session_id: sid,
          attachments: payloadAttaches,
          ...(retryFromRunId ? { retry_from_run_id: retryFromRunId } : {}),
        }),
      });
      const nextSession = ack.session_id || sid || undefined;
      if (!nextSession || !ack.id) {
        throw new Error("AI command did not return a session");
      }
      // Pin session immediately (even if poller gen changes) so the next ask reuses it.
      setSessionId(nextSession);
      setActiveRun(ack.id);
      writeAiPending({
        sessionId: nextSession,
        runId: ack.id,
        command: cmd,
        startedAt: Date.now(),
      });
      if (gen !== pollGenRef.current) return;
      // Show the accepted user turn immediately (run may still be running).
      await loadThread(nextSession);
      if (gen !== pollGenRef.current) return;
      setPendingCommand(null);
      await load();

      if (ack.status === "running") {
        setThinkingLabel(ack.thinking_label || "Preparing answer…");
        const finished = await pollUntilRunDone(nextSession, ack.id, gen);
        if (gen !== pollGenRef.current) return;
        if (finished?.status === "failed" && finished.tool_gap) {
          clearAiPending();
          setActiveRun(undefined);
          await load();
          setThread((prev) => {
            if (!prev) return prev;
            const runs = (prev.runs || []).map((r) =>
              r.id === ack.id
                ? { ...r, status: "failed", result_summary: "Failed.", error: finished.error, tool_gap: true }
                : r,
            );
            const has = runs.some((r) => r.id === ack.id);
            return {
              ...prev,
              status: "failed",
              runs: has
                ? runs
                : [
                    ...runs,
                    {
                      id: ack.id,
                      command: cmd,
                      status: "failed",
                      result_summary: "Failed.",
                      error: finished.error,
                      tool_gap: true,
                    },
                  ],
            };
          });
          return;
        }
      }

      if (gen !== pollGenRef.current) return;
      clearAiPending();
      setActiveRun(undefined);
      await load();
      await loadThread(nextSession);
    } catch (e) {
      if (gen !== pollGenRef.current) return;
      clearAiPending();
      setActiveRun(undefined);
      setError(e instanceof Error ? e.message : "Command failed");
      setPendingCommand(null);
      // Restore draft so the user can retry without retyping.
      setCommand(cmd);
      focusComposer(true);
      if (startingFresh && !sessionIdRef.current) {
        setThread(null);
        setComposerMode("new");
      }
    } finally {
      if (gen === pollGenRef.current) {
        setBusy(false);
        setActiveRun(undefined);
        setThinkingLabel("Preparing answer…");
      }
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    void submit(command);
  }

  function onComposerKeyDown(e: ReactKeyboardEvent<HTMLTextAreaElement>) {
    // Enter = send; Shift+Enter = newline.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (busy || micBusy) return;
      if (!command.trim() && !attachments.length) return;
      void submit(command);
    }
  }

  // Only treat as unconfigured after settings successfully load — avoid a false
  // "GEMINI_API_KEY not configured" banner while loading or after a transient API blip.
  const configured = settingsReady ? Boolean(settings?.configured) : true;

  const pageNumbers = useMemo(() => {
    const pages: (number | "…")[] = [];
    if (totalPages <= 7) {
      for (let i = 1; i <= totalPages; i++) pages.push(i);
      return pages;
    }
    pages.push(1);
    if (safePage > 3) pages.push("…");
    for (let i = Math.max(2, safePage - 1); i <= Math.min(totalPages - 1, safePage + 1); i++) pages.push(i);
    if (safePage < totalPages - 2) pages.push("…");
    pages.push(totalPages);
    return pages;
  }, [safePage, totalPages]);

  return (
    <div className="ai-agent">
      <div className="ai-agent-top">
        <div className="ai-agent-tabs" role="tablist" aria-label="AI Agent sections">
          <button
            type="button"
            role="tab"
            aria-selected={mainTab === "conversations"}
            className={`ai-agent-tab ${mainTab === "conversations" ? "active" : ""}`}
            onClick={() => setMainTab("conversations")}
          >
            <Icon kind="chat" size={16} />
            Conversations
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mainTab === "reports"}
            className={`ai-agent-tab ${mainTab === "reports" ? "active" : ""}`}
            onClick={() => setMainTab("reports")}
          >
            <Icon kind="report" size={16} />
            Work Reports
          </button>
        </div>
        <div className="ai-agent-top-actions">
          <div className={`ai-status-pill tone-${statusTone(settings?.status || "")}`} title="Gemini readiness">
            <span className="ai-status-dot" />
            {settings?.status_label || "Loading…"}
          </div>
          {mainTab === "conversations" && (
            <button type="button" className="btn primary ai-new-chat" onClick={startNewChat}>
              <Icon kind="plus" size={16} />
              New Chat
            </button>
          )}
        </div>
      </div>

      {mainTab === "reports" ? (
        <div className="ai-reports">
          <div className="ai-reports-head">
            <div>
              <h1>Work Reports</h1>
              <p>Completed and running AI Agent tasks (WhatsApp sends, searches, reports).</p>
            </div>
            <button
              type="button"
              className="btn"
              disabled={workReportsLoading}
              onClick={() => void loadWorkReports()}
            >
              Refresh
            </button>
          </div>
          {workReportsError ? <div className="ai-banner err">{workReportsError}</div> : null}
          <div className="ai-work-table-wrap">
            <table className="ai-work-table">
              <thead>
                <tr>
                  <th>Date and time</th>
                  <th>Task name</th>
                  <th>Agent name</th>
                  <th>Task details</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {workReportsLoading && workReports.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="ai-work-empty">
                      Loading…
                    </td>
                  </tr>
                ) : workReports.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="ai-work-empty">
                      No agent tasks logged yet. Completed WhatsApp sends and report jobs will appear here.
                    </td>
                  </tr>
                ) : (
                  workReports.map((row) => (
                    <tr key={row.id}>
                      <td className="ai-work-when">{formatWhen(row.occurred_at || row.created_at)}</td>
                      <td className="ai-work-task">{row.task_name || "—"}</td>
                      <td className="ai-work-agent">{row.agent_name || "—"}</td>
                      <td className="ai-work-details" title={row.task_details || ""}>
                        {row.task_details || "—"}
                      </td>
                      <td>
                        <span className={`ai-work-status tone-${workStatusTone(row.status)}`}>
                          {formatWorkStatus(row.status)}
                        </span>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <div
          className="ai-workspace"
          style={{ ["--ai-list-width" as string]: `${listWidth}px` }}
        >
          <aside className="ai-list-pane">
            <div className="ai-list-head">
              <RobotMark size={40} />
              <div>
                <h1>AI Agent</h1>
              </div>
            </div>

            <div className="ai-list-filters">
              <div className="ai-list-search">
                <Icon kind="search" size={15} />
                <input
                  value={listSearch}
                  onChange={(e) => setListSearch(e.target.value)}
                  placeholder="Search conversations / work…"
                  aria-label="Search conversations"
                />
              </div>
              <div className="ai-list-selects">
                <select
                  value={isAdmin ? listUserFilter : "me"}
                  aria-label="Filter by user"
                  disabled={!isAdmin}
                  onChange={(e) => {
                    setListUserFilter(e.target.value);
                    setPage(1);
                  }}
                >
                  {isAdmin && <option value="all">All Users</option>}
                  <option value="me">{meName} (me)</option>
                  {isAdmin &&
                    sessionOwnerOptions.map((o) => (
                      <option key={o.id} value={String(o.id)}>
                        {o.name}
                      </option>
                    ))}
                </select>
              </div>
            </div>

            <ul className="ai-conv-list">
              {pageConversations.length === 0 && <li className="ai-conv-empty">No conversations yet.</li>}
              {pageConversations.map((c) => {
                const preview = c.title || c.command || "Conversation";
                const kind = runIconKind(c.command || c.title || preview);
                const active = composerMode === "thread" && sessionId === c.session_id;
                const when = c.last_activity_at || c.updated_at || c.created_at;
                const menuOpen = menuOpenId === c.session_id;
                return (
                  <li key={c.session_id}>
                    <div className={`ai-conv-item ${active ? "active" : ""}`}>
                      <button
                        type="button"
                        className="ai-conv-main"
                        onClick={() => selectConversation(c)}
                      >
                        <span className={`ai-conv-ico kind-${kind}`}>
                          <Icon kind={kind} size={15} />
                        </span>
                        <span className="ai-conv-body">
                          <span className="ai-conv-title">{preview}</span>
                          <span className="ai-conv-meta">
                            {c.owner_name || meName}
                            {when ? ` · ${formatWhen(when)}` : ""}
                          </span>
                        </span>
                      </button>
                      <div className="ai-conv-menu-wrap">
                        <button
                          type="button"
                          className={`ai-conv-more ${menuOpen ? "open" : ""}`}
                          aria-label="Conversation actions"
                          aria-haspopup="menu"
                          aria-expanded={menuOpen}
                          onClick={(e) => {
                            e.stopPropagation();
                            setMenuOpenId((id) => (id === c.session_id ? null : c.session_id));
                          }}
                        >
                          <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden>
                            <circle cx="12" cy="5" r="1.8" fill="currentColor" />
                            <circle cx="12" cy="12" r="1.8" fill="currentColor" />
                            <circle cx="12" cy="19" r="1.8" fill="currentColor" />
                          </svg>
                        </button>
                        {menuOpen ? (
                          <div className="ai-conv-menu-pop" role="menu">
                            <button
                              type="button"
                              role="menuitem"
                              onClick={(e) => {
                                e.stopPropagation();
                                openRename(c);
                              }}
                            >
                              Edit
                            </button>
                            <button
                              type="button"
                              role="menuitem"
                              onClick={(e) => {
                                e.stopPropagation();
                                void deleteConversation(c);
                              }}
                            >
                              Delete
                            </button>
                          </div>
                        ) : null}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>

            <div className="ai-list-pager">
              <span>
                Showing {showingFrom}-{showingTo} of {filteredConversations.length}
              </span>
              <div className="ai-pager-btns">
                <button
                  type="button"
                  disabled={safePage <= 1}
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  aria-label="Previous page"
                >
                  ‹
                </button>
                {pageNumbers.map((n, i) =>
                  n === "…" ? (
                    <span key={`e-${i}`} className="ai-pager-ellipsis">
                      …
                    </span>
                  ) : (
                    <button
                      key={n}
                      type="button"
                      className={n === safePage ? "on" : ""}
                      onClick={() => setPage(n)}
                    >
                      {n}
                    </button>
                  )
                )}
                <button
                  type="button"
                  disabled={safePage >= totalPages}
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  aria-label="Next page"
                >
                  ›
                </button>
              </div>
            </div>
          </aside>

          <div
            className="ai-resize-handle"
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize conversation list"
            aria-valuemin={LIST_WIDTH_MIN}
            aria-valuemax={LIST_WIDTH_MAX}
            aria-valuenow={listWidth}
            onPointerDown={onResizePointerDown}
          />

          <section className="ai-chat-pane">
            {error && <div className="ai-banner err ai-chat-banner">{error}</div>}
            {!configured && (
              <div className="ai-banner warn ai-chat-banner">
                <strong>Not configured.</strong> Set <code>GEMINI_API_KEY</code> then hard-refresh.
              </div>
            )}

            {composerMode === "new" || !thread ? (
              <div className="ai-empty-state">
                <RobotMark size={88} />
                <h2>Start a new conversation</h2>
                <p>Ask YesWeigh AI to find dealers, run campaigns, analyse sales, or get business insights.</p>
              </div>
            ) : (
              <div className="ai-thread">
                <div className="ai-thread-scroll">
                  {(thread.runs || []).map((run) => {
                    const failedGap = isRetryableFailedRun(run);
                    const stopped = isStoppedRun(run);
                    const body = failedGap ? "Failed." : stopped ? "Stopped." : run.error || run.result_summary;
                    const reportCard =
                      !failedGap && !stopped ? extractBusinessReport(run.tool_results) : null;
                    const dealerJobId =
                      !failedGap && !stopped
                        ? extractDealerFinderJobId(run.tool_results, run.result_summary)
                        : null;
                    const isDealerFinderReport =
                      Boolean(dealerJobId) &&
                      Boolean(
                        reportCard?.exportBasename?.startsWith("dealer-finder") ||
                          dealerFinderCardNeedsRefresh(run.tool_results) ||
                          hasDealerFinderResultsCard(run.tool_results),
                      );
                    const dealerJobPending =
                      !failedGap &&
                      !stopped &&
                      !reportCard &&
                      Boolean(dealerJobId);
                    const showUser = !run.user_deleted;
                    const showAssistant =
                      !run.assistant_deleted &&
                      Boolean(
                        run.result_summary ||
                          run.error ||
                          failedGap ||
                          stopped ||
                          reportCard ||
                          dealerJobPending ||
                          isDealerFinderReport,
                      );
                    if (!showUser && !showAssistant) return null;
                    const userMenuId = `${run.id}:user`;
                    const asstMenuId = `${run.id}:assistant`;
                    return (
                      <div key={run.id} className="ai-thread-turn">
                        {showUser && (
                          <div className="ai-bubble user">
                            <div className="ai-bubble-top">
                              <div className="ai-bubble-label">You</div>
                              <div className="ai-bubble-menu-wrap">
                                <button
                                  type="button"
                                  className={`ai-bubble-more ${bubbleMenuId === userMenuId ? "open" : ""}`}
                                  aria-label="Message actions"
                                  aria-expanded={bubbleMenuId === userMenuId}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setBubbleMenuId((id) => (id === userMenuId ? null : userMenuId));
                                  }}
                                >
                                  ⋮
                                </button>
                                {bubbleMenuId === userMenuId && (
                                  <div className="ai-bubble-menu-pop" role="menu">
                                    <button
                                      type="button"
                                      role="menuitem"
                                      disabled={busy || micBusy}
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        editUserMessage(run.command || "");
                                      }}
                                    >
                                      Edit
                                    </button>
                                    <button
                                      type="button"
                                      role="menuitem"
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        void deleteBubble(run, "user");
                                      }}
                                    >
                                      Delete
                                    </button>
                                  </div>
                                )}
                              </div>
                            </div>
                            <div className="ai-bubble-text">{run.command}</div>
                          </div>
                        )}

                        {showAssistant && (
                          <div
                            className={`ai-bubble assistant ${run.error || failedGap ? "err" : ""} ${stopped ? "ai-bubble-stopped" : ""} ${reportCard || dealerJobPending || isDealerFinderReport ? "ai-bubble-card" : ""}`}
                          >
                            <div className="ai-bubble-top">
                              <div className="ai-bubble-label">AI Agent</div>
                              <div className="ai-bubble-menu-wrap">
                                <button
                                  type="button"
                                  className={`ai-bubble-more ${bubbleMenuId === asstMenuId ? "open" : ""}`}
                                  aria-label="Message actions"
                                  aria-expanded={bubbleMenuId === asstMenuId}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setBubbleMenuId((id) => (id === asstMenuId ? null : asstMenuId));
                                  }}
                                >
                                  ⋮
                                </button>
                                {bubbleMenuId === asstMenuId && (
                                  <div className="ai-bubble-menu-pop" role="menu">
                                    <button
                                      type="button"
                                      role="menuitem"
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        void deleteBubble(run, "assistant");
                                      }}
                                    >
                                      Delete
                                    </button>
                                  </div>
                                )}
                              </div>
                            </div>
                            {failedGap ? (
                              <div className="ai-bubble-failed">
                                <div className="ai-bubble-text">Failed.</div>
                                <button
                                  type="button"
                                  className="ai-retry-btn"
                                  disabled={busy || micBusy}
                                  onClick={() => void submit(run.command, { retryFromRunId: run.id })}
                                >
                                  Retry
                                </button>
                              </div>
                            ) : stopped ? (
                              <div className="ai-bubble-text">Stopped.</div>
                            ) : isDealerFinderReport || dealerJobPending ? (
                              <DealerFinderLiveCard
                                toolResults={run.tool_results}
                                resultSummary={run.result_summary}
                                fallbackReport={
                                  reportCard?.exportBasename?.startsWith("dealer-finder")
                                    ? reportCard
                                    : null
                                }
                              />
                            ) : reportCard ? (
                              <>
                                <AiMarkdown className="ai-bubble-text ai-bubble-blurb">
                                  {reportCard.shortText}
                                </AiMarkdown>
                                <BusinessReportCard data={reportCard} />
                              </>
                            ) : (
                              <AiMarkdown className="ai-bubble-text">
                                {body || ""}
                              </AiMarkdown>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}

                  {pendingCommand && (
                    <div className="ai-thread-turn">
                      <div className="ai-bubble user">
                        <div className="ai-bubble-label">You</div>
                        <div className="ai-bubble-text">{pendingCommand}</div>
                      </div>
                    </div>
                  )}

                  {busy && (
                    <div className="ai-bubble assistant muted ai-thinking" aria-live="polite">
                      <div className="ai-bubble-text ai-thinking-text">
                        <span>{thinkingLabel}</span>
                        <span className="ai-thinking-dots" aria-hidden>
                          <span />
                          <span />
                          <span />
                        </span>
                      </div>
                      <button
                        type="button"
                        className="ai-stop-inline"
                        onClick={() => void stopGeneration()}
                      >
                        Stop
                      </button>
                    </div>
                  )}
                  <div ref={threadEndRef} aria-hidden />
                </div>
              </div>
            )}

            <form className="ai-composer" onSubmit={onSubmit}>
              <span className="ai-composer-spark" aria-hidden>
                <Icon kind="spark" size={18} />
              </span>
              <div className="ai-composer-main">
                {attachments.length > 0 && (
                  <div className="ai-attach-chips">
                    {attachments.map((a) => (
                      <div key={a.id} className="ai-attach-chip">
                        {a.mime.startsWith("image/") ? (
                          <img src={a.dataUrl} alt={a.name} />
                        ) : (
                          <span className="ai-attach-file">{a.name}</span>
                        )}
                        <button
                          type="button"
                          className="ai-attach-x"
                          aria-label={`Remove ${a.name}`}
                          disabled={busy}
                          onClick={() => removeAttachment(a.id)}
                        >
                          ×
                        </button>
                      </div>
                    ))}
                  </div>
                )}
                <textarea
                  id="ai-command"
                  ref={commandInputRef}
                  rows={1}
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  onKeyDown={onComposerKeyDown}
                  placeholder={
                    recording
                      ? "Recording… tap mic to stop"
                      : transcribing
                        ? "Transcribing…"
                        : "Type your request..."
                  }
                  disabled={busy || micBusy}
                  autoComplete="off"
                  aria-label="Message"
                />
                {speechHint ? (
                  <div className={`ai-speech-hint ${transcribing ? "transcribing" : ""}`}>
                    {speechHint}
                  </div>
                ) : (
                  <p className="ai-composer-hint">Enter to send · Shift+Enter for new line</p>
                )}
              </div>
              <input
                ref={fileInputRef}
                type="file"
                className="ai-file-input"
                accept="image/*,.pdf,.txt,.csv,.json,.md,.log,application/pdf,text/plain,text/csv,application/json"
                multiple
                disabled={busy || micBusy}
                onChange={(e) => {
                  void addFiles(e.target.files);
                  e.target.value = "";
                }}
              />
              <button
                type="button"
                className="ai-tool-btn"
                disabled={busy || micBusy || attachments.length >= 3}
                aria-label="Attach file or image"
                title="Attach file or image"
                onClick={() => fileInputRef.current?.click()}
              >
                <Icon kind="paperclip" size={18} />
              </button>
              <button
                type="button"
                className={`ai-tool-btn ${recording ? "recording" : ""} ${transcribing ? "transcribing" : ""}`}
                disabled={busy || transcribing}
                aria-label={
                  recording ? "Stop recording" : transcribing ? "Transcribing" : "Record voice"
                }
                title={
                  recording
                    ? "Stop recording"
                    : transcribing
                      ? "Transcribing…"
                      : "Record voice (Sarvam → Malayalam)"
                }
                onClick={() => toggleMic()}
              >
                <Icon kind={recording ? "mic-off" : "mic"} size={18} />
              </button>
              {busy ? (
                <button
                  type="button"
                  className="ai-send-btn ai-stop-btn"
                  aria-label="Stop generating"
                  title="Stop"
                  onClick={() => void stopGeneration()}
                >
                  <Icon kind="stop" size={14} />
                </button>
              ) : (
                <button
                  type="submit"
                  className="ai-send-btn"
                  disabled={micBusy || (!command.trim() && !attachments.length)}
                  aria-label="Send"
                  title="Send (Enter)"
                >
                  <Icon kind="send" size={16} />
                </button>
              )}
            </form>
          </section>
        </div>
      )}

      {renameTarget && (
        <div
          className="zoho-modal"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget) closeRename();
          }}
        >
          <form
            className="zoho-dialog ai-rename-dialog"
            role="dialog"
            aria-modal="true"
            aria-label="Edit conversation"
            onSubmit={(e) => void saveRename(e)}
          >
            <div className="zoho-dialog-head">
              <div>
                <h2>Edit conversation</h2>
                <p>Rename the title shown in the conversation list.</p>
              </div>
              <button type="button" className="btn" onClick={closeRename} aria-label="Close" disabled={renameBusy}>
                ✕
              </button>
            </div>
            <label className="ai-rename-label" htmlFor="ai-rename-title">
              Title
            </label>
            <input
              id="ai-rename-title"
              className="ai-rename-input"
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              maxLength={200}
              autoFocus
              disabled={renameBusy}
            />
            <div className="zoho-dialog-actions">
              <button type="button" className="btn" onClick={closeRename} disabled={renameBusy}>
                Cancel
              </button>
              <button type="submit" className="btn primary" disabled={renameBusy || !renameValue.trim()}>
                {renameBusy ? "Saving…" : "Save"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
