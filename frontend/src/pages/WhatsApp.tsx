import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type MouseEvent as ReactMouseEvent } from "react";
import { api, apiWithTimeout } from "../api/client";
import RightPanel from "../components/whatsapp/RightPanel";
import type { AiSummary } from "../components/whatsapp/AiSummaryCard";
import { SearchIcon } from "../components/whatsapp/icons";

const COL_STORAGE_KEY = "wa_inbox_col_widths_v1";
const DEFAULT_COLS = { left: 26, mid: 36, right: 38 };

type Account = {
  id: string;
  display_name: string;
  connection_status: string;
  connected_phone_number?: string;
  unread_count?: number;
  provider?: string;
};

type DealerBrief = {
  id: number;
  name: string;
  company?: string;
  phone: string;
  code?: string;
  city?: string;
  state?: string;
};

type Conv = {
  id: string;
  display_name: string;
  customer_name: string;
  customer_phone: string;
  customer_photo?: string;
  customer_jid?: string;
  chat_kind?: "dm" | "group" | "channel" | "broadcast" | string;
  is_group?: boolean;
  is_channel?: boolean;
  initial: string;
  color: string;
  status: string;
  tag: string;
  assigned_agent: string;
  account: string;
  last_message_preview: string;
  last_message_at?: string | null;
  unread_count: number;
  language?: string;
  region?: string;
  customer_type?: string;
  dealer_id?: number | null;
  dealer?: DealerBrief | null;
  ai_outline?: string;
  ai_outline_at?: string | null;
  /** Care Meta Cloud only — absent on Evolution personal lines */
  provider?: string;
  care_window?: "open" | "expired" | string;
  care_window_open?: boolean;
  can_freeform_reply?: boolean;
  last_inbound_at?: string | null;
  care_window_expires_at?: string | null;
};

function applyAvatarResult(
  prev: Conv,
  r: { customer_photo?: string; customer_name?: string; display_name?: string; chat_kind?: string; is_group?: boolean; is_channel?: boolean },
): Conv {
  const name = (r.customer_name || r.display_name || "").trim();
  const photo = r.customer_photo || prev.customer_photo;
  const display = name || prev.display_name || prev.customer_phone;
  return {
    ...prev,
    customer_photo: photo,
    customer_name: name || prev.customer_name,
    display_name: display,
    chat_kind: r.chat_kind || prev.chat_kind,
    is_group: r.is_group ?? prev.is_group,
    is_channel: r.is_channel ?? prev.is_channel,
    initial: name ? name.slice(0, 2).toUpperCase() : prev.initial,
  };
}

function chatKindBadge(c: Conv): string | null {
  // Channels (@newsletter) are hidden by the list API; real @g.us groups get a badge.
  if (c.is_group || c.chat_kind === "group") return "Group";
  return null;
}

function isCareAccount(a: { provider?: string; display_name?: string } | null | undefined): boolean {
  if (!a) return false;
  return (a.provider || "") === "meta_cloud" || a.display_name === "Care";
}

function isCareConv(c: Conv | null | undefined): boolean {
  if (!c) return false;
  return (c.provider || "") === "meta_cloud" || !!c.care_window;
}

/** Evolution employee lines first (name order), Care (meta_cloud) last. */
function sortAccountsCareLast(items: Account[]): Account[] {
  return [...items].sort((a, b) => {
    const ca = isCareAccount(a) ? 1 : 0;
    const cb = isCareAccount(b) ? 1 : 0;
    if (ca !== cb) return ca - cb;
    return (a.display_name || "").localeCompare(b.display_name || "", undefined, { sensitivity: "base" });
  });
}

function careWindowBadge(c: Conv): { label: string; className: string } | null {
  if (!isCareConv(c) || !c.care_window) return null;
  if (c.care_window === "open" || c.care_window_open) {
    return { label: "Open", className: "care-open" };
  }
  return { label: "Expired", className: "care-expired" };
}

function careFreeformBlocked(c: Conv | null | undefined): boolean {
  if (!c || !isCareConv(c)) return false;
  if (typeof c.can_freeform_reply === "boolean") return !c.can_freeform_reply;
  return c.care_window === "expired";
}

/** WhatsApp-style circular DP with silhouette fallback */
function WaDp({
  photo,
  initial,
  color,
  size = 49,
  className = "",
}: {
  photo?: string;
  initial: string;
  color?: string;
  size?: number;
  className?: string;
}) {
  const [broken, setBroken] = useState(false);
  const showImg = !!photo && !broken;
  return (
    <div
      className={`wa-dp ${className}`}
      style={{
        width: size,
        height: size,
        background: showImg ? "#dfE5e7" : color || "#dfe5e7",
        fontSize: Math.max(12, Math.round(size * 0.34)),
      }}
    >
      {showImg ? (
        <img src={photo} alt="" onError={() => setBroken(true)} />
      ) : (
        <span>{initial || "?"}</span>
      )}
    </div>
  );
}

type Msg = {
  id: string;
  direction: "in" | "out" | "note";
  body: string;
  status: string;
  sender_name: string;
  created_at?: string;
  media_name?: string;
  media_url?: string;
  media_type?: string;
  is_voice?: boolean;
  transcript?: string;
  transcript_language?: string;
  transcription_status?: string;
  transcription_error?: string;
};

function mediaKind(m: Msg): "image" | "video" | "audio" | "document" | "sticker" | "" {
  const t = (m.media_type || "").toLowerCase();
  if (t === "ptt" || t === "voice") return "audio";
  if (t === "image" || t === "sticker" || t === "video" || t === "audio" || t === "document") return t as "image" | "video" | "audio" | "document" | "sticker";
  const name = `${m.media_name || ""} ${m.media_url || ""}`.toLowerCase();
  if (/\.(jpe?g|png|gif|webp|bmp)(\?|$)/i.test(name)) return "image";
  if (/\.(mp4|webm|mov|m4v)(\?|$)/i.test(name)) return "video";
  if (/\.(ogg|opus|mp3|wav|m4a|webm)(\?|$)/i.test(name) || m.is_voice) return "audio";
  if (m.media_url || m.media_name) return "document";
  return "";
}

function isMediaPlaceholder(body?: string) {
  if (!body) return true;
  const b = body.trim();
  if (["📷 Photo", "🎥 Video", "🎤 Voice message", "📄 Document", "Sticker", "[media]", "media", "Attachment"].includes(b)) return true;
  return /^(📷|🎥|🎤|📄)?\s*(Photo|Video|Voice message|Document|Sticker|\[media\]|Attachment)$/i.test(b);
}

function isUnsupportedPlaceholder(body?: string, mediaType?: string) {
  const kind = (mediaType || "").toLowerCase();
  if (kind === "unsupported" || kind === "unknown") return true;
  const b = (body || "").trim().toLowerCase();
  return (
    b === "unsupported message"
    || b === "whatsapp did not include this message."
    || b === "unsupported"
    || b === "unknown"
  );
}

function mediaPlaceholderLabel(kind: string, m: Msg): string {
  if (kind === "audio") return "🎤 Voice message";
  if (kind === "image") return "📷 Photo";
  if (kind === "video") return "🎥 Video";
  if (kind === "sticker") return "Sticker";
  if (kind === "document") return m.media_name ? `📄 ${m.media_name}` : "📄 Document";
  if (m.body && isMediaPlaceholder(m.body) && m.body.trim() !== "[media]" && m.body.trim().toLowerCase() !== "media") {
    return m.body;
  }
  // Never show raw "[media]" in the bubble.
  return "Attachment unavailable";
}

