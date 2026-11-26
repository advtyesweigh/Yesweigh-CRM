export type WaMsg = {
  id: string;
  direction: "in" | "out" | "note" | string;
  body?: string;
  status?: string;
  sender_name?: string;
  created_at?: string;
  media_name?: string;
  media_url?: string;
  media_type?: string;
  is_voice?: boolean;
  transcript?: string;
  /** Display/transcript language after translate (often ml) — not the spoken source. */
  transcript_language?: string;
  /** Sarvam STT detected spoken language of the audio (e.g. hi, ta). */
  spoken_language?: string;
  transcription_status?: string;
  transcription_error?: string;
};

/** Human labels for spoken_language ISO codes. Unknown → empty (no wrong guess). */
const SPOKEN_LANGUAGE_LABELS: Record<string, string> = {
  hi: "Hindi",
  ml: "Malayalam",
  ta: "Tamil",
  te: "Telugu",
  kn: "Kannada",
  en: "English",
  mr: "Marathi",
  bn: "Bengali",
  gu: "Gujarati",
  pa: "Punjabi",
  od: "Odia",
  or: "Odia",
  ur: "Urdu",
  as: "Assamese",
};

/**
 * Label for the voice source-language pill.
 * Uses spoken_language only — never transcript_language (which may be Malayalam after translate).
 */
export function voiceSpokenLanguageLabel(spoken?: string | null): string {
  const raw = (spoken || "").trim().toLowerCase().replace(/_/g, "-");
  if (!raw || raw === "unknown" || raw === "auto" || raw === "null" || raw === "none" || raw === "mixed") {
    return "";
  }
  if (SPOKEN_LANGUAGE_LABELS[raw]) return SPOKEN_LANGUAGE_LABELS[raw];
  const base = raw.split("-")[0] || "";
  return SPOKEN_LANGUAGE_LABELS[base] || "";
}

export type WaConvMeta = {
  status?: string;
  provider?: string;
  care_window?: string;
  care_window_open?: boolean;
  can_freeform_reply?: boolean;
};

export function isCareConv(c: WaConvMeta | null | undefined): boolean {
  if (!c) return false;
  return (c.provider || "") === "meta_cloud" || !!c.care_window;
}

export function careFreeformBlocked(c: WaConvMeta | null | undefined): boolean {
  if (!c || !isCareConv(c)) return false;
  if (typeof c.can_freeform_reply === "boolean") return !c.can_freeform_reply;
  return c.care_window === "expired";
}

export function mediaKind(m: WaMsg): "image" | "video" | "audio" | "document" | "sticker" | "" {
  const t = (m.media_type || "").toLowerCase();
  if (t === "ptt" || t === "voice") return "audio";
  if (t === "image" || t === "sticker" || t === "video" || t === "audio" || t === "document") {
    return t as "image" | "video" | "audio" | "document" | "sticker";
  }
  const name = `${m.media_name || ""} ${m.media_url || ""}`.toLowerCase();
  if (/\.(jpe?g|png|gif|webp|bmp)(\?|$)/i.test(name)) return "image";
  if (/\.(mp4|webm|mov|m4v)(\?|$)/i.test(name)) return "video";
  if (/\.(ogg|opus|mp3|wav|m4a|webm)(\?|$)/i.test(name) || m.is_voice) return "audio";
  if (m.media_url || m.media_name) return "document";
  return "";
}

export function isMediaPlaceholder(body?: string) {
  if (!body) return true;
  const b = body.trim();
  if (["📷 Photo", "🎥 Video", "🎤 Voice message", "📄 Document", "Sticker", "[media]", "media", "Attachment"].includes(b)) {
    return true;
  }
  return /^(📷|🎥|🎤|📄)?\s*(Photo|Video|Voice message|Document|Sticker|\[media\]|Attachment)$/i.test(b);
}

export function isUnsupportedPlaceholder(body?: string, mediaType?: string) {
  const kind = (mediaType || "").toLowerCase();
  if (kind === "unsupported" || kind === "unknown") return true;
  const b = (body || "").trim().toLowerCase();
  return (
    b === "unsupported message" ||
    b === "whatsapp did not include this message." ||
    b === "unsupported" ||
    b === "unknown"
  );
}

export function mediaPlaceholderLabel(kind: string, m: WaMsg): string {
  if (kind === "audio") return "🎤 Voice message";
  if (kind === "image") return "📷 Photo";
  if (kind === "video") return "🎥 Video";
  if (kind === "sticker") return "Sticker";
  if (kind === "document") return m.media_name ? `📄 ${m.media_name}` : "📄 Document";
  if (m.body && isMediaPlaceholder(m.body) && m.body.trim() !== "[media]" && m.body.trim().toLowerCase() !== "media") {
    return m.body;
  }
  return "Attachment unavailable";
}

const IST = "Asia/Kolkata";

/** Calendar day key (YYYY-MM-DD) in IST — for day-separator grouping. */
export function istDayKey(v?: string | null): string {
  if (!v) return "";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return "";
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: IST,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(d);
}

/** Centered chat day chip: Today / Yesterday / 18 Nov / 18 Nov 2025. */
export function fmtDayChip(v?: string | null): string {
  if (!v) return "";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return "";
  const key = istDayKey(v);
  if (!key) return "";
  const todayKey = istDayKey(new Date().toISOString());
  if (key === todayKey) return "Today";
  // Yesterday = noon IST of today, minus one calendar day
  const yNoon = new Date(`${todayKey}T12:00:00+05:30`);
  yNoon.setUTCDate(yNoon.getUTCDate() - 1);
  const yesterdayKey = new Intl.DateTimeFormat("en-CA", {
    timeZone: IST,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(yNoon);
  if (key === yesterdayKey) return "Yesterday";
  const sameYear = key.slice(0, 4) === todayKey.slice(0, 4);
  return d.toLocaleDateString("en-IN", {
    timeZone: IST,
    day: "numeric",
    month: "short",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}

/** Message-bubble clock time in IST (always time — day chip carries the date). */
export function fmtTime(v?: string | null) {
  if (!v) return "";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
    timeZone: IST,
  });
}

export function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result || ""));
    r.onerror = () => reject(new Error("Could not read file"));
    r.readAsDataURL(blob);
  });
}