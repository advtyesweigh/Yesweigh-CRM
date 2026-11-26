import { useCallback, useEffect, useRef, useState, Fragment } from "react";
import { api } from "../../api/client";
import { PaperclipIcon } from "../whatsapp/icons";
import WaMessageBubble from "../whatsapp/WaMessageBubble";
import {
  blobToDataUrl,
  careFreeformBlocked,
  fmtDayChip,
  istDayKey,
  type WaConvMeta,
  type WaMsg,
} from "../../lib/waMessageUtils";
import IntelAvatar from "./IntelAvatar";

const PAGE_LIMIT = 200;

const MOVE_STAGES = [
  { key: "enquiry", label: "Enquiry" },
  { key: "sales_order", label: "Sales order" },
  { key: "waiting", label: "Waiting" },
  { key: "paid", label: "Paid" },
  { key: "stamping", label: "Stamping" },
  { key: "service", label: "Service" },
  { key: "commitments", label: "Commitments" },
  { key: "closed", label: "Closed (dismiss)" },
] as const;

type Props = {
  threadId: string;
  name: string;
  phone: string;
  photo?: string;
  currentStage?: string;
  onClose: () => void;
  onStageMoved?: (toStage: string) => void;
};

export default function ChatPreviewModal({
  threadId,
  name,
  phone,
  photo,
  currentStage = "",
  onClose,
  onStageMoved,
}: Props) {
  const [msgs, setMsgs] = useState<WaMsg[] | null>(null);
  const [conv, setConv] = useState<WaConvMeta | null>(null);
  const [err, setErr] = useState("");
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [attachOpen, setAttachOpen] = useState(false);
  const [recording, setRecording] = useState(false);
  const [recSecs, setRecSecs] = useState(0);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [hasOlder, setHasOlder] = useState(false);
  const [toast, setToast] = useState("");
  const [lightbox, setLightbox] = useState<{ src: string; name: string } | null>(null);
  const [moveOpen, setMoveOpen] = useState(false);
  const [moving, setMoving] = useState(false);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const msgScrollRef = useRef<HTMLDivElement>(null);
  const sendInFlightRef = useRef(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const recTimerRef = useRef<number | null>(null);

  const careBlocked = careFreeformBlocked(conv);
  const resolved = conv?.status === "Resolved";

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    return () => {
      streamRef.current?.getTracks().forEach((t) => t.stop());
      if (recTimerRef.current) window.clearInterval(recTimerRef.current);
    };
  }, []);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(""), 3200);
    return () => window.clearTimeout(t);
  }, [toast]);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  const loadMessages = useCallback(
    async (opts?: { beforeId?: string; append?: boolean }) => {
      const q = new URLSearchParams({ limit: String(PAGE_LIMIT) });
      if (opts?.beforeId) q.set("before_id", opts.beforeId);
      const res = await api<{ items: WaMsg[] }>(`/api/whatsapp/conversations/${threadId}/messages?${q}`);
      const items = res.items || [];
      if (opts?.append) {
        setMsgs((prev) => (prev ? [...items, ...prev] : items));
        setHasOlder(items.length >= PAGE_LIMIT);
      } else {
        setMsgs(items);
        setHasOlder(items.length >= PAGE_LIMIT);
        window.requestAnimationFrame(() => scrollToBottom());
      }
    },
    [threadId, scrollToBottom],
  );

  useEffect(() => {
    let cancel = false;
    setMsgs(null);
    setConv(null);
    setErr("");
    setDraft("");
    Promise.all([
      api<WaConvMeta>(`/api/whatsapp/conversations/${threadId}`),
      api<{ items: WaMsg[] }>(`/api/whatsapp/conversations/${threadId}/messages?limit=${PAGE_LIMIT}`),
    ])
      .then(([c, res]) => {
        if (cancel) return;
        setConv(c);
        const items = res.items || [];
        setMsgs(items);
        setHasOlder(items.length >= PAGE_LIMIT);
        window.requestAnimationFrame(() => scrollToBottom());
      })
      .catch((e) => {
        if (!cancel) setErr(e instanceof Error ? e.message : "Could not load chat");
      });
    return () => {
      cancel = true;
    };
  }, [threadId, scrollToBottom]);

  async function loadOlder() {
    if (!msgs?.length || loadingOlder) return;
    const el = msgScrollRef.current;
    const prevHeight = el?.scrollHeight ?? 0;
    setLoadingOlder(true);
    try {
      await loadMessages({ beforeId: msgs[0].id, append: true });
      window.requestAnimationFrame(() => {
        if (el) el.scrollTop = el.scrollHeight - prevHeight;
      });
    } catch (e) {
      setToast(e instanceof Error ? e.message : "Could not load older messages");
    } finally {
      setLoadingOlder(false);
    }
  }

  async function retryTranscription(messageId: string) {
    try {
      await api(`/api/whatsapp/messages/${messageId}/transcribe`, { method: "POST", body: "{}" });
      await loadMessages();
    } catch (e) {
      setToast(e instanceof Error ? e.message : "Transcription failed");
    }
  }

  async function sendMedia(opts: { media_url: string; media_type: string; media_name: string; body?: string }) {
    if (resolved) {
      setToast("Reopen conversation to reply");
      return;
    }
    if (careBlocked) {
      setToast("Care 24h window expired — wait for customer reply or use a Meta template");
      return;
    }
    setSending(true);
    try {
      await api(`/api/whatsapp/conversations/${threadId}/messages`, {
        method: "POST",
        body: JSON.stringify({
          body: opts.body || "",
          media_url: opts.media_url,
          media_type: opts.media_type,
          media_name: opts.media_name,
          client_message_id: crypto.randomUUID(),
        }),
      });
      setToast(opts.media_type === "audio" ? "Voice note sent" : "Attachment sent");
      await loadMessages();
      scrollToBottom();
    } catch (e) {
      setToast(e instanceof Error ? e.message : "Send failed");
    } finally {
      setSending(false);
    }
  }

  async function onPickFile(file: File | null) {
    setAttachOpen(false);
    if (!file) return;
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
      setToast(e instanceof Error ? e.message : "Upload failed");
    }
  }

  async function startRecording() {
    if (resolved) {
      setToast("Reopen to send voice");
      return;
    }
    if (careBlocked) {
      setToast("Care 24h window expired — wait for customer reply or use a Meta template");
      return;
    }
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
      setToast("Microphone permission denied");
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
        setToast(sendIt ? "Recording too short" : "Recording cancelled");
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
        setToast(e instanceof Error ? e.message : "Voice send failed");
      } finally {
        setRecSecs(0);
      }
    };
    if (recorder.state !== "inactive") recorder.stop();
    else setRecording(false);
  }

  async function send() {
    if (sendInFlightRef.current) return;
    if (recording) {
      stopRecording(true);
      return;
    }
    const text = draft.trim();
    if (!text) return;
    if (resolved) {
      setToast("Reopen conversation to reply");
      return;
    }
    if (careBlocked) {
      setToast("Care 24h window expired — wait for customer reply or use a Meta template");
      return;
    }
    sendInFlightRef.current = true;
    setDraft("");
    setSending(true);
    setAttachOpen(false);
    try {
      await api(`/api/whatsapp/conversations/${threadId}/messages`, {
        method: "POST",
        body: JSON.stringify({ body: text, client_message_id: crypto.randomUUID() }),
      });
      setToast("Message sent");
      await loadMessages();
      scrollToBottom();
    } catch (e) {
      setDraft(text);
      setToast(e instanceof Error ? e.message : "Send failed");
    } finally {
      sendInFlightRef.current = false;
      setSending(false);
    }
  }

  async function moveToStage(toStage: string) {
    if (moving) return;
    setMoving(true);
    setMoveOpen(false);
    try {
      const res = await api<{ to_label?: string; to_stage: string }>("/api/intel/stage-move", {
        method: "POST",
        body: JSON.stringify({
          conversation_id: threadId,
          to_stage: toStage,
          from_stage: currentStage || "",
        }),
      });
      setToast(
        toStage === "closed"
          ? "Closed — learning updated (exclude patterns saved)"
          : `Saved — learning updated · ${res.to_label || toStage}`,
      );
      onStageMoved?.(toStage);
      if (toStage === "closed") {
        window.setTimeout(() => onClose(), 450);
      }
    } catch (e) {
      setToast(e instanceof Error ? e.message : "Move failed");
    } finally {
      setMoving(false);
    }
  }

  return (
    <div className="intel-modal-back" onClick={onClose} role="presentation">
      <div
        className="intel-modal intel-modal-chat"
        role="dialog"
        aria-modal="true"
        aria-label={name || "WhatsApp chat"}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="intel-modal-head">
          <div className="intel-modal-head-main">
            <IntelAvatar name={name || "Chat"} photo={photo} className="intel-avatar-modal" />
            <div>
              <strong>{name || "Chat"}</strong>
              <span>{phone || "WhatsApp conversation"}</span>
            </div>
          </div>
          <div className="intel-modal-head-actions">
            <div className="intel-move-wrap">
              <button
                type="button"
                className="intel-move-btn"
                disabled={moving}
                aria-expanded={moveOpen}
                onClick={() => setMoveOpen((v) => !v)}
              >
                {moving ? "Moving…" : "Move to…"}
              </button>
              {moveOpen ? (
                <div className="intel-move-menu" role="menu">
                  {MOVE_STAGES.map((s) => (
                    <button
                      key={s.key}
                      type="button"
                      role="menuitem"
                      className={s.key === "closed" ? "is-closed" : undefined}
                      onClick={() => void moveToStage(s.key)}
                    >
                      {s.label}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
            <button type="button" className="intel-modal-x" onClick={onClose} aria-label="Close">
              ×
            </button>
          </div>
        </header>

        <div ref={msgScrollRef} className="wa-messages intel-modal-msgs intel-modal-wa-bg">
          {err ? <p className="intel-modal-status">{err}</p> : null}
          {!err && msgs === null ? <p className="intel-modal-status">Loading chat…</p> : null}
          {msgs && msgs.length === 0 ? <p className="intel-modal-status">No messages in this chat yet.</p> : null}
          {hasOlder && msgs && msgs.length > 0 ? (
            <div className="intel-modal-load-older">
              <button type="button" disabled={loadingOlder} onClick={() => void loadOlder()}>
                {loadingOlder ? "Loading…" : "Load older messages"}
              </button>
            </div>
          ) : null}
          {(msgs || []).map((m, i) => {
            const dayKey = istDayKey(m.created_at);
            const prevDayKey = i > 0 ? istDayKey(msgs![i - 1]?.created_at) : "";
            const showDayChip = Boolean(dayKey && dayKey !== prevDayKey);
            return (
              <Fragment key={m.id}>
                {showDayChip ? <div className="wa-date-chip">{fmtDayChip(m.created_at)}</div> : null}
                <WaMessageBubble
                  m={m}
                  onImageClick={(src, n) => setLightbox({ src, name: n })}
                  onRetryTranscribe={(id) => void retryTranscription(id)}
                />
              </Fragment>
            );
          })}
          <div ref={messagesEndRef} />
        </div>

        {careBlocked ? (
          <div className="wa-care-window-note intel-modal-care-note">
            24h customer care window expired — free-form reply disabled until the customer messages again.
          </div>
        ) : null}

        <div className={`wa-composer intel-modal-composer ${recording ? "recording" : ""}`}>
          {attachOpen && !careBlocked ? (
            <div className="wa-attach-menu">
              <button
                type="button"
                onClick={() => {
                  setAttachOpen(false);
                  fileInputRef.current?.click();
                }}
              >
                <span className="wa-attach-ico file">📄</span>Document
              </button>
              <button
                type="button"
                onClick={() => {
                  setAttachOpen(false);
                  fileInputRef.current?.click();
                }}
              >
                <span className="wa-attach-ico media">🖼️</span>Photos &amp; videos
              </button>
            </div>
          ) : null}
          <input
            ref={fileInputRef}
            type="file"
            hidden
            accept="image/*,video/*,audio/*,.pdf,.doc,.docx,.xls,.xlsx,.txt"
            onChange={(e) => void onPickFile(e.target.files?.[0] || null)}
          />
          {!recording ? (
            <>
              <button
                type="button"
                className={`wa-icon ${attachOpen ? "on" : ""}`}
                title="Attach"
                aria-label="Attach"
                disabled={careBlocked || resolved}
                onClick={() => setAttachOpen((v) => !v)}
              >
                <PaperclipIcon size={17} />
              </button>
              <div className="wa-input-wrap">
                <textarea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  rows={1}
                  placeholder={careBlocked ? "Window expired — wait for customer" : resolved ? "Conversation resolved" : "Type a message"}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      if (sendInFlightRef.current || sending || !draft.trim()) return;
                      void send();
                    }
                  }}
                  disabled={careBlocked || resolved}
                />
              </div>
              {draft.trim() ? (
                <button
                  type="button"
                  className="wa-send"
                  disabled={sending || careBlocked || resolved}
                  onClick={() => void send()}
                  title="Send"
                >
                  ➤
                </button>
              ) : (
                <button
                  type="button"
                  className="wa-send"
                  disabled={sending || careBlocked || resolved}
                  onClick={() => void startRecording()}
                  title="Voice message"
                >
                  🎤
                </button>
              )}
            </>
          ) : (
            <>
              <button type="button" className="wa-icon danger" title="Cancel" onClick={() => stopRecording(false)}>
                ✕
              </button>
              <div className="wa-rec-bar">
                <span className="wa-rec-dot" />
                Recording… {Math.floor(recSecs / 60)}:{String(recSecs % 60).padStart(2, "0")}
              </div>
              <button type="button" className="wa-send" disabled={sending} onClick={() => stopRecording(true)} title="Send voice">
                ➤
              </button>
            </>
          )}
        </div>

        {toast ? <div className="intel-modal-toast">{toast}</div> : null}
      </div>

      {lightbox ? (
        <div
          className="wa-lightbox wa-lightbox--scoped"
          role="dialog"
          aria-modal="true"
          aria-label={lightbox.name}
          onClick={(e) => {
            if (e.target === e.currentTarget) setLightbox(null);
          }}
        >
          <div className="wa-lightbox-panel" onClick={(e) => e.stopPropagation()}>
            <div className="wa-lightbox-bar">
              <span className="wa-lightbox-title">{lightbox.name}</span>
              <div className="wa-lightbox-actions">
                <button type="button" className="wa-lightbox-close" onClick={() => setLightbox(null)} aria-label="Close">
                  Close ✕
                </button>
              </div>
            </div>
            <div className="wa-lightbox-body">
              <img src={lightbox.src} alt={lightbox.name} className="wa-lightbox-img" />
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}