function WaTicks({ status }: { status: string }) {
  const s = (status || "").toLowerCase();
  if (s === "failed") return <span className="wa-tick failed" title="Failed">!</span>;
  if (s === "sending" || s === "queued") {
    return (
      <svg className="wa-ticks" viewBox="0 0 16 11" width="16" height="11" aria-label="Sending">
        <path d="M11.071.653a.457.457 0 0 0-.304-.102.493.493 0 0 0-.381.178l-6.19 7.636-2.405-2.272a.463.463 0 0 0-.336-.146.47.47 0 0 0-.349.146l-.39.397a.484.484 0 0 0 0 .694l3.127 2.96c.10.0.0.2.10.0.1.5.266.10.0.1.1 0 .27-.04.38-.13.03-.024.075-.061.11-.105L12.58 1.97a.497.497 0 0 0-.03-.695L11.07.653z" fill="currentColor" opacity=".5"/>
      </svg>
    );
  }
  // single tick = sent
  if (s === "sent") {
    return (
      <svg className="wa-ticks" viewBox="0 0 16 11" width="16" height="11" aria-label="Sent">
        <path d="M11.071.653a.457.457 0 0 0-.304-.102.493.493 0 0 0-.381.178l-6.19 7.636-2.405-2.272a.463.463 0 0 0-.336-.146.47.47 0 0 0-.349.146l-.39.397a.484.484 0 0 0 0 .694l3.127 2.96c.10.0.0.2.10.0.1.5.266.10.0.1.1 0 .27-.04.38-.13.03-.024.075-.061.11-.105L12.58 1.97a.497.497 0 0 0-.03-.695L11.07.653z" fill="currentColor"/>
      </svg>
    );
  }
  // double tick — gray delivered / blue read
  const cls = s === "read" ? "wa-ticks read" : "wa-ticks";
  return (
    <svg className={cls} viewBox="0 0 16 11" width="16" height="11" aria-label={s === "read" ? "Read" : "Delivered"}>
      <path d="M11.071.653a.457.457 0 0 0-.304-.102.493.493 0 0 0-.381.178l-6.19 7.636-2.405-2.272a.463.463 0 0 0-.336-.146.47.47 0 0 0-.349.146l-.39.397a.484.484 0 0 0 0 .694l3.127 2.96c.10.0.0.2.10.0.1.5.266.10.0.1.1 0 .27-.04.38-.13.03-.024.075-.061.11-.105L12.58 1.97a.497.497 0 0 0-.03-.695L11.07.653z" fill="currentColor"/>
      <path d="M14.6.653a.457.457 0 0 0-.304-.102.493.493 0 0 0-.381.178l-6.19 7.636-.91-.86a.47.47 0 0 0-.34-.14.48.48 0 0 0-.35.15l-.27.27a.48.48 0 0 0 0 .69l1.61 1.52c.10.0.0.2.10.0.1.5.27.10.0.0.2 0 .27-.04.38-.13.03-.02.07-.06.11-.1L15.11 1.97a.5.5 0 0 0-.03-.7L14.6.653z" fill="currentColor"/>
    </svg>
  );
}

type Metrics = {
  open: number;
  unread: number;
  unread_conversations?: number;
  unassigned: number;
  agents_online: number;
  agents_total: number;
};

const TABS = ["All", "Groups", "Mine", "Unassigned", "Unread", "Resolved"] as const;

function fmtTime(v?: string | null) {
  if (!v) return "";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  return sameDay
    ? d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}

function tagClass(t: string) {
  const v = (t || "").toLowerCase();
  if (v.includes("hot")) return "hot";
  if (v.includes("new") || v.includes("reopen")) return "new";
  if (v.includes("resolved") || v.includes("good")) return "good";
  if (v === "dealer") return "dealer";
  return "";
}

function accountConnected(status?: string) {
  const s = (status || "").toLowerCase();
  return s === "connected" || s === "open" || s === "online";
}

