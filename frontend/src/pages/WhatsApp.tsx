import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";

type Account = {
  id: string;
  display_name: string;
  connection_status: string;
  connected_phone_number?: string;
};

type Conv = {
  id: string;
  display_name: string;
  customer_name: string;
  customer_phone: string;
  customer_photo?: string;
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
};

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
};

function mediaKind(m: Msg): "image" | "video" | "audio" | "document" | "sticker" | "" {
  const t = (m.media_type || "").toLowerCase();
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
  if (["📷 Photo", "🎥 Video", "🎤 Voice message", "📄 Document", "Sticker", "[media]"].includes(b)) return true;
  return /^(📷|🎥|🎤|📄)?\s*(Photo|Video|Voice message|Document|Sticker|\[media\])$/i.test(b);
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

type Metrics = { open: number; unread: number; unassigned: number; agents_online: number; agents_total: number };
type Agent = { id: number; name: string; department: string; open_conversations: number; initial: string; availability: string };

const TABS = ["All", "Mine", "Unassigned", "Unread", "Resolved"] as const;

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
  return "";
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
  const [agents, setAgents] = useState<Agent[]>([]);
  const [mode, setMode] = useState<"reply" | "note">("reply");
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [toast, setToast] = useState("");
  const [profileOpen, setProfileOpen] = useState(true);
  const [newOpen, setNewOpen] = useState(false);
  const [newPhone, setNewPhone] = useState("");
  const [newName, setNewName] = useState("");
  const [loading, setLoading] = useState(true);
  const [recording, setRecording] = useState(false);
  const [recSecs, setRecSecs] = useState(0);
  const [emojiOpen, setEmojiOpen] = useState(false);
  const [attachOpen, setAttachOpen] = useState(false);
  const [lightbox, setLightbox] = useState<{ src: string; name?: string } | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const recTimerRef = useRef<number | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const draftRef = useRef<HTMLTextAreaElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);

  const EMOJIS = ["😀","😁","😂","😊","😍","😘","😎","🤔","😅","😭","🙏","👍","👎","👏","🔥","❤️","💯","🎉","✅","❌","🤝","📍","📞","🧾","📦","💰"];

  const selectedConv = useMemo(() => items.find((c) => c.id === selected) || null, [items, selected]);

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

  const loadList = useCallback(async () => {
    const params = new URLSearchParams({
      tab,
      sort,
      q,
      limit: "80",
    });
    if (accountId !== "all") params.set("instance_id", accountId);
    try {
      const res = await api<{ items: Conv[]; metrics: Metrics }>(`/api/whatsapp/conversations?${params}`);
      setItems(res.items);
      setMetrics(res.metrics);
      // Fetch missing DPs in background (max 3, staggered — avatar can be slow)
      const missing = res.items.filter((c) => !c.customer_photo).slice(0, 3);
      missing.forEach((c, i) => {
        window.setTimeout(() => {
          api<{ customer_photo: string }>(`/api/whatsapp/conversations/${c.id}/avatar`, {
            method: "POST",
            body: "{}",
          })
            .then((r) => {
              if (!r.customer_photo) return;
              setItems((prev) => prev.map((x) => (x.id === c.id ? { ...x, customer_photo: r.customer_photo } : x)));
            })
            .catch(() => null);
        }, i * 1500);
      });
    } finally {
      setLoading(false);
    }
  }, [tab, sort, q, accountId]);

  async function loadMessages(id: string) {
    const res = await api<{ items: Msg[] }>(`/api/whatsapp/conversations/${id}/messages`);
    setMessages(res.items);
    await api(`/api/whatsapp/conversations/${id}/read`, { method: "POST", body: "{}" }).catch(() => null);
    // Refresh DP when opening a chat
    const conv = items.find((c) => c.id === id);
    if (conv && !conv.customer_photo) {
      api<{ customer_photo: string }>(`/api/whatsapp/conversations/${id}/avatar`, { method: "POST", body: "{}" })
        .then((r) => {
          if (!r.customer_photo) return;
          setItems((prev) => prev.map((x) => (x.id === id ? { ...x, customer_photo: r.customer_photo } : x)));
        })
        .catch(() => null);
    }
  }

  useEffect(() => {
    api<{ items: Account[] }>("/api/whatsapp/inbox/accounts")
      .then((r) => setAccounts(r.items))
      .catch((e) => showToast(e instanceof Error ? e.message : "Accounts failed"));
    api<{ items: Agent[] }>("/api/whatsapp/agents")
      .then((r) => setAgents(r.items))
      .catch(() => null);
  }, []);

  useEffect(() => {
    loadList().catch((e) => showToast(e instanceof Error ? e.message : "Load failed"));
    const t = setInterval(() => {
      loadList().catch(() => null);
    }, 12000);
    return () => clearInterval(t);
  }, [loadList]);

  useEffect(() => {
    if (!selected) {
      setMessages([]);
      return;
    }
    loadMessages(selected).catch((e) => showToast(e instanceof Error ? e.message : "Messages failed"));
  }, [selected]);

  async function openConv(id: string) {
    setSelected(id);
    setDraft("");
    setMode("reply");
    setEmojiOpen(false);
    setAttachOpen(false);
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

  async function assign(employeeId: string) {
    if (!selectedConv) return;
    try {
      await api(`/api/whatsapp/conversations/${selectedConv.id}/assign`, {
        method: "POST",
        body: JSON.stringify(
          employeeId === "me"
            ? { assign_to_me: true }
            : employeeId
              ? { employee_id: Number(employeeId) }
              : { employee_id: null }
        ),
      });
      showToast("Assignment updated");
      await loadList();
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Assign failed");
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

  async function createChat(e: React.FormEvent) {
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

  return (
    <div className="wa-page">
      <section className={`wa-workspace ${profileOpen ? "" : "no-profile"}`}>
        <section className="wa-queue">
          <div className="wa-queuehead">
            <div className="wa-sectiontitle">
              Chats
              <span className="wa-pill">{items.length || metrics.open}</span>
              <div className="spacer" />
              <button type="button" className="wa-icon" title="Refresh" onClick={() => loadList()}>↻</button>
              <button type="button" className="wa-icon" title="Test send" disabled={!connectedCount} onClick={testPing}>⌁</button>
              <button type="button" className="wa-icon wa-icon-new" title="New chat" onClick={() => setNewOpen(true)}>＋</button>
            </div>
          </div>
          <div className="wa-account">
            <span className={`dot ${connectedCount ? "" : "off"}`} />
            <select value={accountId} onChange={(e) => setAccountId(e.target.value)} aria-label="WhatsApp account">
              <option value="all">All authorized accounts</option>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>{a.display_name} · {a.connection_status}</option>
              ))}
            </select>
          </div>
          <div className="wa-search">
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search name, number, message" />
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
          <div className="wa-threads">
            {loading && <div className="wa-empty">Loading…</div>}
            {!loading && !items.length && (
              <div className="wa-empty">
                No conversations yet. Click <b>Test send</b> or <b>New chat</b>, or wait for an inbound WhatsApp message.
              </div>
            )}
            {items.map((t) => (
              <button key={t.id} type="button" className={`wa-thread ${selected === t.id ? "selected" : ""}`} onClick={() => openConv(t.id)}>
                <WaDp photo={t.customer_photo} initial={t.initial} color={t.color} size={49} />
                <div className="wa-threadinfo">
                  <div className="wa-threadline">
                    <b>{t.display_name || t.customer_phone}</b>
                    <time>{fmtTime(t.last_message_at)}</time>
                  </div>
                  <div className="wa-snippet">{t.last_message_preview || "—"}</div>
                  <div className="wa-threadmeta">
                    <span className={`wa-tag ${tagClass(t.tag)}`}>{t.tag || t.status}</span>
                    <span>· {t.assigned_agent ? t.assigned_agent.split(" ")[0] : "Unassigned"}</span>
                    {t.unread_count > 0 && <span className="wa-unread">{t.unread_count}</span>}
                  </div>
                </div>
              </button>
            ))}
          </div>
        </section>

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
                <WaDp photo={selectedConv.customer_photo} initial={selectedConv.initial} color={selectedConv.color} size={40} />
                <div className="wa-chathead-info">
                  <h2>{selectedConv.display_name || selectedConv.customer_phone}</h2>
                  <div className="wa-sub">{selectedConv.customer_phone} · via {selectedConv.account || "WhatsApp"}</div>
                </div>
                <div className="wa-chathead-actions">
                  <button type="button" className="wa-icon" title="Search" aria-label="Search">⌕</button>
                  <button type="button" className="wa-icon" title="Customer panel" onClick={() => setProfileOpen((v) => !v)}>☰</button>
                  <button type="button" className="wa-icon" title={selectedConv.status === "Resolved" ? "Reopen" : "Resolve"} onClick={toggleResolve}>✓</button>
                </div>
              </div>
              <div className="wa-assign">
                <span>Assigned to</span>
                <select
                  aria-label="Assign agent"
                  value=""
                  onChange={(e) => {
                    if (e.target.value) assign(e.target.value);
                  }}
                >
                  <option value="">{selectedConv.assigned_agent || "Unassigned"}</option>
                  <option value="me">Assign to me</option>
                  <option value="">Clear assignment</option>
                  {agents.map((a) => (
                    <option key={a.id} value={String(a.id)}>{a.name}</option>
                  ))}
                </select>
                <span className={`wa-tag ${selectedConv.status === "Resolved" ? "good" : "new"}`}>{selectedConv.status}</span>
                <div className="spacer" />
                <button type="button" className={`wa-mode-chip ${mode === "reply" ? "on" : ""}`} onClick={() => setMode("reply")}>Reply</button>
                <button type="button" className={`wa-mode-chip ${mode === "note" ? "on" : ""}`} onClick={() => setMode("note")}>Note</button>
              </div>
              <div className="wa-messages">
                <div className="wa-date-chip">Today</div>
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
                      {(m.direction === "out" || m.direction === "note") && m.sender_name ? (
                        <div className="wa-sender">{m.sender_name}</div>
                      ) : null}
                      {(() => {
                        const kind = mediaKind(m);
                        const src = m.media_url || "";
                        const showCaption = Boolean(m.body && !isMediaPlaceholder(m.body));
                        return (
                      <div className={`wa-bubble ${kind === "image" || kind === "sticker" || kind === "audio" || kind === "video" ? "has-media" : ""}`}>
                        {m.direction === "note" && <div className="wa-note-label">Internal note</div>}
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
                        {src && kind === "document" ? (
                          <a className="wa-doc-card" href={src} target="_blank" rel="noreferrer">
                            <span className="wa-doc-ico">📄</span>
                            <span className="wa-doc-name">{m.media_name || "Document"}</span>
                          </a>
                        ) : null}
                        {!src && m.media_name ? (
                          <div className="wa-attach">📎 {m.media_name}</div>
                        ) : null}
                        {showCaption ? <div className="wa-bubble-text">{m.body}</div> : null}
                        {!m.body && !src ? <div className="wa-bubble-text">[media]</div> : null}
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
              <div className={`wa-composer ${mode === "note" ? "notemode" : ""} ${recording ? "recording" : ""}`}>
                {emojiOpen && (
                  <div className="wa-emoji-panel">
                    {EMOJIS.map((e) => (
                      <button key={e} type="button" onClick={() => insertEmoji(e)}>{e}</button>
                    ))}
                  </div>
                )}
                {attachOpen && (
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
                      onClick={() => { setEmojiOpen((v) => !v); setAttachOpen(false); }}
                    >☺</button>
                    <button
                      type="button"
                      className={`wa-icon ${attachOpen ? "on" : ""}`}
                      title="Attach"
                      aria-label="Attach"
                      onClick={() => { setAttachOpen((v) => !v); setEmojiOpen(false); }}
                    >＋</button>
                    <div className="wa-input-wrap">
                      <textarea
                        ref={draftRef}
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        rows={1}
                        placeholder={mode === "note" ? "Add an internal note…" : "Type a message"}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && !e.shiftKey) {
                            e.preventDefault();
                            send();
                          }
                        }}
                        disabled={selectedConv.status === "Resolved" && mode === "reply"}
                      />
                    </div>
                    {draft.trim() || mode === "note" ? (
                      <button type="button" className="wa-send" disabled={sending} onClick={send} title="Send">➤</button>
                    ) : (
                      <button type="button" className="wa-send" disabled={sending} onClick={startRecording} title="Voice message">🎤</button>
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

        {profileOpen && (
          <aside className="wa-profile">
            <div className="wa-phead">Customer details</div>
            {selectedConv ? (
              <>
                <div className="wa-pblock">
                  <div className="wa-pcontact">
                    <WaDp photo={selectedConv.customer_photo} initial={selectedConv.initial} color={selectedConv.color} size={56} />
                    <div>
                      <b>{selectedConv.display_name}</b>
                      <p>{selectedConv.customer_phone}</p>
                      <span className="wa-tag">{selectedConv.customer_type || "Customer"}</span>
                    </div>
                  </div>
                </div>
                <div className="wa-pblock">
                  <h3>CONTACT</h3>
                  <div className="wa-fields">
                    <span>Location</span><span>{selectedConv.region || "—"}</span>
                    <span>Language</span><span>{selectedConv.language || "—"}</span>
                    <span>Account</span><span>{selectedConv.account || "—"}</span>
                    <span>Agent</span><span>{selectedConv.assigned_agent || "Unassigned"}</span>
                  </div>
                </div>
                <div className="wa-pblock">
                  <h3>TEAM</h3>
                  {agents.slice(0, 5).map((a) => (
                    <div key={a.id} className="wa-agentrow">
                      <div className="avatar" style={{ width: 25, height: 25, fontSize: 10 }}>{a.initial}</div>
                      <b>{a.name}</b>
                      <span className="wa-load">{a.open_conversations} active</span>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <div className="wa-empty">No conversation selected</div>
            )}
          </aside>
        )}
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
