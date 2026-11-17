import { FormEvent, PointerEvent as ReactPointerEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import BusinessReportCard, { extractBusinessReport } from "../components/BusinessReportCard";
import {
  clearActiveSession,
  clearAiPending,
  readActiveSession,
  readAiPending,
  sleep,
  writeActiveSession,
  writeAiPending,
} from "../lib/aiPending";

type AiUsage = {
  spent_today_inr: number;
  spent_month_inr: number;
  daily_budget_inr: number;
  monthly_budget_inr: number;
  remaining_monthly_inr: number;
  requests_today: number;
  requests_month: number;
  input_tokens_month: number;
  output_tokens_month: number;
};

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
  usage: AiUsage;
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
};

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

function inr(n: number | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
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
  if (run.tool_gap) return true;
  if (run.status === "failed") return true;
  if (run.factory && run.factory.ok === false) return true;
  if (run.events?.some((e) => e.kind === "tool_gap" || (e.kind === "factory" && e.status === "error"))) {
    return true;
  }
  return looksLikeFailedReply(run.error || run.result_summary || "");
}

/** Client-side ceiling so "Preparing answer…" cannot spin forever if the server stalls. */
const POLL_MAX_MS = 4 * 60 * 1000;

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

export default function Work() {
  const [mainTab, setMainTab] = useState<MainTab>("conversations");
  const [settings, setSettings] = useState<AiSettings | null>(null);
  const [meName, setMeName] = useState("You");
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
  /** Sync ref so send always reuses the latest session even before React re-renders. */
  const sessionIdRef = useRef<string | undefined>(readActiveSession() || undefined);
  /** Bumped on unmount so in-component pollers stop updating state (server job keeps running). */
  const pollGenRef = useRef(0);

  function setSessionId(next: string | undefined) {
    sessionIdRef.current = next;
    setSessionIdState(next);
    if (next) writeActiveSession(next);
    else clearActiveSession();
  }

  useEffect(() => {
    document.title = "AI Agent · YesWeigh CRM";
    return () => {
      document.title = "YesWeigh CRM";
      pollGenRef.current += 1;
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

  const load = useCallback(async () => {
    const [s, convs, me] = await Promise.all([
      api<AiSettings>("/api/ai/settings"),
      api<AiConversation[]>("/api/ai/sessions?limit=100"),
      api<{ name?: string }>("/api/me").catch(() => ({ name: "You" })),
    ]);
    setSettings(s);
    setConversations(convs);
    if (me?.name) setMeName(me.name);
  }, []);

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
    load().catch((e) => setError(e instanceof Error ? e.message : "Failed to load AI Agent"));
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
          return;
        }
        const finished = await pollUntilRunDone(pending.sessionId, pending.runId, gen);
        if (gen !== pollGenRef.current) return;
        clearAiPending();
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

  const filteredConversations = useMemo(() => {
    const q = listSearch.trim().toLowerCase();
    return conversations.filter((c) => {
      const hay = `${c.title || ""} ${c.command || ""}`.toLowerCase();
      if (q && !hay.includes(q)) return false;
      return true;
    });
  }, [conversations, listSearch]);

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
    setMainTab("conversations");
  }

  function selectConversation(conv: AiConversation) {
    setMenuOpenId(null);
    setError("");
    setPendingCommand(null);
    setBusy(false);
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

  async function submit(text: string) {
    const cmd = text.trim();
    if (!cmd || busy) return;

    // Reuse active session unless New Chat cleared it — never force-null from composerMode.
    const sid = sessionIdRef.current || sessionId || readActiveSession() || null;
    const startingFresh = !sid;
    const gen = pollGenRef.current;
    setCommand("");
    setError("");
    setPendingCommand(cmd);
    setBusy(true);
    setThinkingLabel("Preparing answer…");
    setComposerMode("thread");
    if (!thread || startingFresh) {
      setThread({
        id: sid || "pending",
        session_id: sid || "pending",
        title: cmd.slice(0, 80),
        status: "running",
        runs: [],
      });
    }

    try {
      // Returns quickly with status=running; work continues on the server.
      const ack = await api<AiRun>("/api/ai/command", {
        method: "POST",
        body: JSON.stringify({ command: cmd, session_id: sid }),
      });
      const nextSession = ack.session_id || sid || undefined;
      if (!nextSession || !ack.id) {
        throw new Error("AI command did not return a session");
      }
      // Pin session immediately (even if poller gen changes) so the next ask reuses it.
      setSessionId(nextSession);
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
      await load();
      await loadThread(nextSession);
    } catch (e) {
      if (gen !== pollGenRef.current) return;
      clearAiPending();
      setError(e instanceof Error ? e.message : "Command failed");
      setPendingCommand(null);
      // Restore draft so the user can retry without retyping.
      setCommand(cmd);
      if (startingFresh && !sessionIdRef.current) {
        setThread(null);
        setComposerMode("new");
      }
    } finally {
      if (gen === pollGenRef.current) {
        setBusy(false);
        setThinkingLabel("Preparing answer…");
      }
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void submit(command);
  }

  const usage = settings?.usage;
  const configured = settings?.configured ?? false;

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
            <h1>AI Agent</h1>
            <p>Usage, model, and budget for Gemini commands.</p>
          </div>
          {!configured && (
            <div className="ai-banner warn">
              <strong>Not configured.</strong> Set <code>GEMINI_API_KEY</code> in the server <code>.env</code>,
              restart the API container, then hard-refresh this page.
            </div>
          )}
          <div className="ai-kpis">
            <div className="ai-kpi">
              <div className="ai-kpi-label">Status</div>
              <div className="ai-kpi-value">{settings?.status_label || "—"}</div>
              <div className="ai-kpi-sub">{configured ? "Gemini key present" : "GEMINI_API_KEY missing"}</div>
            </div>
            <div className="ai-kpi">
              <div className="ai-kpi-label">Model</div>
              <div className="ai-kpi-value ai-kpi-model">{settings?.model || "—"}</div>
              <div className="ai-kpi-sub">{settings?.provider || "gemini"}</div>
            </div>
            <div className="ai-kpi">
              <div className="ai-kpi-label">Spent this month</div>
              <div className="ai-kpi-value">{inr(usage?.spent_month_inr)}</div>
              <div className="ai-kpi-sub">Budget {inr(usage?.monthly_budget_inr)}</div>
            </div>
            <div className="ai-kpi">
              <div className="ai-kpi-label">Remaining</div>
              <div className="ai-kpi-value">{inr(usage?.remaining_monthly_inr)}</div>
              <div className="ai-kpi-sub">
                Today {inr(usage?.spent_today_inr)} / {inr(usage?.daily_budget_inr)}
              </div>
            </div>
            <div className="ai-kpi">
              <div className="ai-kpi-label">Requests</div>
              <div className="ai-kpi-value">{usage?.requests_month ?? "—"}</div>
              <div className="ai-kpi-sub">{usage?.requests_today ?? 0} today</div>
            </div>
            <div className="ai-kpi">
              <div className="ai-kpi-label">Automations</div>
              <div className="ai-kpi-value">0</div>
              <div className="ai-kpi-sub">Phase 2 worker</div>
            </div>
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
                <select value="all" aria-label="Filter by user" disabled>
                  <option value="all">All Users</option>
                  <option value="me">{meName}</option>
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
                            {meName}
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
                    const body = failedGap ? "Failed." : run.error || run.result_summary;
                    const reportCard = !failedGap ? extractBusinessReport(run.tool_results) : null;
                    const showUser = !run.user_deleted;
                    const showAssistant =
                      !run.assistant_deleted && Boolean(run.result_summary || run.error || failedGap || reportCard);
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
                            className={`ai-bubble assistant ${run.error || failedGap ? "err" : ""} ${reportCard ? "ai-bubble-card" : ""}`}
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
                                  disabled={busy}
                                  onClick={() => void submit(run.command)}
                                >
                                  Retry
                                </button>
                              </div>
                            ) : reportCard ? (
                              <>
                                <div className="ai-bubble-text ai-bubble-blurb">
                                  {reportCard.shortText}
                                </div>
                                <BusinessReportCard data={reportCard} />
                              </>
                            ) : (
                              <div className="ai-bubble-text">{body}</div>
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
              <input
                id="ai-command"
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                placeholder="Type your request..."
                disabled={busy}
                autoComplete="off"
              />
              <button
                type="submit"
                className="ai-send-btn"
                disabled={busy || !command.trim()}
                aria-label={busy ? "Sending" : "Send"}
              >
                <Icon kind="send" size={16} />
              </button>
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