export default function WhatsApp() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountId, setAccountId] = useState("all");
  const [tab, setTab] = useState<(typeof TABS)[number]>("All");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("recent");
  const [items, setItems] = useState<Conv[]>([]);
  const [metrics, setMetrics] = useState<Metrics>({ open: 0, unread: 0, unassigned: 0, agents_online: 0, agents_total: 0 });
  const [selected, setSelected] = useState<string | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [mode, setMode] = useState<"reply" | "note">("reply");
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [toast, setToast] = useState("");
  const [profileOpen, setProfileOpen] = useState(true);
  const [newOpen, setNewOpen] = useState(false);
  const [newPhone, setNewPhone] = useState("");
  const [newName, setNewName] = useState("");
  const [loading, setLoading] = useState(true);
  const [listRefreshing, setListRefreshing] = useState(false);
  const [recording, setRecording] = useState(false);
  const [recSecs, setRecSecs] = useState(0);
  const [emojiOpen, setEmojiOpen] = useState(false);
  const [attachOpen, setAttachOpen] = useState(false);
  const [lightbox, setLightbox] = useState<{ src: string; name?: string } | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [editName, setEditName] = useState("");
  const [editRegion, setEditRegion] = useState("");
  const [editLanguage, setEditLanguage] = useState("");
  const [dealerSearchOpen, setDealerSearchOpen] = useState(false);
  const [dealerQ, setDealerQ] = useState("");
  const [dealerHits, setDealerHits] = useState<DealerBrief[]>([]);
  const [dealerSearching, setDealerSearching] = useState(false);
  const [selectedDealerId, setSelectedDealerId] = useState<number | null>(null);
  const [linkingDealer, setLinkingDealer] = useState(false);
  const [summary, setSummary] = useState<AiSummary | null>(null);
  const [outlineAt, setOutlineAt] = useState<string | null>(null);
  const [outlineLoading, setOutlineLoading] = useState(false);
  const [outlineError, setOutlineError] = useState("");
  const [outlineStale, setOutlineStale] = useState(false);
  const [cols, setCols] = useState(() => {
    try {
      const raw = localStorage.getItem(COL_STORAGE_KEY);
      if (!raw) return DEFAULT_COLS;
      const parsed = JSON.parse(raw) as { left?: number; mid?: number; right?: number };
      const left = Number(parsed.left) || DEFAULT_COLS.left;
      const mid = Number(parsed.mid) || DEFAULT_COLS.mid;
      const right = Number(parsed.right) || DEFAULT_COLS.right;
      const sum = left + mid + right;
      if (sum < 90 || sum > 110) return DEFAULT_COLS;
      return { left, mid, right };
    } catch {
      return DEFAULT_COLS;
    }
  });
  const dragRef = useRef<{ which: "left" | "right"; startX: number; start: typeof DEFAULT_COLS } | null>(null);
  const workspaceRef = useRef<HTMLElement | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const recTimerRef = useRef<number | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const draftRef = useRef<HTMLTextAreaElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const historyRequestedRef = useRef<Set<string>>(new Set());
  const listCacheRef = useRef<Map<string, { items: Conv[]; metrics: Metrics }>>(new Map());
  const listAbortRef = useRef<AbortController | null>(null);
  const listGenRef = useRef(0);
  const avatarTimersRef = useRef<number[]>([]);
  const accountIdRef = useRef(accountId);
  accountIdRef.current = accountId;

  const EMOJIS = ["😀","😁","😂","😊","😍","😘","😎","🤔","😅","😭","🙏","👍","👎","👏","🔥","❤️","💯","🎉","✅","❌","🤝","📍","📞","🧾","📦","💰"];

  const selectedConv = useMemo(() => items.find((c) => c.id === selected) || null, [items, selected]);
  const selectedCareBadge = useMemo(() => (selectedConv ? careWindowBadge(selectedConv) : null), [selectedConv]);
  const selectedCareBlocked = careFreeformBlocked(selectedConv);

  function listCacheKey(acc: string, t: string, s: string, query: string) {
    return `${acc}|${t}|${s}|${query}`;
  }

  function clearAvatarTimers() {
    for (const id of avatarTimersRef.current) window.clearTimeout(id);
    avatarTimersRef.current = [];
  }

  function selectAccount(nextId: string) {
    if (nextId === accountIdRef.current) return;
    setSelected(null);
    setAccountId(nextId);
  }

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, selected]);

  useEffect(() => {
    if (!lightbox) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setLightbox(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lightbox]);

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(""), 3200);
  }

  const loadAccounts = useCallback(async () => {
    try {
      const r = await api<{ items: Account[] }>("/api/whatsapp/inbox/accounts");
      setAccounts(sortAccountsCareLast(r.items || []));
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Accounts failed");
    }
  }, []);

  const syncCareInbox = useCallback(async (waId?: string) => {
    try {
      const q = waId ? `?wa_id=${encodeURIComponent(waId)}` : "";
      await api(`/api/whatsapp/care/sync${q}`, { method: "POST", body: "{}" });
    } catch {
      /* best-effort — list poll / webhook still apply */
    }
  }, []);

  const loadList = useCallback(async (opts?: { bypassCache?: boolean; quiet?: boolean }) => {
    const key = listCacheKey(accountId, tab, sort, q);
    const cached = listCacheRef.current.get(key);
    const quiet = !!opts?.quiet;

    // Instant paint from memory when toggling chips / tabs
    if (cached && !opts?.bypassCache) {
      setItems(cached.items);
      setMetrics(cached.metrics);
      setLoading(false);
      if (!quiet) setListRefreshing(true);
    } else if (!quiet) {
      // Keep previous list visible; only first mount shows full Loading…
      setListRefreshing(true);
    }

    listAbortRef.current?.abort();
    clearAvatarTimers();
    const ac = new AbortController();
    listAbortRef.current = ac;
    const gen = ++listGenRef.current;

    const params = new URLSearchParams({
      tab,
      sort,
      q,
      limit: "80",
    });
    if (accountId !== "all") params.set("instance_id", accountId);

    try {
      const res = await apiWithTimeout<{ items: Conv[]; metrics: Metrics }>(
        `/api/whatsapp/conversations?${params}`,
        15000,
        { signal: ac.signal }
      );
      if (ac.signal.aborted || gen !== listGenRef.current) return;

      listCacheRef.current.set(key, { items: res.items, metrics: res.metrics });
      setItems(res.items);
      setMetrics(res.metrics);

      // Defer DP / group-title fetches until after list paint; cancel on rapid toggle
      const missing = res.items
        .filter((c) => {
          const needsPhoto = !c.customer_photo;
          const groupish = Boolean(c.is_group || c.is_channel || c.chat_kind === "group" || c.chat_kind === "channel");
          const name = (c.customer_name || "").trim();
          const needsGroupName =
            groupish && (!name || name === (c.customer_phone || "").trim() || /^@?\d{14,}$/.test(name));
          return needsPhoto || needsGroupName;
        })
        .slice(0, 8);
      const startAvatars = () => {
        if (gen !== listGenRef.current) return;
        missing.forEach((c, i) => {
          const tid = window.setTimeout(() => {
            if (gen !== listGenRef.current) return;
            api<{
              customer_photo: string;
              customer_name?: string;
              display_name?: string;
              chat_kind?: string;
              is_group?: boolean;
              is_channel?: boolean;
            }>(`/api/whatsapp/conversations/${c.id}/avatar`, {
              method: "POST",
              body: "{}",
              signal: ac.signal,
            })
              .then((r) => {
                if (gen !== listGenRef.current) return;
                if (!r.customer_photo && !r.customer_name) return;
                setItems((prev) => {
                  const next = prev.map((x) => (x.id === c.id ? applyAvatarResult(x, r) : x));
                  const cur = listCacheRef.current.get(key);
                  if (cur) listCacheRef.current.set(key, { ...cur, items: next });
                  return next;
                });
              })
              .catch(() => null);
          }, 400 + i * 700);
          avatarTimersRef.current.push(tid);
        });
      };
      if (typeof requestAnimationFrame === "function") {
        requestAnimationFrame(() => {
          const tid = window.setTimeout(startAvatars, 50);
          avatarTimersRef.current.push(tid);
        });
      } else {
        startAvatars();
      }
    } catch (e) {
      if (ac.signal.aborted || (e instanceof DOMException && e.name === "AbortError")) return;
      if (e instanceof Error && e.name === "AbortError") return;
      throw e;
    } finally {
      // Always clear loading for the latest generation — aborted predecessors must not
      // leave the inbox stuck on "Loading…" when a newer request also fails/aborts.
      if (gen === listGenRef.current) {
        setLoading(false);
        setListRefreshing(false);
      }
    }
  }, [tab, sort, q, accountId]);

  async function retryTranscription(messageId: string) {
    try {
      const res = await api<{
        ok?: boolean;
        status?: string;
        transcript?: string;
        transcription_error?: string;
        transcript_language?: string;
      }>(`/api/whatsapp/messages/${messageId}/transcribe`, { method: "POST", body: "{}" });
      setMessages((prev) =>
        prev.map((m) =>
          m.id === messageId
            ? {
                ...m,
                transcript: res.transcript || m.transcript || "",
                transcript_language: res.transcript_language || m.transcript_language || "",
                transcription_status: res.status || m.transcription_status || "",
                transcription_error: res.transcription_error || "",
              }
            : m,
        ),
      );
      if (res.status === "completed" && res.transcript) showToast("Transcript ready");
      else if (res.status === "failed") showToast(res.transcription_error || "Transcription failed");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Transcription failed");
    }
  }

  async function loadMessages(id: string) {
    const res = await api<{ items: Msg[] }>(`/api/whatsapp/conversations/${id}/messages`);
    setMessages(res.items);
    // Refresh outline if new messages/transcripts arrived (uses cache fingerprint).
    loadOutline(id, false).catch(() => null);
    // Poll briefly while Sarvam STT is in flight for voice notes.
    const pending = res.items.some((m) => /pending|processing/i.test(m.transcription_status || ""));
    if (pending) {
      window.setTimeout(() => {
        api<{ items: Msg[] }>(`/api/whatsapp/conversations/${id}/messages`)
          .then((again) => {
            setMessages(again.items);
            loadOutline(id, false).catch(() => null);
          })
          .catch(() => null);
      }, 3500);
    }
    // Do NOT mark read / clear unread / flip New→Open on open or message fetch.
    // Unread + New stay until staff successfully sends a reply (backend clear_conversation_unread).
    // Refresh DP / group-channel title when opening a chat
    const conv = items.find((c) => c.id === id);
    const groupish = Boolean(conv && (conv.is_group || conv.is_channel || conv.chat_kind === "group" || conv.chat_kind === "channel"));
    const gName = (conv?.customer_name || "").trim();
    const needsGroupName =
      groupish && (!gName || gName === (conv?.customer_phone || "").trim() || /^@?\d{14,}$/.test(gName));
    if (conv && (!conv.customer_photo || needsGroupName)) {
      api<{
        customer_photo: string;
        customer_name?: string;
        display_name?: string;
        chat_kind?: string;
        is_group?: boolean;
        is_channel?: boolean;
      }>(`/api/whatsapp/conversations/${id}/avatar`, { method: "POST", body: "{}" })
        .then((r) => {
          if (!r.customer_photo && !r.customer_name) return;
          setItems((prev) => prev.map((x) => (x.id === id ? applyAvatarResult(x, r) : x)));
        })
        .catch(() => null);
    }
    // Empty threads: Evolution history poll, or Care Firestore sync via sync-history.
    if (res.items.length === 0 && !historyRequestedRef.current.has(id)) {
      historyRequestedRef.current.add(id);
      api(`/api/whatsapp/conversations/${id}/sync-history`, { method: "POST", body: "{}" })
        .then(() => {
          window.setTimeout(() => {
            api<{ items: Msg[] }>(`/api/whatsapp/conversations/${id}/messages`)
              .then((again) => {
                if (again.items.length) setMessages(again.items);
              })
              .catch(() => null);
            loadList().catch(() => null);
          }, 2500);
        })
        .catch(() => null);
    }
  }

  useEffect(() => {
    loadAccounts().catch(() => null);
    const t = setInterval(() => {
      loadAccounts().catch(() => null);
    }, 20000);
    return () => clearInterval(t);
  }, [loadAccounts]);

  useEffect(() => {
    loadList().catch((e) => showToast(e instanceof Error ? e.message : "Load failed"));
    const t = setInterval(() => {
      loadList({ quiet: true }).catch(() => null);
    }, 12000);
    return () => clearInterval(t);
  }, [loadList]);

  useEffect(() => {
    if (!selected) {
      setMessages([]);
      setSummary(null);
      setOutlineAt(null);
      setOutlineError("");
      setOutlineStale(false);
      setDealerSearchOpen(false);
      setSelectedDealerId(null);
      return;
    }
    loadMessages(selected).catch((e) => showToast(e instanceof Error ? e.message : "Messages failed"));
    enrichConversation(selected).catch(() => null);
    loadOutline(selected, false).catch(() => null);
  }, [selected]);

  useEffect(() => {
    try {
      localStorage.setItem(COL_STORAGE_KEY, JSON.stringify(cols));
    } catch {
      /* ignore */
    }
  }, [cols]);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const drag = dragRef.current;
      const el = workspaceRef.current;
      if (!drag || !el) return;
      const width = el.getBoundingClientRect().width || 1;
      const dxPct = ((e.clientX - drag.startX) / width) * 100;
      if (drag.which === "left") {
        let left = Math.min(40, Math.max(18, drag.start.left + dxPct));
        let mid = drag.start.mid - (left - drag.start.left);
        if (mid < 24) {
          left -= 24 - mid;
          mid = 24;
        }
        const right = 100 - left - mid;
        if (right < 22) return;
        setCols({ left, mid, right });
      } else {
        let right = Math.min(48, Math.max(22, drag.start.right - dxPct));
        let mid = drag.start.mid - (right - drag.start.right);
        if (mid < 24) {
          right -= 24 - mid;
          mid = 24;
        }
        const left = 100 - mid - right;
        if (left < 18) return;
        setCols({ left, mid, right });
      }
    };
    const onUp = () => {
      dragRef.current = null;
      document.body.classList.remove("wa-col-dragging");
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  function startColDrag(which: "left" | "right", e: ReactMouseEvent) {
    e.preventDefault();
    dragRef.current = { which, startX: e.clientX, start: { ...cols } };
    document.body.classList.add("wa-col-dragging");
  }

  async function enrichConversation(id: string) {
    const detail = await api<Conv>(`/api/whatsapp/conversations/${id}`);
    setItems((prev) => prev.map((x) => (x.id === id ? { ...x, ...detail } : x)));
  }

  function applySummaryPayload(res: {
    summary?: AiSummary | null;
    outline?: string;
    generated_at?: string | null;
    stale?: boolean;
  }) {
    if (res.summary && typeof res.summary === "object") {
      setSummary(res.summary);
    } else if (res.outline?.trim().startsWith("{")) {
      try {
        setSummary(JSON.parse(res.outline) as AiSummary);
      } catch {
        setSummary({ topic: res.outline || "" });
      }
    } else if (res.outline) {
      setSummary({ topic: res.outline, language: "legacy" });
    } else {
      setSummary(null);
    }
    setOutlineAt(res.generated_at || res.summary?.updated_at || null);
    setOutlineStale(Boolean(res.stale));
  }

  async function loadOutline(id: string, force: boolean) {
    setOutlineLoading(true);
    setOutlineError("");
    try {
      const cached = await api<{
        outline: string;
        summary?: AiSummary;
        stale: boolean;
        generated_at?: string | null;
      }>(`/api/whatsapp/conversations/${id}/outline`);
      if (cached.summary || cached.outline) {
        applySummaryPayload(cached);
      }
      if (cached.outline && !cached.stale && !force) {
        return;
      }
      const res = await api<{
        outline: string;
        summary?: AiSummary;
        generated_at?: string | null;
        stale?: boolean;
        cached?: boolean;
        error?: string;
      }>(`/api/whatsapp/conversations/${id}/outline?force=${force ? "true" : "false"}`, {
        method: "POST",
        body: "{}",
      });
      applySummaryPayload(res);
      if (res.error) setOutlineError(res.error);
    } catch (e) {
      setOutlineError(e instanceof Error ? e.message : "Summary failed");
    } finally {
      setOutlineLoading(false);
    }
  }

  async function searchDealers(query: string) {
    if (!selectedConv) return;
    setDealerSearching(true);
    try {
      const res = await api<{ items: DealerBrief[] }>(
        `/api/whatsapp/conversations/${selectedConv.id}/dealers/search?q=${encodeURIComponent(query)}&limit=12`,
      );
      setDealerHits(res.items || []);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Dealer search failed");
    } finally {
      setDealerSearching(false);
    }
  }

  async function linkSelectedDealer() {
    if (!selectedConv || !selectedDealerId) {
      showToast("Select a dealer first");
      return;
    }
    setLinkingDealer(true);
    try {
      const updated = await api<Conv>(`/api/whatsapp/conversations/${selectedConv.id}/link-dealer`, {
        method: "POST",
        body: JSON.stringify({ dealer_id: selectedDealerId }),
      });
      setItems((prev) => prev.map((x) => (x.id === selectedConv.id ? { ...x, ...updated } : x)));
      setDealerSearchOpen(false);
      setSelectedDealerId(null);
      showToast("Dealer linked");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Link failed");
    } finally {
      setLinkingDealer(false);
    }
  }

  async function saveContactEdit(e: FormEvent) {
    e.preventDefault();
    if (!selectedConv) return;
    try {
      const updated = await api<Conv>(`/api/whatsapp/conversations/${selectedConv.id}/contact`, {
        method: "PATCH",
        body: JSON.stringify({
          customer_name: editName,
          region: editRegion,
          language: editLanguage,
        }),
      });
      setItems((prev) => prev.map((x) => (x.id === selectedConv.id ? { ...x, ...updated } : x)));
      setEditOpen(false);
      showToast("Contact updated");
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Update failed");
    }
  }

  async function openConv(id: string) {
    setSelected(id);
    setDraft("");
    setMode("reply");
    setEmojiOpen(false);
    setAttachOpen(false);
    setEditOpen(false);
    setDealerSearchOpen(false);
    if (recording) stopRecording(false);
  }

  function insertEmoji(emoji: string) {
    setDraft((d) => d + emoji);
    setEmojiOpen(false);
    draftRef.current?.focus();
  }

  function blobToDataUrl(blob: Blob): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error("Failed to read file"));
      reader.readAsDataURL(blob);
    });
  }

  async function sendMedia(opts: { media_url: string; media_type: string; media_name: string; body?: string }) {
    if (!selectedConv) return;
    if (selectedConv.status === "Resolved") {
      showToast("Reopen conversation to reply");
      return;
    }
    if (careFreeformBlocked(selectedConv)) {
      showToast("Care 24h window expired — wait for customer reply or use a Meta template");
      return;
    }
    setSending(true);
    try {
      await api(`/api/whatsapp/conversations/${selectedConv.id}/messages`, {
        method: "POST",
        body: JSON.stringify({
          body: opts.body || "",
          media_url: opts.media_url,
          media_type: opts.media_type,
          media_name: opts.media_name,
          client_message_id: crypto.randomUUID(),
        }),
      });
      showToast(opts.media_type === "audio" ? "Voice note sent" : "Attachment sent");
      await loadMessages(selectedConv.id);
      await loadList();
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Send failed");
    } finally {
      setSending(false);
    }
  }

  async function onPickFile(file: File | null) {
    setAttachOpen(false);
    if (!file || !selectedConv) return;
    const media_type = file.type.startsWith("image/")
      ? "image"
      : file.type.startsWith("video/")
        ? "video"
        : file.type.startsWith("audio/")
          ? "audio"
          : "document";
    try {
      const dataUrl = await blobToDataUrl(file);
      await sendMedia({ media_url: dataUrl, media_type, media_name: file.name });
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Upload failed");
    }
  }

  async function startRecording() {
    if (!selectedConv || mode === "note" || selectedConv.status === "Resolved") {
      showToast(selectedConv?.status === "Resolved" ? "Reopen to send voice" : "Voice notes are for replies");
      return;
    }
    if (careFreeformBlocked(selectedConv)) {
      showToast("Care 24h window expired — wait for customer reply or use a Meta template");
      return;
    }
    setEmojiOpen(false);
    setAttachOpen(false);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : MediaRecorder.isTypeSupported("audio/webm")
          ? "audio/webm"
          : "";
      const recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
      chunksRef.current = [];
      recorder.ondataavailable = (ev) => {
        if (ev.data.size > 0) chunksRef.current.push(ev.data);
      };
      mediaRecorderRef.current = recorder;
      recorder.start(250);
      setRecording(true);
      setRecSecs(0);
      if (recTimerRef.current) window.clearInterval(recTimerRef.current);
      recTimerRef.current = window.setInterval(() => setRecSecs((s) => s + 1), 1000);
    } catch {
      showToast("Microphone permission denied");
    }
  }

  function stopRecording(sendIt: boolean) {
    const recorder = mediaRecorderRef.current;
    if (!recorder) {
      setRecording(false);
      return;
    }
    recorder.onstop = async () => {
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      if (recTimerRef.current) {
        window.clearInterval(recTimerRef.current);
        recTimerRef.current = null;
      }
      const blob = new Blob(chunksRef.current, { type: recorder.mimeType || "audio/webm" });
      chunksRef.current = [];
      mediaRecorderRef.current = null;
      setRecording(false);
      if (!sendIt || blob.size < 200) {
        showToast(sendIt ? "Recording too short" : "Recording cancelled");
        setRecSecs(0);
        return;
      }
      try {
        const dataUrl = await blobToDataUrl(blob);
        await sendMedia({
          media_url: dataUrl,
          media_type: "audio",
          media_name: `voice-${Date.now()}.webm`,
          body: "",
        });
      } catch (e) {
        showToast(e instanceof Error ? e.message : "Voice send failed");
      } finally {
        setRecSecs(0);
      }
    };
    if (recorder.state !== "inactive") recorder.stop();
    else setRecording(false);
  }

  async function send() {
    if (!selectedConv) return;
    if (recording) {
      stopRecording(true);
      return;
    }
    if (!draft.trim() && mode === "reply") return;
    if (selectedConv.status === "Resolved" && mode === "reply") {
      showToast("Reopen conversation to reply");
      return;
    }
    if (mode === "reply" && careFreeformBlocked(selectedConv)) {
      showToast("Care 24h window expired — wait for customer reply or use a Meta template");
      return;
    }
    setSending(true);
    setEmojiOpen(false);
    setAttachOpen(false);
    try {
      if (mode === "note") {
        await api(`/api/whatsapp/conversations/${selectedConv.id}/notes`, {
          method: "POST",
          body: JSON.stringify({ body: draft.trim() }),
        });
        showToast("Internal note added");
      } else {
        await api(`/api/whatsapp/conversations/${selectedConv.id}/messages`, {
          method: "POST",
          body: JSON.stringify({ body: draft.trim(), client_message_id: crypto.randomUUID() }),
        });
        showToast("Message sent");
      }
      setDraft("");
      await loadMessages(selectedConv.id);
      await loadList();
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Send failed");
    } finally {
      setSending(false);
    }
  }

  async function toggleResolve() {
    if (!selectedConv) return;
    try {
      if (selectedConv.status === "Resolved") {
        await api(`/api/whatsapp/conversations/${selectedConv.id}/reopen`, { method: "POST", body: "{}" });
        showToast("Conversation reopened");
      } else {
        await api(`/api/whatsapp/conversations/${selectedConv.id}/resolve`, {
          method: "POST",
          body: JSON.stringify({ reason: "Resolved from inbox" }),
        });
        showToast("Conversation resolved");
      }
      await loadList();
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Update failed");
    }
  }

  async function createChat(e: FormEvent) {
    e.preventDefault();
    const instance = accountId !== "all" ? accountId : accounts.find((a) => a.connection_status === "Connected")?.id || accounts[0]?.id;
    if (!instance) {
      showToast("No WhatsApp account available");
      return;
    }
    try {
      const created = await api<Conv>("/api/whatsapp/conversations", {
        method: "POST",
        body: JSON.stringify({
          instance_id: instance,
          customer_phone: newPhone,
          customer_name: newName,
          message: "",
        }),
      });
      setNewOpen(false);
      setNewPhone("");
      setNewName("");
      setTab("All");
      await loadList();
      setSelected(created.id);
      showToast("Conversation created");
    } catch (ex) {
      showToast(ex instanceof Error ? ex.message : "Create failed");
    }
  }

  async function testPing() {
    try {
      const created = await api<Conv>("/api/whatsapp/inbox/test-ping", { method: "POST", body: "{}" });
      showToast("Test message sent to connected WhatsApp");
      await loadList();
      setSelected(created.id);
      await loadMessages(created.id);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Test failed");
    }
  }

  const connectedCount = accounts.filter((a) => a.connection_status === "Connected").length;

  const gridStyle = profileOpen
    ? {
        gridTemplateColumns: `minmax(220px, ${cols.left}%) 5px minmax(280px, ${cols.mid}%) 5px minmax(280px, ${cols.right}%)`,
      }
    : {
        gridTemplateColumns: `minmax(220px, ${Math.min(42, cols.left + cols.right * 0.35)}%) 5px minmax(320px, 1fr)`,
      };

  return (
    <div className="wa-page">
      <section
        ref={workspaceRef}
        className={`wa-workspace ${profileOpen ? "" : "no-profile"}`}
        style={gridStyle}
      >
        <section className="wa-queue">
          <div className="wa-queuehead">
            <div className="wa-sectiontitle">
              Chats
              <span
                className={`wa-pill ${metrics.unread > 0 ? "" : "muted"}`}
                title="Unread inbound WhatsApp messages from phone"
              >
                {metrics.unread}
              </span>
              <div className="spacer" />
              <button
                type="button"
                className="wa-icon"
                title="Refresh"
                onClick={() => {
                  const care = accounts.find(isCareAccount);
                  const refresh = () => loadList({ bypassCache: true }).catch(() => null);
                  if (care && (accountId === "all" || accountId === care.id)) {
                    syncCareInbox().then(refresh).catch(refresh);
                  } else {
                    refresh();
                  }
                }}
              >
                ↻
              </button>
              <button type="button" className="wa-icon" title="Test send" disabled={!connectedCount} onClick={testPing}>⌁</button>
              <button type="button" className="wa-icon wa-icon-new" title="New chat" onClick={() => setNewOpen(true)}>＋</button>
            </div>
          </div>
          <div className="wa-account-inline" role="tablist" aria-label="WhatsApp accounts">
            <button
              type="button"
              role="tab"
              aria-selected={accountId === "all"}
              className={`wa-acc-chip ${accountId === "all" ? "on" : ""}`}
              onClick={() => selectAccount("all")}
            >
              All
              {metrics.unread > 0 ? <span className="wa-acc-unread">{metrics.unread}</span> : null}
            </button>
            {accounts.map((a) => {
              const on = accountConnected(a.connection_status);
              const unread = a.unread_count || 0;
              const isCare = isCareAccount(a);
              return (
                <button
                  key={a.id}
                  type="button"
                  role="tab"
                  aria-selected={accountId === a.id}
                  className={`wa-acc-chip ${accountId === a.id ? "on" : ""}`}
                  title={`${a.display_name} · ${a.connection_status}${isCare ? " · Cloud API" : ""}${unread ? ` · ${unread} unread` : ""}`}
                  onClick={() => {
                    selectAccount(a.id);
                    // Care Firestore sync in background — never block chip / list paint
                    if (isCare) {
                      const careId = a.id;
                      void syncCareInbox()
                        .then(() => {
                          if (accountIdRef.current !== careId) return;
                          return loadList({ bypassCache: true });
                        })
                        .catch(() => null);
                    }
                  }}
                >
                  <span className={`wa-acc-dot ${on ? "on" : "off"}`} aria-hidden />
                  <span className="wa-acc-name">{a.display_name}</span>
                  {unread > 0 ? <span className="wa-acc-unread">{unread}</span> : null}
                </button>
              );
            })}
          </div>
          <div className="wa-search">
            <SearchIcon size={15} />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search name, group, number, message" />
          </div>
          <div className="wa-tabs">
            {TABS.map((t) => (
              <button key={t} type="button" className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{t}</button>
            ))}
          </div>
          <div className="wa-sort">
            <select value={sort} onChange={(e) => setSort(e.target.value)}>
              <option value="recent">Most recent</option>
              <option value="oldest">Oldest first</option>
              <option value="unread">Unread first</option>
            </select>
          </div>
          <div className={`wa-threads${listRefreshing ? " is-refreshing" : ""}`}>
            {loading && !items.length && <div className="wa-empty">Loading…</div>}
            {!loading && !items.length && !listRefreshing && (
              <div className="wa-empty">
                No conversations yet. Click <b>Test send</b> or <b>New chat</b>, or wait for an inbound WhatsApp message.
              </div>
            )}
            {items.map((t) => {
              const careBadge = careWindowBadge(t);
              return (
              <button key={t.id} type="button" className={`wa-thread ${selected === t.id ? "selected" : ""}`} onClick={() => openConv(t.id)}>
                <WaDp photo={t.customer_photo} initial={t.initial} color={t.color} size={42} />
                <div className="wa-threadinfo">
                  <div className="wa-threadline">
                    <b>{t.display_name || t.customer_phone}</b>
                    <time>{fmtTime(t.last_message_at)}</time>
                  </div>
                  <div className="wa-snippet">{t.last_message_preview || "—"}</div>
                  <div className="wa-threadmeta">
                    {chatKindBadge(t) && <span className="wa-tag group">{chatKindBadge(t)}</span>}
                    {careBadge ? (
                      <span className={`wa-tag ${careBadge.className}`}>{careBadge.label}</span>
                    ) : (
                      <span className={`wa-tag ${tagClass(t.tag)}`}>{t.tag || t.status}</span>
                    )}
                    <span className="wa-thread-agent">
                      · {t.assigned_agent ? t.assigned_agent.split(" ")[0] : (t.account || "—")}
                    </span>
                    {t.unread_count > 0 && <span className="wa-unread">{t.unread_count}</span>}
                  </div>
                </div>
              </button>
              );
            })}
          </div>
        </section>

        <div
          className="wa-col-resizer"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize conversation list"
          onMouseDown={(e) => startColDrag("left", e)}
        />

        <section className="wa-chat">
          {!selectedConv ? (
            <div className="wa-chat-empty">
              <div className="wa-chat-empty-icon">💬</div>
              <h3>YesWeigh WhatsApp</h3>
              <p>Select a conversation to start messaging.<br />Messages stay private between you and the customer.</p>
            </div>
          ) : (
            <>
              <div className="wa-chathead">
                <WaDp photo={selectedConv.customer_photo} initial={selectedConv.initial} color={selectedConv.color} size={36} />
                <div className="wa-chathead-info">
                  <h2>
                    {selectedConv.display_name || selectedConv.customer_phone}
                    {chatKindBadge(selectedConv) && (
                      <span className="wa-tag group" style={{ marginLeft: 8, verticalAlign: "middle" }}>
                        {chatKindBadge(selectedConv)}
                      </span>
                    )}
                  </h2>
                  <div className="wa-sub">
                    {selectedConv.is_group || selectedConv.is_channel
                      ? `${selectedConv.chat_kind === "channel" || selectedConv.is_channel ? "Channel" : "Group"} · via ${selectedConv.account || "WhatsApp"}`
                      : `${selectedConv.customer_phone} · via ${selectedConv.account || "WhatsApp"}`}
                  </div>
                </div>
                <div className="wa-chathead-actions">
                  <button type="button" className="wa-icon" title="Search" aria-label="Search">⌕</button>
                  <button type="button" className="wa-icon" title="Customer panel" onClick={() => setProfileOpen((v) => !v)}>☰</button>
                  <button type="button" className="wa-icon" title={selectedConv.status === "Resolved" ? "Reopen" : "Resolve"} onClick={toggleResolve}>✓</button>
                </div>
              </div>
              <div className="wa-assign">
                <span>Owner</span>
                <strong className="wa-owner-name">{selectedConv.assigned_agent || selectedConv.account || "—"}</strong>
                <span className={`wa-tag ${selectedConv.status === "Resolved" ? "good" : "new"}`}>{selectedConv.status}</span>
                {selectedCareBadge && (
                  <span
                    className={`wa-tag ${selectedCareBadge.className}`}
                    title={
                      selectedConv.care_window_expires_at
                        ? `Customer care window · expires ${fmtTime(selectedConv.care_window_expires_at)}`
                        : "Meta Cloud 24h customer care window"
                    }
                  >
                    {selectedCareBadge.label}
                  </span>
                )}
                <div className="spacer" />
                <button type="button" className={`wa-mode-chip ${mode === "reply" ? "on" : ""}`} onClick={() => setMode("reply")}>Reply</button>
                <button type="button" className={`wa-mode-chip ${mode === "note" ? "on" : ""}`} onClick={() => setMode("note")}>Note</button>
              </div>
              <div className="wa-messages">
                <div className="wa-date-chip">Today</div>
                {!messages.length ? (
                  <div className="wa-empty" style={{ padding: "24px 16px", textAlign: "center", opacity: 0.75 }}>
                    No messages in CRM yet. Requesting WhatsApp history…
                    <br />
                    Live messages appear instantly; older history depends on WhatsApp delivering peer sync.
                  </div>
                ) : null}
                {messages.map((m) => {
                  const ticks =
                    m.direction !== "out"
                      ? null
                      : m.status === "read"
                        ? "read"
                        : m.status === "delivered"
                          ? "delivered"
                          : m.status === "failed"
                            ? "failed"
                            : "sent";
                  return (
                    <div key={m.id} className={`wa-msg ${m.direction === "out" ? "out" : m.direction === "note" ? "note" : "in"}`}>
                      {((m.direction === "out" || m.direction === "note") ||
                        ((selectedConv?.is_group || selectedConv?.is_channel) && m.direction === "in")) &&
                      m.sender_name ? (
                        <div className="wa-sender">{m.sender_name}</div>
                      ) : null}
                      {(() => {
                        const kind = mediaKind(m);
                        const src = m.media_url || "";
                        const unsupported = isUnsupportedPlaceholder(m.body, m.media_type);
                        const showCaption = Boolean(m.body && !isMediaPlaceholder(m.body) && !unsupported);
                        return (
                      <div className={`wa-bubble ${kind === "image" || kind === "sticker" || kind === "audio" || kind === "video" ? "has-media" : ""}`}>
                        {m.direction === "note" && <div className="wa-note-label">Internal note</div>}
                        {unsupported && !src ? (
                          <div className="wa-bubble-text wa-unsupported">
                            Unsupported message
                            <div className="wa-unsupported-hint">WhatsApp Cloud API did not include this content</div>
                          </div>
                        ) : null}
                        {src && (kind === "image" || kind === "sticker") ? (
                          <button
                            type="button"
                            className="wa-media-frame"
                            onClick={() => setLightbox({ src, name: m.media_name || "Photo" })}
                            title="View photo"
                          >
                            <img src={src} alt={m.media_name || "Photo"} loading="lazy" />
                          </button>
                        ) : null}
                        {src && kind === "video" ? (
                          <video className="wa-media-frame" src={src} controls playsInline preload="metadata" />
                        ) : null}
                        {src && kind === "audio" ? (
                          <div className="wa-voice">
                            <span className="wa-voice-mic" aria-hidden>🎤</span>
                            <audio className="wa-audio" src={src} controls preload="metadata" />
                          </div>
                        ) : null}
                        {src && kind === "audio" ? (
                          <div className="wa-transcript">
                            {m.transcript ? (
                              <p className="wa-transcript-text">{m.transcript}</p>
                            ) : /pending|processing/i.test(m.transcription_status || "") ? (
                              <p className="wa-transcript-status">Transcribing…</p>
                            ) : /failed/i.test(m.transcription_status || "") ? (
                              <div className="wa-transcript-failed">
                                <p className="wa-transcript-status">
                                  {m.transcription_error || "Could not transcribe"}
                                </p>
                                <button
                                  type="button"
                                  className="wa-transcript-retry"
                                  onClick={() => void retryTranscription(m.id)}
                                >
                                  Retry
                                </button>
                              </div>
                            ) : (
                              <button
                                type="button"
                                className="wa-transcript-retry"
                                onClick={() => void retryTranscription(m.id)}
                              >
                                Transcribe
                              </button>
                            )}
                          </div>
                        ) : null}
                        {src && kind === "document" ? (
                          <a className="wa-doc-card" href={src} target="_blank" rel="noreferrer">
                            <span className="wa-doc-ico">📄</span>
                            <span className="wa-doc-name">{m.media_name || "Document"}</span>
                          </a>
                        ) : null}
                        {!src && m.media_name && kind === "document" ? (
                          <div className="wa-attach">📎 {m.media_name}</div>
                        ) : null}
                        {showCaption ? <div className="wa-bubble-text">{m.body}</div> : null}
                        {!src && !showCaption && !unsupported ? (
                          <div className="wa-bubble-text wa-media-fallback">{mediaPlaceholderLabel(kind, m)}</div>
                        ) : null}
                        <div className="wa-bubble-meta">
                          <span>{fmtTime(m.created_at)}</span>
                          {m.direction === "out" && <WaTicks status={ticks || m.status} />}
                        </div>
                      </div>
                        );
                      })()}
                    </div>
                  );
                })}
                <div ref={messagesEndRef} />
              </div>
              {selectedCareBlocked && mode === "reply" && (
                <div className="wa-care-window-note">
                  24h customer care window expired — free-form reply disabled. Switch to Note, or wait for the customer to message (Meta template-only until then).
                </div>
              )}
              <div className={`wa-composer ${mode === "note" ? "notemode" : ""} ${recording ? "recording" : ""}`}>
                {emojiOpen && (
                  <div className="wa-emoji-panel">
                    {EMOJIS.map((e) => (
                      <button key={e} type="button" onClick={() => insertEmoji(e)}>{e}</button>
                    ))}
                  </div>
                )}
                {attachOpen && !(selectedCareBlocked && mode === "reply") && (
                  <div className="wa-attach-menu">
                    <button type="button" onClick={() => { setAttachOpen(false); fileInputRef.current?.click(); }}>
                      <span className="wa-attach-ico file">📄</span>Document
                    </button>
                    <button type="button" onClick={() => { setAttachOpen(false); fileInputRef.current?.click(); }}>
                      <span className="wa-attach-ico media">🖼️</span>Photos &amp; videos
                    </button>
                  </div>
                )}
                <input
                  ref={fileInputRef}
                  type="file"
                  hidden
                  accept="image/*,video/*,audio/*,.pdf,.doc,.docx,.xls,.xlsx,.txt"
                  onChange={(e) => onPickFile(e.target.files?.[0] || null)}
                />
                {!recording ? (
                  <>
                    <button
                      type="button"
                      className={`wa-icon ${emojiOpen ? "on" : ""}`}
                      title="Emoji"
                      aria-label="Emoji"
                      disabled={selectedCareBlocked && mode === "reply"}
                      onClick={() => { setEmojiOpen((v) => !v); setAttachOpen(false); }}
                    >☺</button>
                    <button
                      type="button"
                      className={`wa-icon ${attachOpen ? "on" : ""}`}
                      title="Attach"
                      aria-label="Attach"
                      disabled={selectedCareBlocked && mode === "reply"}
                      onClick={() => { setAttachOpen((v) => !v); setEmojiOpen(false); }}
                    >＋</button>
                    <div className="wa-input-wrap">
                      <textarea
                        ref={draftRef}
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        rows={1}
                        placeholder={
                          mode === "note"
                            ? "Add an internal note…"
                            : selectedCareBlocked
                              ? "Window expired — template-only / wait for customer"
                              : "Type a message"
                        }
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && !e.shiftKey) {
                            e.preventDefault();
                            send();
                          }
                        }}
                        disabled={
                          (selectedConv.status === "Resolved" && mode === "reply") ||
                          (selectedCareBlocked && mode === "reply")
                        }
                      />
                    </div>
                    {draft.trim() || mode === "note" ? (
                      <button
                        type="button"
                        className="wa-send"
                        disabled={sending || (selectedCareBlocked && mode === "reply")}
                        onClick={send}
                        title="Send"
                      >➤</button>
                    ) : (
                      <button
                        type="button"
                        className="wa-send"
                        disabled={sending || selectedCareBlocked}
                        onClick={startRecording}
                        title="Voice message"
                      >🎤</button>
                    )}
                  </>
                ) : (
                  <>
                    <button type="button" className="wa-icon danger" title="Cancel" onClick={() => stopRecording(false)}>✕</button>
                    <div className="wa-rec-bar">
                      <span className="wa-rec-dot" />
                      Recording… {Math.floor(recSecs / 60)}:{String(recSecs % 60).padStart(2, "0")}
                    </div>
                    <button type="button" className="wa-send" disabled={sending} onClick={() => stopRecording(true)} title="Send voice">➤</button>
                  </>
                )}
              </div>
            </>
          )}
        </section>

        {profileOpen ? (
          <>
            <div
              className="wa-col-resizer"
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize customer panel"
              onMouseDown={(e) => startColDrag("right", e)}
            />
            <RightPanel
              conv={selectedConv}
              messages={messages}
              summary={summary}
              outlineLoading={outlineLoading}
              outlineError={outlineError}
              outlineAt={outlineAt}
              outlineStale={outlineStale}
              onRegenerate={() => selected && loadOutline(selected, true)}
              WaDp={WaDp}
              dealerSearchOpen={dealerSearchOpen}
              setDealerSearchOpen={setDealerSearchOpen}
              dealerQ={dealerQ}
              setDealerQ={setDealerQ}
              dealerHits={dealerHits}
              dealerSearching={dealerSearching}
              selectedDealerId={selectedDealerId}
              setSelectedDealerId={setSelectedDealerId}
              linkingDealer={linkingDealer}
              searchDealers={searchDealers}
              linkSelectedDealer={linkSelectedDealer}
              onEditContact={() => {
                if (!selectedConv) return;
                setEditName(selectedConv.customer_name || selectedConv.display_name || "");
                setEditRegion(selectedConv.region || "");
                setEditLanguage(selectedConv.language || "");
                setEditOpen(true);
              }}
              onToast={showToast}
              onRefreshConv={async () => {
                if (!selected) return;
                await enrichConversation(selected);
                await loadMessages(selected);
              }}
              fmtTime={fmtTime}
            />
          </>
        ) : null}
      </section>

      {newOpen && (
        <div className="emp-overlay" onClick={(e) => { if (e.target === e.currentTarget) setNewOpen(false); }}>
          <section className="emp-drawer" style={{ maxWidth: 440, margin: "auto", height: "auto", borderRadius: 12 }}>
            <div className="emp-drawer-head">
              <div><h2>New conversation</h2><p>Start a WhatsApp chat from a connected account</p></div>
              <button type="button" className="iconbtn" onClick={() => setNewOpen(false)}>✕</button>
            </div>
            <form className="emp-formbody" onSubmit={createChat}>
              <div className="field"><label>Customer name</label><input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="Name" /></div>
              <div className="field"><label>WhatsApp number</label><input value={newPhone} onChange={(e) => setNewPhone(e.target.value)} placeholder="9198xxxxxxxx" required /></div>
              <div className="actions" style={{ justifyContent: "flex-end" }}>
                <button type="button" className="btn" onClick={() => setNewOpen(false)}>Cancel</button>
                <button type="submit" className="btn primary">Create chat</button>
              </div>
            </form>
          </section>
        </div>
      )}

      {editOpen && selectedConv && (
        <div className="emp-overlay" onClick={(e) => { if (e.target === e.currentTarget) setEditOpen(false); }}>
          <section className="emp-drawer" style={{ maxWidth: 440, margin: "auto", height: "auto", borderRadius: 12 }}>
            <div className="emp-drawer-head">
              <div><h2>Edit contact</h2><p>Update name, location, and language for this chat</p></div>
              <button type="button" className="iconbtn" onClick={() => setEditOpen(false)}>✕</button>
            </div>
            <form className="emp-formbody" onSubmit={saveContactEdit}>
              <div className="field"><label>Name</label><input value={editName} onChange={(e) => setEditName(e.target.value)} /></div>
              <div className="field"><label>Location</label><input value={editRegion} onChange={(e) => setEditRegion(e.target.value)} placeholder="City / region" /></div>
              <div className="field"><label>Language</label><input value={editLanguage} onChange={(e) => setEditLanguage(e.target.value)} placeholder="e.g. Malayalam" /></div>
              <div className="actions" style={{ justifyContent: "flex-end" }}>
                <button type="button" className="btn" onClick={() => setEditOpen(false)}>Cancel</button>
                <button type="submit" className="btn primary">Save</button>
              </div>
            </form>
          </section>
        </div>
      )}

      {lightbox && (
        <div
          className="wa-lightbox"
          role="dialog"
          aria-modal="true"
          aria-label={lightbox.name || "Photo"}
          onClick={(e) => { if (e.target === e.currentTarget) setLightbox(null); }}
        >
          <div className="wa-lightbox-panel" onClick={(e) => e.stopPropagation()}>
            <div className="wa-lightbox-bar">
              <span>{lightbox.name || "Photo"}</span>
              <button type="button" className="wa-lightbox-close" onClick={() => setLightbox(null)} aria-label="Close">
                Close ✕
              </button>
            </div>
            <div className="wa-lightbox-body">
              <img src={lightbox.src} alt={lightbox.name || "Photo"} className="wa-lightbox-img" />
            </div>
          </div>
        </div>
      )}

      {toast && <div className="emp-toast" role="status">{toast}</div>}
    </div>
  );
}
