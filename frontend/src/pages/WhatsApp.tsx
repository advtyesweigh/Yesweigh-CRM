import { useCallback, useEffect, useMemo, useRef, useState, Fragment, type FormEvent, type MouseEvent as ReactMouseEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, apiWithTimeout, formatApiError } from "../api/client";
import type { LeadCreatePrefill } from "./Leads";
import RightPanel from "../components/whatsapp/RightPanel";
import type { AiSummary } from "../components/whatsapp/AiSummaryCard";
import { PaperclipIcon, PlusIcon, SearchIcon, SparklesIcon, UserPlusIcon } from "../components/whatsapp/icons";
import {
  holdClockTitle,
  serviceCategoryClass,
  serviceCategoryLabel,
  servicePriorityClass,
  type ConvServiceMeta,
} from "../lib/whatsappServiceMeta";
import {
  fmtDayChip,
  fmtTime as fmtMsgTime,
  istDayKey,
  voiceSpokenLanguageLabel,
} from "../lib/waMessageUtils";

const COL_STORAGE_KEY = "wa_inbox_col_widths_v1";
/** Default ON — exclude @g.us groups from inbox list + counts. */
const HIDE_GROUPS_STORAGE_KEY = "wa_inbox_hide_groups_v1";
const DEFAULT_COLS = { left: 26, mid: 36, right: 38 };

function readHideGroupsPref(): boolean {
  try {
    const raw = localStorage.getItem(HIDE_GROUPS_STORAGE_KEY);
    if (raw === null) return true;
    return raw !== "0" && raw !== "false";
  } catch {
    return true;
  }
}
/** WhatsApp inbox list page size (backend allows up to 200). */
const LIST_PAGE_SIZE = 80;
const LIST_FAST_OPTS = { include_metrics: "false", lite: "true" } as const;

type Account = {
  id: string;
  pk?: number;
  display_name: string;
  connection_status: string;
  connected_phone_number?: string;
  unread_count?: number;
  unreplied_count?: number;
  reply_avg_label?: string | null;
  reply_office_avg_label?: string | null;
  reply_off_avg_label?: string | null;
  reply_count_7d?: number;
  provider?: string;
  sarvam_voice_id?: string;
  voice_tts_enabled?: boolean;
};

type CampaignReplyFilter = {
  uid: number;
  campaign_number?: number;
  replied_count: number;
  media_name?: string;
  audience_name?: string;
  agent_name?: string;
  hint?: string;
  label?: string;
  status?: string;
};

type DealerBrief = {
  id: number;
  name: string;
  company?: string;
  phone: string;
  code?: string;
  city?: string;
  state?: string;
  /** Lifetime tax-exclusive Zoho invoice total (merge-aware). */
  total_sales?: number;
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
  whatsapp_instance_id?: number;
  last_message_preview: string;
  last_message_at?: string | null;
  unread_count: number;
  language?: string;
  region?: string;
  customer_type?: string;
  dealer_id?: number | null;
  dealer?: DealerBrief | null;
  /** CRM match from linked dealer or phone → dealers / leads table */
  crm_contact_kind?: "dealer" | "lead" | null;
  onboarding_step?: string;
  onboarding_lead_id?: number | null;
  onboarding?: {
    step?: string;
    lead_id?: number | null;
    at?: string | null;
    ran?: boolean;
  } | null;
  ai_outline?: string;
  ai_outline_at?: string | null;
  /** Care Meta Cloud only — absent on Evolution personal lines */
  provider?: string;
  care_window?: "open" | "expired" | string;
  care_window_open?: boolean;
  can_freeform_reply?: boolean;
  last_inbound_at?: string | null;
  care_window_expires_at?: string | null;
} & ConvServiceMeta;

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

function careWindowExpired(c: Conv): boolean {
  if (!isCareConv(c) || !c.care_window) return false;
  if (c.care_window === "open" || c.care_window_open) return false;
  return true;
}

function CareExpiredIcon() {
  return (
    <span className="wa-care-expired-icon" title="Expired" role="img" aria-label="Expired">
      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2.5" />
        <path d="M12 7v5.2l3.2 1.8" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </span>
  );
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
  onPhotoClick,
}: {
  photo?: string;
  initial: string;
  color?: string;
  size?: number;
  className?: string;
  onPhotoClick?: () => void;
}) {
  const [broken, setBroken] = useState(false);
  const showImg = !!photo && !broken;
  const style = {
    width: size,
    height: size,
    background: showImg ? "#dfE5e7" : color || "#dfe5e7",
    fontSize: Math.max(12, Math.round(size * 0.34)),
  };
  const body = showImg ? (
    <img src={photo} alt="" onError={() => setBroken(true)} />
  ) : (
    <span>{initial || "?"}</span>
  );
  if (showImg && onPhotoClick) {
    return (
      <button
        type="button"
        className={`wa-dp wa-dp-clickable ${className}`}
        style={style}
        onClick={onPhotoClick}
        aria-label="View profile photo"
        title="View profile photo"
      >
        {body}
      </button>
    );
  }
  return (
    <div className={`wa-dp ${className}`} style={style}>
      {body}
    </div>
  );
}

function openContactPhotoLightbox(
  setLightbox: (v: { src: string; name?: string; scoped?: boolean } | null) => void,
  photo: string | undefined,
  name?: string,
) {
  const src = (photo || "").trim();
  if (!src) return;
  setLightbox({ src, name: name || "Profile photo", scoped: true });
}

type VisitingCardExtract = {
  business_name?: string;
  contact_persons?: string[];
  phones?: string[];
  address?: string;
  city?: string;
  district?: string;
  state?: string;
  pin?: string;
  tagline?: string;
  email?: string;
  website?: string;
  notes?: string;
  raw_text?: string;
  places_resolved?: boolean;
  places_query?: string;
  places_note?: string;
  lead_prefill?: LeadCreatePrefill;
};

function mediaPathForExtract(src: string): string {
  const raw = (src || "").trim();
  if (!raw) return "";
  try {
    if (raw.startsWith("http://") || raw.startsWith("https://")) {
      return new URL(raw).pathname;
    }
  } catch {
    /* ignore */
  }
  return raw.split("?")[0];
}

function WaLightboxOverlay({
  lightbox,
  onClose,
  onCreateLead,
  onToast,
}: {
  lightbox: { src: string; name?: string; scoped?: boolean };
  onClose: () => void;
  onCreateLead?: (prefill: LeadCreatePrefill) => void;
  onToast?: (msg: string) => void;
}) {
  const [extracting, setExtracting] = useState(false);
  const [extractError, setExtractError] = useState("");
  const [extracted, setExtracted] = useState<VisitingCardExtract | null>(null);
  const [panelOpen, setPanelOpen] = useState(false);
  const canCardTools = !lightbox.scoped;

  async function runExtract(): Promise<VisitingCardExtract | null> {
    const imageUrl = mediaPathForExtract(lightbox.src);
    if (!imageUrl.includes("/api/whatsapp/media/")) {
      setExtractError("Only WhatsApp chat photos can be read with AI");
      setPanelOpen(true);
      return null;
    }
    setExtracting(true);
    setExtractError("");
    try {
      const data = await apiWithTimeout<VisitingCardExtract>(
        "/api/whatsapp/visiting-card/extract",
        90000,
        {
          method: "POST",
          body: JSON.stringify({
            image_url: imageUrl,
            resolve_places: true,
            source: "Visiting Card",
          }),
        },
      );
      setExtracted(data);
      setPanelOpen(true);
      return data;
    } catch (e) {
      const msg =
        e instanceof Error
          ? e.message
          : formatApiError(0, "", "AI extract failed");
      setExtractError(msg);
      setPanelOpen(true);
      onToast?.(msg);
      return null;
    } finally {
      setExtracting(false);
    }
  }

  async function onAiClick() {
    if (extracting) return;
    if (extracted) {
      setPanelOpen(true);
      return;
    }
    await runExtract();
  }

  async function onCreateLeadClick() {
    if (!onCreateLead) return;
    let data = extracted;
    if (!data?.lead_prefill) {
      data = await runExtract();
    }
    const prefill = data?.lead_prefill;
    if (!prefill) {
      onToast?.("Could not extract card details for a lead");
      return;
    }
    onClose();
    onCreateLead(prefill);
  }

  const fields: { label: string; value: string }[] = extracted
    ? [
        { label: "Business", value: extracted.business_name || "" },
        {
          label: "Contacts",
          value: (extracted.contact_persons || []).filter(Boolean).join(", "),
        },
        {
          label: "Phones",
          value: (extracted.phones || []).filter(Boolean).join(", "),
        },
        { label: "Address", value: extracted.address || "" },
        { label: "City", value: extracted.city || "" },
        { label: "District", value: extracted.district || "" },
        { label: "State", value: extracted.state || "" },
        { label: "PIN", value: extracted.pin || "" },
        { label: "Tagline", value: extracted.tagline || "" },
        { label: "Email", value: extracted.email || "" },
        { label: "Website", value: extracted.website || "" },
      ].filter((f) => f.value)
    : [];

  const isDp = !!lightbox.scoped;

  return (
    <div
      className={`wa-lightbox${isDp ? " wa-lightbox--scoped wa-lightbox--dp" : ""}`}
      role="dialog"
      aria-modal="true"
      aria-label={lightbox.name || "Photo"}
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div
        className={`wa-lightbox-panel${isDp ? " wa-lightbox-panel--dp" : ""}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="wa-lightbox-bar">
          <span className="wa-lightbox-title">{lightbox.name || "Photo"}</span>
          <div className="wa-lightbox-actions">
            {canCardTools ? (
              <>
                <button
                  type="button"
                  className="wa-lightbox-ai"
                  onClick={() => void onAiClick()}
                  disabled={extracting}
                  title="Read visiting card with AI"
                  aria-label="AI read visiting card"
                >
                  <SparklesIcon size={15} />
                  <span>{extracting ? "Reading…" : extracted ? "AI result" : "AI"}</span>
                </button>
                <button
                  type="button"
                  className="wa-lightbox-lead"
                  onClick={() => void onCreateLeadClick()}
                  disabled={extracting}
                  title="Create lead from visiting card"
                  aria-label="Create lead from visiting card"
                >
                  <UserPlusIcon size={15} />
                  <span>Create Lead</span>
                </button>
              </>
            ) : null}
            <button type="button" className="wa-lightbox-close" onClick={onClose} aria-label="Close">
              Close ✕
            </button>
          </div>
        </div>
        <div className={`wa-lightbox-body${isDp ? " wa-lightbox-body--dp" : ""}${panelOpen && canCardTools ? " has-extract" : ""}`}>
          <img
            src={lightbox.src}
            alt={lightbox.name || "Photo"}
            className={`wa-lightbox-img${isDp ? " wa-lightbox-img--dp" : ""}`}
          />
          {panelOpen && canCardTools ? (
            <aside className="wa-card-extract" aria-live="polite">
              <div className="wa-card-extract-head">
                <strong>Card details</strong>
                <button
                  type="button"
                  className="wa-card-extract-hide"
                  onClick={() => setPanelOpen(false)}
                  aria-label="Hide extracted fields"
                >
                  Hide
                </button>
              </div>
              {extracting ? <p className="wa-card-extract-status">Reading card with Gemini…</p> : null}
              {extractError ? <p className="wa-card-extract-error">{extractError}</p> : null}
              {!extracting && !extractError && fields.length === 0 && extracted ? (
                <p className="wa-card-extract-status">No fields detected on this image.</p>
              ) : null}
              {fields.length ? (
                <dl className="wa-card-extract-fields">
                  {fields.map((f) => (
                    <div key={f.label} className="wa-card-extract-row">
                      <dt>{f.label}</dt>
                      <dd>{f.value}</dd>
                    </div>
                  ))}
                </dl>
              ) : null}
              {extracted?.places_resolved ? (
                <p className="wa-card-extract-places">
                  State/district filled via Google Places
                  {extracted.places_query ? ` (${extracted.places_query})` : ""}.
                </p>
              ) : null}
              {extracted && !extracted.places_resolved && extracted.places_note && extracted.places_note !== "skipped" && extracted.places_note !== "state and district already present" ? (
                <p className="wa-card-extract-places muted">Places: {extracted.places_note}</p>
              ) : null}
              <div className="wa-card-extract-actions">
                <button
                  type="button"
                  className="btn"
                  disabled={extracting}
                  onClick={() => {
                    setExtracted(null);
                    void runExtract();
                  }}
                >
                  Re-read
                </button>
                <button
                  type="button"
                  className="btn primary"
                  disabled={extracting || !extracted?.lead_prefill}
                  onClick={() => void onCreateLeadClick()}
                >
                  Create Lead
                </button>
              </div>
            </aside>
          ) : null}
        </div>
      </div>
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
  spoken_language?: string;
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

type ServiceCategoryCounts = { all: number; sales: number; service: number; cloud: number; stamping: number };

type Metrics = {
  open: number;
  unread: number;
  /** 1:1 chats where a human reply is still needed (groups excluded; short acks excluded). */
  unreplied?: number;
  unread_conversations?: number;
  unassigned: number;
  agents_online: number;
  agents_total: number;
  service_categories?: ServiceCategoryCounts;
};

const TABS = ["All", "Groups", "Mine", "Unassigned", "Unread", "Resolved"] as const;
const SERVICE_CHIPS = [
  { id: "", label: "All" },
  { id: "sales", label: "Sales" },
  { id: "service", label: "Service" },
  { id: "cloud", label: "Cloud" },
  { id: "stamping", label: "Stamping" },
] as const;

function serviceChipLabel(
  chip: (typeof SERVICE_CHIPS)[number],
  counts?: ServiceCategoryCounts
): string {
  if (!counts) return chip.label;
  if (chip.id === "") return `${chip.label} (${counts.all})`;
  if (chip.id === "sales") return `${chip.label} (${counts.sales})`;
  if (chip.id === "service") return `${chip.label} (${counts.service})`;
  if (chip.id === "cloud") return `${chip.label} (${counts.cloud})`;
  if (chip.id === "stamping") return `${chip.label} (${counts.stamping ?? 0})`;
  return chip.label;
}

/** Inbox list / timeline: time today, date otherwise (IST). Bubbles use fmtMsgTime. */
function fmtTime(v?: string | null) {
  if (!v) return "";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return "";
  const todayKey = istDayKey(new Date().toISOString());
  const sameDay = istDayKey(v) === todayKey;
  return sameDay
    ? d.toLocaleTimeString("en-IN", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: true,
        timeZone: "Asia/Kolkata",
      })
    : d.toLocaleDateString("en-IN", {
        day: "numeric",
        month: "short",
        timeZone: "Asia/Kolkata",
      });
}

function tagClass(t: string) {
  const v = (t || "").toLowerCase();
  if (v.includes("hot")) return "hot";
  if (v.includes("new") || v.includes("reopen")) return "new";
  if (v.includes("resolved") || v.includes("good")) return "good";
  if (v === "dealer") return "dealer";
  return "";
}

/** Dealer link / phone match wins over lead — same rule as backend crm_contact_kind. */
function ServiceInboxPills({
  c,
  compact,
  hideHold,
}: {
  c: Conv;
  compact?: boolean;
  /** Header puts hold timing on its own row. */
  hideHold?: boolean;
}) {
  const catLabel = serviceCategoryLabel(c.service_category);
  const prio = (c.service_priority || "").trim();
  const prioCls = servicePriorityClass(prio);
  const showHold = Boolean(c.hold_clock) && !hideHold;
  if (!catLabel && !prio && !showHold) return null;
  return (
  <span className={`wa-service-meta${compact ? " compact" : ""}`}>
    {catLabel ? (
      <span className={`wa-tag wa-svc-cat ${serviceCategoryClass(c.service_category)}`}>{catLabel}</span>
    ) : null}
    {prio ? <span className={`wa-prio ${prioCls}`}>{prio}</span> : null}
    {showHold ? (
      <span className="wa-hold-clock" title={holdClockTitle(c.hold_since)}>
        {compact ? `⏱ ${c.hold_clock}` : `On hold ${c.hold_clock}`}
      </span>
    ) : null}
  </span>
  );
}

function crmContactKindPill(c: Conv): "dealer" | "lead" | null {
  if (c.is_group || c.is_channel || c.chat_kind === "group" || c.chat_kind === "channel") return null;
  if (c.crm_contact_kind === "dealer" || c.dealer_id || c.dealer) return "dealer";
  if (c.crm_contact_kind === "lead") return "lead";
  return null;
}

function accountConnected(status?: string) {
  const s = (status || "").toLowerCase();
  return s === "connected" || s === "open" || s === "online";
}

function campaignRepliedLabel(count: number) {
  if (count === 1) return "1 replied";
  if (count > 0) return `${count} replied`;
  return "0 replied";
}

function CampaignFilterLabel({ c }: { c: CampaignReplyFilter }) {
  const hint = (c.hint || c.label || "").trim();
  const replied = c.replied_count || 0;
  return (
    <>
      {hint ? `${c.uid} — ${hint}` : `${c.uid}`}
      {" ("}
      <span className="wa-campaign-replied">{campaignRepliedLabel(replied)}</span>
      {")"}
    </>
  );
}

export default function WhatsApp() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountId, setAccountId] = useState("all");
  const [campaignUid, setCampaignUid] = useState<number | null>(null);
  const [campaignFilters, setCampaignFilters] = useState<CampaignReplyFilter[]>([]);
  const [campaignFilterOpen, setCampaignFilterOpen] = useState(false);
  const [tab, setTab] = useState<(typeof TABS)[number]>("All");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("recent");
  const [serviceChip, setServiceChip] = useState<(typeof SERVICE_CHIPS)[number]["id"]>("");
  const [items, setItems] = useState<Conv[]>([]);
  const [metrics, setMetrics] = useState<Metrics>({ open: 0, unread: 0, unassigned: 0, agents_online: 0, agents_total: 0 });
  const metricsRef = useRef(metrics);
  const [selected, setSelected] = useState<string | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [mode, setMode] = useState<"reply" | "note">("reply");
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [toast, setToast] = useState("");
  const [profileOpen, setProfileOpen] = useState(true);
  const [loading, setLoading] = useState(true);
  const [listLoadError, setListLoadError] = useState("");
  const [listRefreshing, setListRefreshing] = useState(false);
  const [listHasMore, setListHasMore] = useState(false);
  const [listTotal, setListTotal] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [recording, setRecording] = useState(false);
  const [recSecs, setRecSecs] = useState(0);
  const [attachOpen, setAttachOpen] = useState(false);
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const threadParam = (searchParams.get("thread") || "").trim();
  const [lightbox, setLightbox] = useState<{ src: string; name?: string; scoped?: boolean } | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [editName, setEditName] = useState("");
  const [editRegion, setEditRegion] = useState("");
  const [editLanguage, setEditLanguage] = useState("");
  const [spamModalOpen, setSpamModalOpen] = useState(false);
  const [spamConfirmed, setSpamConfirmed] = useState(false);
  const [spamSubmitting, setSpamSubmitting] = useState(false);
  const [hideGroups, setHideGroups] = useState(readHideGroupsPref);
  const hideGroupsRef = useRef(hideGroups);
  hideGroupsRef.current = hideGroups;
  const [dealerSearchOpen, setDealerSearchOpen] = useState(false);
  const [dealerQ, setDealerQ] = useState("");
  const [dealerHits, setDealerHits] = useState<DealerBrief[]>([]);
  const [dealerSearching, setDealerSearching] = useState(false);
  const [selectedDealerId, setSelectedDealerId] = useState<number | null>(null);
  const [linkingDealer, setLinkingDealer] = useState(false);
  const [delinkingDealer, setDelinkingDealer] = useState(false);
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
  const sendInFlightRef = useRef(false);
  const streamRef = useRef<MediaStream | null>(null);
  const historyRequestedRef = useRef<Set<string>>(new Set());
  const listCacheRef = useRef<Map<string, { items: Conv[]; metrics: Metrics; hasMore: boolean; total: number | null }>>(
    new Map(),
  );
  const itemsRef = useRef<Conv[]>([]);
  const threadsRef = useRef<HTMLDivElement | null>(null);
  const loadingMoreRef = useRef(false);
  const listAbortRef = useRef<AbortController | null>(null);
  const metricsAbortRef = useRef<AbortController | null>(null);
  const metricsGenRef = useRef(0);
  const prefetchGenRef = useRef(0);
  const listGenRef = useRef(0);
  const accountsRef = useRef<Account[]>([]);
  const msgAbortRef = useRef<AbortController | null>(null);
  const msgGenRef = useRef(0);
  const msgTimersRef = useRef<number[]>([]);
  const selectedRef = useRef<string | null>(null);
  const selectedConvCacheRef = useRef<Conv | null>(null);
  const avatarTimersRef = useRef<number[]>([]);
  const accountIdRef = useRef(accountId);
  const campaignFilterRef = useRef<HTMLDivElement | null>(null);
  accountIdRef.current = accountId;
  selectedRef.current = selected;
  itemsRef.current = items;
  loadingMoreRef.current = loadingMore;
  accountsRef.current = accounts;
  metricsRef.current = metrics;

  const selectedCampaignFilter = useMemo(
    () => (campaignUid == null ? null : campaignFilters.find((c) => c.uid === campaignUid) ?? null),
    [campaignUid, campaignFilters],
  );

  const selectedConv = useMemo(() => {
    const fromList = selected ? items.find((c) => c.id === selected) || null : null;
    if (fromList) {
      selectedConvCacheRef.current = fromList;
      return fromList;
    }
    // Keep middle/right pane mounted if a quiet list refresh briefly omits the open chat
    // (campaign filter / cache swap), so switching never flashes empty or sticks.
    if (selected && selectedConvCacheRef.current?.id === selected) {
      return selectedConvCacheRef.current;
    }
    return null;
  }, [items, selected]);
  const selectedCareExpired = useMemo(() => (selectedConv ? careWindowExpired(selectedConv) : false), [selectedConv]);
  const selectedCareBlocked = careFreeformBlocked(selectedConv);
  const selectedCloneAccount = useMemo(() => {
    if (!selectedConv?.whatsapp_instance_id) return null;
    return accounts.find((a) => a.pk === selectedConv.whatsapp_instance_id) || null;
  }, [accounts, selectedConv]);
  const canSendCloneVoice = Boolean(
    selectedCloneAccount?.voice_tts_enabled && selectedCloneAccount?.sarvam_voice_id,
  );

  function listCacheKey(
    acc: string,
    camp: number | null,
    t: string,
    s: string,
    query: string,
    svc: string,
    hide = hideGroupsRef.current,
  ) {
    return `${acc}|c${camp ?? "all"}|${t}|${s}|${query}|${svc}|hg${hide ? 1 : 0}`;
  }

  function accountPkForList(accId: string): number | null {
    if (accId === "all") return null;
    const a = accountsRef.current.find((x) => x.id === accId);
    return a?.pk ?? null;
  }

  function buildListQuery(
    accId: string,
    camp: number | null,
    t: string,
    s: string,
    query: string,
    svc: string,
    offset: number,
    limit: number,
    extra?: Record<string, string>,
    hide = hideGroupsRef.current,
  ) {
    const params = new URLSearchParams({
      tab: t,
      sort: s,
      q: query,
      limit: String(limit),
      offset: String(offset),
      hide_groups: hide ? "true" : "false",
      ...extra,
    });
    if (accId !== "all") params.set("instance_id", accId);
    if (camp != null) params.set("campaign_uid", String(camp));
    if (svc.startsWith("prio:")) params.set("service_priority", svc.slice(5));
    else if (svc) params.set("service_category", svc);
    return params;
  }

  function optimisticItemsForAccount(accId: string): Conv[] | null {
    if (accId === "all") return null;
    const pk = accountPkForList(accId);
    if (pk == null) return null;
    const allKey = listCacheKey("all", campaignUid, tab, sort, q, serviceChip);
    const allCached = listCacheRef.current.get(allKey);
    if (!allCached?.items.length) return null;
    const filtered = allCached.items.filter((c) => c.whatsapp_instance_id === pk);
    return filtered.length ? filtered : null;
  }

  function clearAvatarTimers() {
    for (const id of avatarTimersRef.current) window.clearTimeout(id);
    avatarTimersRef.current = [];
  }

  function clearMsgTimers() {
    for (const id of msgTimersRef.current) window.clearTimeout(id);
    msgTimersRef.current = [];
  }

  function selectAccount(nextId: string) {
    if (nextId === accountIdRef.current) return;
    selectedRef.current = null;
    selectedConvCacheRef.current = null;
    msgAbortRef.current?.abort();
    clearMsgTimers();
    msgGenRef.current += 1;
    setSelected(null);
    setAccountId(nextId);
  }

  function selectCampaignFilter(next: number | null) {
    selectedRef.current = null;
    selectedConvCacheRef.current = null;
    msgAbortRef.current?.abort();
    clearMsgTimers();
    msgGenRef.current += 1;
    setSelected(null);
    setCampaignUid(next);
    setCampaignFilterOpen(false);
  }

  useEffect(() => {
    if (!campaignFilterOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (campaignFilterRef.current && !campaignFilterRef.current.contains(e.target as Node)) {
        setCampaignFilterOpen(false);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [campaignFilterOpen]);

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
      const r = await api<{ items: Account[] }>("/api/whatsapp/inbox/accounts?lite=true");
      const next = sortAccountsCareLast(r.items || []);
      setAccounts((prev) => {
        if (
          prev.length === next.length &&
          prev.every(
            (p, i) =>
              p.id === next[i]?.id &&
              p.unreplied_count === next[i]?.unreplied_count &&
              p.connection_status === next[i]?.connection_status,
          )
        ) {
          return prev;
        }
        return next;
      });
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Accounts failed");
    }
  }, []);

  const loadCampaignFilters = useCallback(async () => {
    try {
      const r = await api<{ items: CampaignReplyFilter[] }>("/api/whatsapp/inbox/campaign-reply-filters");
      setCampaignFilters(r.items || []);
    } catch {
      /* optional — inbox still works without campaign filter */
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

  const loadInboxMetrics = useCallback(
    async (ctx: {
      accId: string;
      camp: number | null;
      t: string;
      s: string;
      query: string;
      svc: string;
      gen: number;
      cacheKey: string;
    }) => {
      metricsAbortRef.current?.abort();
      const ac = new AbortController();
      metricsAbortRef.current = ac;
      const mgen = ++metricsGenRef.current;
      const params = buildListQuery(
        ctx.accId,
        ctx.camp,
        ctx.t,
        ctx.s,
        ctx.query,
        ctx.svc,
        0,
        1,
        { metrics_only: "true" },
      );
      try {
        const res = await apiWithTimeout<{ metrics?: Metrics }>(
          `/api/whatsapp/conversations?${params}`,
          20000,
          { signal: ac.signal },
        );
        if (ac.signal.aborted || mgen !== metricsGenRef.current || ctx.gen !== listGenRef.current) return;
        if (!res.metrics) return;
        setMetrics(res.metrics);
        const cur = listCacheRef.current.get(ctx.cacheKey);
        if (cur) listCacheRef.current.set(ctx.cacheKey, { ...cur, metrics: res.metrics });
      } catch (e) {
        if (ac.signal.aborted || (e instanceof DOMException && e.name === "AbortError")) return;
        if (e instanceof Error && e.name === "AbortError") return;
      }
    },
    [],
  );

  const prefetchAccountListPages = useCallback(() => {
    const gen = ++prefetchGenRef.current;
    const accs = accountsRef.current;
    if (!accs.length) return;
    const stale = { tab, sort, q, serviceChip, campaignUid };
    let i = 0;
    const step = () => {
      if (gen !== prefetchGenRef.current) return;
      if (i >= accs.length) return;
      const a = accs[i++];
      const key = listCacheKey(a.id, stale.campaignUid, stale.tab, stale.sort, stale.q, stale.serviceChip);
      if (listCacheKey(accountIdRef.current, stale.campaignUid, stale.tab, stale.sort, stale.q, stale.serviceChip) === key) {
        window.setTimeout(step, 40);
        return;
      }
      if (listCacheRef.current.has(key)) {
        window.setTimeout(step, 40);
        return;
      }
      const params = buildListQuery(
        a.id,
        stale.campaignUid,
        stale.tab,
        stale.sort,
        stale.q,
        stale.serviceChip,
        0,
        LIST_PAGE_SIZE,
        {
          ...LIST_FAST_OPTS,
          include_total: "false",
        },
      );
      apiWithTimeout<{ items: Conv[]; has_more?: boolean }>(`/api/whatsapp/conversations?${params}`, 20000)
        .then((res) => {
          if (gen !== prefetchGenRef.current) return;
          if (!res.items?.length) return;
          const hasMore = res.has_more ?? res.items.length >= LIST_PAGE_SIZE;
          if (!listCacheRef.current.has(key)) {
            const allKey = listCacheKey("all", stale.campaignUid, stale.tab, stale.sort, stale.q, stale.serviceChip);
            const m = listCacheRef.current.get(allKey)?.metrics;
            listCacheRef.current.set(key, {
              items: res.items,
              metrics: m ?? { open: 0, unread: 0, unassigned: 0, agents_online: 0, agents_total: 0 },
              hasMore,
              total: null,
            });
          }
        })
        .catch(() => null)
        .finally(() => window.setTimeout(step, 120));
    };
    window.setTimeout(step, 800);
  }, [tab, sort, q, serviceChip, campaignUid, hideGroups]);

  const loadList = useCallback(async (opts?: { bypassCache?: boolean; quiet?: boolean; append?: boolean }) => {
    const append = !!opts?.append;
    const key = listCacheKey(accountId, campaignUid, tab, sort, q, serviceChip);
    const cached = listCacheRef.current.get(key);
    const quiet = !!opts?.quiet;

    const offset = append ? itemsRef.current.length : 0;
    const limit =
      append || !quiet
        ? LIST_PAGE_SIZE
        : Math.min(Math.max(itemsRef.current.length, LIST_PAGE_SIZE), 400);

    // Instant paint from memory when toggling chips / tabs
    if (cached && !opts?.bypassCache && !append) {
      setItems(cached.items);
      setMetrics(cached.metrics);
      setListHasMore(cached.hasMore);
      setListTotal(cached.total);
      setLoading(false);
      if (!quiet) setListRefreshing(true);
    } else if (!append) {
      const optimistic = !opts?.bypassCache ? optimisticItemsForAccount(accountId) : null;
      if (optimistic) {
        setItems(optimistic);
        setListHasMore(true);
        setLoading(false);
        if (!quiet) setListRefreshing(true);
      } else if (!quiet) {
        // Keep previous list visible; only first mount shows full Loading…
        setListRefreshing(true);
      }
    }

    if (!append) {
      listAbortRef.current?.abort();
      clearAvatarTimers();
    }
    const ac = new AbortController();
    if (!append) listAbortRef.current = ac;
    const gen = append ? listGenRef.current : ++listGenRef.current;

    const listExtra: Record<string, string> = {
      ...LIST_FAST_OPTS,
      include_total: "false",
    };
    const params = buildListQuery(accountId, campaignUid, tab, sort, q, serviceChip, offset, limit, listExtra);

    const fetchMetrics = !append && !quiet;

    try {
      if (!append) setListLoadError("");
      const res = await apiWithTimeout<{ items: Conv[]; metrics?: Metrics; has_more?: boolean; total?: number }>(
        `/api/whatsapp/conversations?${params}`,
        12000,
        { signal: ac.signal }
      );
      if (ac.signal.aborted || gen !== listGenRef.current) return;

      const hasMore =
        res.has_more ?? (res.items.length >= limit && (res.total == null || offset + res.items.length < res.total));

      const nextItems = append
        ? (() => {
            const seen = new Set(itemsRef.current.map((x) => x.id));
            return [...itemsRef.current, ...res.items.filter((x) => !seen.has(x.id))];
          })()
        : res.items;

      if (append) {
        setItems(nextItems);
        setListHasMore(hasMore);
        const cur = listCacheRef.current.get(key);
        if (cur) {
          listCacheRef.current.set(key, { ...cur, items: nextItems, hasMore });
        }
      } else {
        const total = res.total ?? null;
        const cachedMetrics = listCacheRef.current.get(key)?.metrics ?? metricsRef.current;
        const nextMetrics = res.metrics ?? cachedMetrics;
        listCacheRef.current.set(key, { items: nextItems, metrics: nextMetrics, hasMore, total });
        setItems(nextItems);
        if (res.metrics) setMetrics(res.metrics);
        setListHasMore(hasMore);
        setListTotal(total);
        if (fetchMetrics && !res.metrics) {
          void loadInboxMetrics({
            accId: accountId,
            camp: campaignUid,
            t: tab,
            s: sort,
            query: q,
            svc: serviceChip,
            gen,
            cacheKey: key,
          });
        }
      }

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
      if (gen === listGenRef.current && !append) {
        const msg = e instanceof Error ? e.message : "Could not load chats";
        setListLoadError(msg);
      }
      throw e;
    } finally {
      if (append) return;
      // Clear spinner for the active generation, or if nothing ever painted (aborted chain).
      if (gen === listGenRef.current || itemsRef.current.length === 0) {
        setLoading(false);
        setListRefreshing(false);
      }
    }
  }, [tab, sort, q, accountId, campaignUid, serviceChip, hideGroups, loadInboxMetrics]);

  const loadMoreList = useCallback(async () => {
    if (loadingMoreRef.current || !listHasMore) return;
    loadingMoreRef.current = true;
    setLoadingMore(true);
    try {
      await loadList({ append: true, bypassCache: true, quiet: true });
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Could not load more chats");
    } finally {
      loadingMoreRef.current = false;
      setLoadingMore(false);
    }
  }, [loadList, listHasMore]);

  const onThreadsScroll = useCallback(() => {
    const el = threadsRef.current;
    if (!el || loadingMoreRef.current || !listHasMore) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 140) {
      void loadMoreList();
    }
  }, [listHasMore, loadMoreList]);

  async function retryTranscription(messageId: string) {
    try {
      const res = await api<{
        ok?: boolean;
        status?: string;
        transcript?: string;
        transcription_error?: string;
        transcript_language?: string;
        spoken_language?: string;
        source_language_code?: string;
      }>(`/api/whatsapp/messages/${messageId}/transcribe`, { method: "POST", body: "{}" });
      setMessages((prev) =>
        prev.map((m) =>
          m.id === messageId
            ? {
                ...m,
                transcript: res.transcript || m.transcript || "",
                transcript_language: res.transcript_language || m.transcript_language || "",
                spoken_language:
                  res.spoken_language ||
                  res.source_language_code ||
                  m.spoken_language ||
                  "",
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

  function isActiveConv(id: string, gen?: number) {
    if (selectedRef.current !== id) return false;
    if (gen != null && gen !== msgGenRef.current) return false;
    return true;
  }

  async function loadMessages(id: string, gen: number = msgGenRef.current) {
    // Only abort in-flight fetch when this is a new selection generation.
    // Post-send refreshes reuse the current gen and should not cancel themselves.
    if (gen === msgGenRef.current) {
      msgAbortRef.current?.abort();
      const ac = new AbortController();
      msgAbortRef.current = ac;
    }
    const ac = msgAbortRef.current;
    try {
      const res = await api<{ items: Msg[] }>(`/api/whatsapp/conversations/${id}/messages`, {
        signal: ac?.signal,
      });
      if (!isActiveConv(id, gen)) return;
      setMessages(res.items);
      // Refresh outline if new messages/transcripts arrived (uses cache fingerprint).
      loadOutline(id, false, gen).catch(() => null);
      // Poll briefly while Sarvam STT / text→Malayalam translate is in flight.
      const pending = res.items.some((m) => /pending|processing/i.test(m.transcription_status || ""));
      if (pending) {
        const tid = window.setTimeout(() => {
          if (!isActiveConv(id, gen)) return;
          api<{ items: Msg[] }>(`/api/whatsapp/conversations/${id}/messages`)
            .then((again) => {
              if (!isActiveConv(id, gen)) return;
              setMessages(again.items);
              loadOutline(id, false, gen).catch(() => null);
            })
            .catch(() => null);
        }, 3500);
        msgTimersRef.current.push(tid);
      }
      // Do NOT mark read / clear unread / flip New→Open on open or message fetch.
      // Unread + New stay until staff successfully sends a reply (backend clear_conversation_unread).
      // Refresh DP / group-channel title when opening a chat
      const conv =
        selectedConvCacheRef.current?.id === id
          ? selectedConvCacheRef.current
          : items.find((c) => c.id === id);
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
            if (!isActiveConv(id, gen)) return;
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
            const tid = window.setTimeout(() => {
              if (!isActiveConv(id, gen)) return;
              api<{ items: Msg[] }>(`/api/whatsapp/conversations/${id}/messages`)
                .then((again) => {
                  if (!isActiveConv(id, gen)) return;
                  if (again.items.length) setMessages(again.items);
                })
                .catch(() => null);
              loadList().catch(() => null);
            }, 2500);
            msgTimersRef.current.push(tid);
          })
          .catch(() => null);
      }
    } catch (e) {
      if (ac?.signal.aborted || (e instanceof DOMException && e.name === "AbortError")) return;
      if (e instanceof Error && e.name === "AbortError") return;
      throw e;
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
    if (!accounts.length) return;
    const ric = window.requestIdleCallback ?? ((cb: () => void) => window.setTimeout(cb, 400));
    const id = ric(() => prefetchAccountListPages());
    return () => {
      prefetchGenRef.current += 1;
      if (window.cancelIdleCallback && typeof id === "number") window.cancelIdleCallback(id);
    };
  }, [accounts, prefetchAccountListPages]);

  useEffect(() => {
    loadCampaignFilters().catch(() => null);
    const t = setInterval(() => {
      loadCampaignFilters().catch(() => null);
    }, 30000);
    return () => clearInterval(t);
  }, [loadCampaignFilters]);

  useEffect(() => {
    if (campaignUid == null) return;
    if (campaignFilters.some((c) => c.uid === campaignUid)) return;
    // Keep selection while filters are still loading; clear only once we know it's gone.
    if (campaignFilters.length === 0) return;
    setCampaignUid(null);
  }, [campaignFilters, campaignUid]);

  useEffect(() => {
    if (!loading) return;
    const guard = window.setTimeout(() => {
      setLoading(false);
      setListRefreshing(false);
      setListLoadError((prev) => prev || "Chat list is taking longer than expected — try refresh.");
    }, 18000);
    return () => window.clearTimeout(guard);
  }, [loading]);

  useEffect(() => {
    loadList().catch((e) => showToast(e instanceof Error ? e.message : "Load failed"));
    const listPoll = setInterval(() => {
      loadList({ quiet: true }).catch(() => null);
    }, 12000);
    const metricsPoll = setInterval(() => {
      const key = listCacheKey(accountId, campaignUid, tab, sort, q, serviceChip);
      void loadInboxMetrics({
        accId: accountId,
        camp: campaignUid,
        t: tab,
        s: sort,
        query: q,
        svc: serviceChip,
        gen: listGenRef.current,
        cacheKey: key,
      });
    }, 25000);
    return () => {
      listAbortRef.current?.abort();
      clearInterval(listPoll);
      clearInterval(metricsPoll);
    };
  }, [loadList, loadInboxMetrics, accountId, campaignUid, tab, sort, q, serviceChip, hideGroups]);

  useEffect(() => {
    try {
      localStorage.setItem(HIDE_GROUPS_STORAGE_KEY, hideGroups ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, [hideGroups]);

  useEffect(() => {
    if (!selected) {
      msgAbortRef.current?.abort();
      clearMsgTimers();
      msgGenRef.current += 1;
      selectedConvCacheRef.current = null;
      setMessages([]);
      setSummary(null);
      setOutlineAt(null);
      setOutlineError("");
      setOutlineStale(false);
      setOutlineLoading(false);
      setDealerSearchOpen(false);
      setSelectedDealerId(null);
      return;
    }
    // Invalidate any prior in-flight message/outline work for a previous chat.
    msgAbortRef.current?.abort();
    clearMsgTimers();
    const gen = ++msgGenRef.current;
    loadMessages(selected, gen).catch((e) => {
      if (!isActiveConv(selected, gen)) return;
      showToast(e instanceof Error ? e.message : "Messages failed");
    });
    enrichConversation(selected, gen).catch(() => null);
    loadOutline(selected, false, gen).catch(() => null);
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

  async function enrichConversation(id: string, gen?: number) {
    const detail = await api<Conv>(`/api/whatsapp/conversations/${id}`);
    if (!isActiveConv(id, gen)) return;
    selectedConvCacheRef.current =
      selectedConvCacheRef.current?.id === id
        ? { ...selectedConvCacheRef.current, ...detail }
        : detail;
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

  async function loadOutline(id: string, force: boolean, gen?: number) {
    const activeGen = gen ?? msgGenRef.current;
    setOutlineLoading(true);
    setOutlineError("");
    try {
      const cached = await api<{
        outline: string;
        summary?: AiSummary;
        stale: boolean;
        generated_at?: string | null;
      }>(`/api/whatsapp/conversations/${id}/outline`);
      if (!isActiveConv(id, activeGen)) return;
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
      if (!isActiveConv(id, activeGen)) return;
      applySummaryPayload(res);
      if (res.error) setOutlineError(res.error);
    } catch (e) {
      if (!isActiveConv(id, activeGen)) return;
      setOutlineError(e instanceof Error ? e.message : "Summary failed");
    } finally {
      if (isActiveConv(id, activeGen)) setOutlineLoading(false);
    }
  }

  async function searchDealers(query: string) {
    if (!selectedConv) return;
    const q = query.trim();
    if (q.length < 2) {
      setDealerHits([]);
      return;
    }
    setDealerSearching(true);
    try {
      const res = await api<{ items: DealerBrief[] }>(
        `/api/whatsapp/conversations/${selectedConv.id}/dealers/search?q=${encodeURIComponent(q)}&limit=20`,
      );
      setDealerHits(res.items || []);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Dealer search failed");
    } finally {
      setDealerSearching(false);
    }
  }

  function openLinkDealerPanel() {
    setDealerSearchOpen(true);
    setDealerQ("");
    setDealerHits([]);
    setSelectedDealerId(null);
  }

  useEffect(() => {
    if (!dealerSearchOpen || !selectedConv) return;
    const q = dealerQ.trim();
    if (q.length < 2) {
      setDealerHits([]);
      return;
    }
    const timer = window.setTimeout(() => {
      searchDealers(q).catch(() => null);
    }, 280);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- debounce dealerQ only while panel open
  }, [dealerQ, dealerSearchOpen, selectedConv?.id]);

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

  async function delinkDealer() {
    if (!selectedConv?.dealer && !selectedConv?.dealer_id) {
      showToast("No dealer linked");
      return;
    }
    if (!window.confirm("Remove the linked dealer from this chat?")) return;
    setDelinkingDealer(true);
    try {
      const updated = await api<Conv>(`/api/whatsapp/conversations/${selectedConv.id}/unlink-dealer`, {
        method: "POST",
        body: "{}",
      });
      setItems((prev) => prev.map((x) => (x.id === selectedConv.id ? { ...x, ...updated } : x)));
      showToast("Dealer delinked");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Delink failed");
    } finally {
      setDelinkingDealer(false);
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

  function openConv(id: string) {
    if (id === selectedRef.current) {
      // Re-click same row: force a fresh message load (gen bump).
      msgAbortRef.current?.abort();
      clearMsgTimers();
      const gen = ++msgGenRef.current;
      loadMessages(id, gen).catch((e) => {
        if (!isActiveConv(id, gen)) return;
        showToast(e instanceof Error ? e.message : "Messages failed");
      });
      loadOutline(id, false, gen).catch(() => null);
      return;
    }
    const fromList = items.find((c) => c.id === id) || null;
    if (fromList) selectedConvCacheRef.current = fromList;
    // Sync refs before setState so in-flight callbacks cannot write to the prior chat.
    selectedRef.current = id;
    msgAbortRef.current?.abort();
    clearMsgTimers();
    msgGenRef.current += 1;
    // Drop stale pane immediately so previous chat media/summary never sticks.
    setMessages([]);
    setSummary(null);
    setOutlineAt(null);
    setOutlineError("");
    setOutlineStale(false);
    setOutlineLoading(false);
    setSelected(id);
    setDraft("");
    setMode("reply");
    setAttachOpen(false);
    setEditOpen(false);
    setDealerSearchOpen(false);
    if (recording) stopRecording(false);
  }

  useEffect(() => {
    const id = threadParam;
    if (!id) return;
    let cancelled = false;
    (async () => {
      try {
        const detail = await api<Conv>(`/api/whatsapp/conversations/${id}`);
        if (cancelled) return;
        selectedConvCacheRef.current = detail;
        setItems((prev) => (prev.some((x) => x.id === id) ? prev.map((x) => (x.id === id ? { ...x, ...detail } : x)) : [detail, ...prev]));
        openConv(id);
      } catch {
        /* thread may be outside this user's instances */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [threadParam]);

  function openCreateLeadFromChat() {
    if (!selectedConv) return;
    setAttachOpen(false);
    const name = selectedConv.customer_name?.trim() || selectedConv.display_name?.trim() || "";
    const notes = [
      summary?.topic ? `Topic: ${summary.topic}` : "",
      summary?.customer_intent ? `Intent: ${summary.customer_intent}` : "",
      summary?.recommended_action ? `Action: ${summary.recommended_action}` : "",
      summary?.product_interest?.length ? `Products: ${summary.product_interest.join(", ")}` : "",
      `WhatsApp chat: ${selectedConv.id}`,
    ]
      .filter(Boolean)
      .join("\n");
    const prefill: LeadCreatePrefill = {
      name,
      phone: selectedConv.customer_phone || "",
      city: selectedConv.region || "",
      source: "WhatsApp",
      notes,
    };
    if (name) prefill.contact_person = name;
    navigate("/leads", { state: { createLead: prefill } });
  }

  function blobToDataUrl(blob: Blob): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error("Failed to read file"));
      reader.readAsDataURL(blob);
    });
  }

  async function sendAsCloneVoice() {
    if (!selectedConv) return;
    const text = draft.trim();
    if (!text) {
      showToast("Type a message to speak with the clone");
      return;
    }
    if (!canSendCloneVoice) {
      showToast("Voice TTS off — enable in Settings → Voice Clones");
      return;
    }
    if (selectedConv.status === "Resolved") {
      showToast("Reopen conversation to reply");
      return;
    }
    if (careFreeformBlocked(selectedConv)) {
      showToast("Care 24h window expired — wait for customer reply or use a Meta template");
      return;
    }
    if (!window.confirm("Send this text as a cloned voice note to the customer?")) return;
    setSending(true);
    try {
      await api("/api/voice-clones/send-tts", {
        method: "POST",
        body: JSON.stringify({
          conversation_public_id: selectedConv.id,
          text,
          language_code: "ml-IN",
          confirm_send: true,
        }),
      });
      setDraft("");
      showToast("Clone voice note sent");
      await loadMessages(selectedConv.id);
      await loadList({ bypassCache: true, quiet: true });
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Clone voice send failed");
    } finally {
      setSending(false);
    }
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
    if (sendInFlightRef.current) return;
    if (recording) {
      stopRecording(true);
      return;
    }
    const text = draft.trim();
    if (!text && mode === "reply") return;
    if (selectedConv.status === "Resolved" && mode === "reply") {
      showToast("Reopen conversation to reply");
      return;
    }
    if (mode === "reply" && careFreeformBlocked(selectedConv)) {
      showToast("Care 24h window expired — wait for customer reply or use a Meta template");
      return;
    }
    sendInFlightRef.current = true;
    setDraft("");
    setSending(true);
    setAttachOpen(false);
    try {
      if (mode === "note") {
        await api(`/api/whatsapp/conversations/${selectedConv.id}/notes`, {
          method: "POST",
          body: JSON.stringify({ body: text }),
        });
        showToast("Internal note added");
      } else {
        await api(`/api/whatsapp/conversations/${selectedConv.id}/messages`, {
          method: "POST",
          body: JSON.stringify({ body: text, client_message_id: crypto.randomUUID() }),
        });
        showToast("Message sent");
      }
      await loadMessages(selectedConv.id);
      await loadList();
    } catch (e) {
      setDraft(text);
      showToast(e instanceof Error ? e.message : "Send failed");
    } finally {
      sendInFlightRef.current = false;
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

  function openSpamModal() {
    setSpamConfirmed(false);
    setSpamModalOpen(true);
  }

  function closeSpamModal() {
    if (spamSubmitting) return;
    setSpamModalOpen(false);
    setSpamConfirmed(false);
  }

  async function confirmMarkSpam() {
    if (!selectedConv || !spamConfirmed || spamSubmitting) return;
    setSpamSubmitting(true);
    const spamId = selectedConv.id;
    try {
      await api(`/api/whatsapp/conversations/${spamId}/spam`, {
        method: "POST",
        body: JSON.stringify({ confirmed: true }),
      });
      setSpamModalOpen(false);
      setSpamConfirmed(false);
      setSelected(null);
      setMessages([]);
      showToast("Marked as spam — hidden from inbox");
      await Promise.all([loadList(), loadAccounts()]);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Could not mark as spam");
    } finally {
      setSpamSubmitting(false);
    }
  }

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
              {(metrics.unreplied ?? 0) > 0 ? (
                <span
                  className="wa-chats-unreplied"
                  title="Chats needing reply (1:1 only — groups & short acks like Ok/ശരി excluded)"
                >
                  {metrics.unreplied}
                </span>
              ) : null}
              <button
                type="button"
                className="wa-reply-stats-toggle"
                title="Open reply stats & staff performance (charts — no chat logs)"
                onClick={() => navigate("/whatsapp/reply-stats?period=7d")}
              >
                ⏱ Reply stats
              </button>
              <span className="spacer" aria-hidden />
              <button
                type="button"
                className={`wa-reply-stats-toggle wa-hide-groups-toggle${hideGroups ? " on" : ""}`}
                title={
                  hideGroups
                    ? "Groups hidden from list and counts — click to show groups"
                    : "Click to hide group chats from list and counts"
                }
                aria-pressed={hideGroups}
                onClick={() => {
                  listCacheRef.current.clear();
                  setHideGroups((v) => !v);
                }}
              >
                Hide group
              </button>
            </div>
          </div>
          <div className="wa-account-inline" role="tablist" aria-label="WhatsApp accounts">
            <button
              type="button"
              role="tab"
              aria-selected={accountId === "all"}
              className={`wa-acc-chip ${accountId === "all" ? "on" : ""}`}
              title={
                (metrics.unreplied ?? 0) > 0
                  ? `${metrics.unreplied} chats needing reply (1:1, groups & short acks excluded)`
                  : "All WhatsApp accounts"
              }
              onClick={() => selectAccount("all")}
            >
              <span className="wa-acc-name">All</span>
              {(metrics.unreplied ?? 0) > 0 ? (
                <span className="wa-acc-unreplied" aria-label={`${metrics.unreplied} needing reply`}>
                  {metrics.unreplied}
                </span>
              ) : null}
            </button>
            {accounts.map((a) => {
              const on = accountConnected(a.connection_status);
              const unrepliedN = a.unreplied_count ?? 0;
              const isCare = isCareAccount(a);
              return (
                <button
                  key={a.id}
                  type="button"
                  role="tab"
                  aria-selected={accountId === a.id}
                  className={`wa-acc-chip ${accountId === a.id ? "on" : ""}`}
                  title={[
                    a.display_name,
                    a.connection_status,
                    isCare ? "Cloud API" : null,
                    unrepliedN ? `${unrepliedN} needing reply` : null,
                    a.reply_count_7d && (a.reply_count_7d ?? 0) > 0
                      ? `7d office ${a.reply_office_avg_label ?? "—"} · off ${a.reply_off_avg_label ?? "—"} (${a.reply_count_7d})`
                      : null,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                  onClick={() => {
                    selectAccount(a.id);
                    // Care Firestore sync in background — never block chip / list paint
                    if (isCare) {
                      const careId = a.id;
                      void syncCareInbox()
                        .then(() => {
                          if (accountIdRef.current !== careId) return;
                          return loadList({ quiet: true });
                        })
                        .catch(() => null);
                    }
                  }}
                >
                  <span className={`wa-acc-dot ${on ? "on" : "off"}`} aria-hidden />
                  <span className="wa-acc-name">{a.display_name}</span>
                  {unrepliedN > 0 ? (
                    <span className="wa-acc-unreplied" aria-label={`${unrepliedN} needing reply`}>
                      {unrepliedN}
                    </span>
                  ) : null}
                </button>
              );
            })}
          </div>
          <div
            className={`wa-campaign-filter${campaignFilterOpen ? " is-open" : ""}`}
            ref={campaignFilterRef}
            aria-label="Campaign replies"
          >
            <button
              type="button"
              className="wa-campaign-filter-btn"
              title="Show chats that replied after this campaign send (by campaign sender — not inbox owner). Owner badge on each row is the assigned WhatsApp account."
              aria-label="Campaign reply filter"
              aria-haspopup="listbox"
              aria-expanded={campaignFilterOpen}
              onClick={() => setCampaignFilterOpen((v) => !v)}
            >
              {selectedCampaignFilter ? <CampaignFilterLabel c={selectedCampaignFilter} /> : "All campaigns"}
            </button>
            {campaignFilterOpen ? (
              <div className="wa-campaign-filter-menu" role="listbox" aria-label="Campaign reply filter">
                <button
                  type="button"
                  role="option"
                  aria-selected={campaignUid == null}
                  className={campaignUid == null ? "on" : ""}
                  onClick={() => selectCampaignFilter(null)}
                >
                  All campaigns
                </button>
                {campaignFilters.map((c) => (
                  <button
                    key={c.uid}
                    type="button"
                    role="option"
                    aria-selected={campaignUid === c.uid}
                    className={campaignUid === c.uid ? "on" : ""}
                    onClick={() => selectCampaignFilter(c.uid)}
                  >
                    <CampaignFilterLabel c={c} />
                  </button>
                ))}
              </div>
            ) : null}
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
          <div className="wa-service-chips" aria-label="Sales, service, cloud, and stamping filters">
            {SERVICE_CHIPS.map((chip) => (
              <button
                key={chip.id || "all"}
                type="button"
                className={serviceChip === chip.id ? "on" : ""}
                onClick={() => setServiceChip(chip.id)}
              >
                {serviceChipLabel(chip, metrics.service_categories)}
              </button>
            ))}
          </div>
          <div className="wa-sort">
            <select value={sort} onChange={(e) => setSort(e.target.value)}>
              <option value="recent">Most recent</option>
              <option value="oldest">Oldest first</option>
              <option value="unread">Unread first</option>
            </select>
          </div>
          <div
            className={`wa-threads${listRefreshing ? " is-refreshing" : ""}`}
            ref={threadsRef}
            onScroll={onThreadsScroll}
          >
            {loading && !items.length && !listLoadError ? <div className="wa-empty">Loading…</div> : null}
            {!loading && !items.length && listLoadError ? <div className="wa-empty err">{listLoadError}</div> : null}
            {!loading && !items.length && !listRefreshing && (
              <div className="wa-empty">
                No conversations yet. Wait for an inbound WhatsApp message.
              </div>
            )}
            {items.map((t) => {
              const careExpired = careWindowExpired(t);
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
                    {careExpired ? (
                      <CareExpiredIcon />
                    ) : (
                      <span className={`wa-tag ${tagClass(t.tag)}`}>{t.tag || t.status}</span>
                    )}
                    <span className="wa-thread-agent">
                      · {t.assigned_agent ? t.assigned_agent.split(" ")[0] : (t.account || "—")}
                    </span>
                    {t.crm_contact_kind === "dealer" && (
                      <span className="wa-crm-kind dealer">· Dealer</span>
                    )}
                    {t.crm_contact_kind === "lead" && (
                      <span className="wa-crm-kind lead">· Lead</span>
                    )}
                    <ServiceInboxPills c={t} compact />
                    {t.unread_count > 0 && <span className="wa-unread">{t.unread_count}</span>}
                  </div>
                </div>
              </button>
              );
            })}
            {listHasMore ? (
              <div className="wa-list-more">
                {loadingMore ? (
                  <span className="wa-list-more-msg">Loading older chats…</span>
                ) : (
                  <button type="button" className="wa-list-more-btn" onClick={() => void loadMoreList()}>
                    Load more
                    {listTotal != null && listTotal > items.length ? ` (${items.length} of ${listTotal})` : ""}
                  </button>
                )}
              </div>
            ) : listTotal != null && listTotal > 0 && items.length >= listTotal ? (
              <div className="wa-list-more wa-list-more-end">
                <span className="wa-list-more-msg">All {listTotal} chats loaded</span>
              </div>
            ) : null}
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
                <WaDp
                  photo={selectedConv.customer_photo}
                  initial={selectedConv.initial}
                  color={selectedConv.color}
                  size={36}
                  onPhotoClick={
                    selectedConv.customer_photo
                      ? () => openContactPhotoLightbox(setLightbox, selectedConv.customer_photo, selectedConv.display_name)
                      : undefined
                  }
                />
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
                <div className="wa-assign-row wa-assign-meta">
                  <span className="wa-assign-owner">
                    <span className="wa-assign-label">Owner</span>
                    <strong className="wa-owner-name">{selectedConv.assigned_agent || selectedConv.account || "—"}</strong>
                  </span>
                  {(() => {
                    const kind = crmContactKindPill(selectedConv);
                    return kind ? (
                      <span className={`wa-tag ${kind}`}>{kind === "dealer" ? "Dealer" : "Lead"}</span>
                    ) : null;
                  })()}
                  <span className={`wa-tag ${selectedConv.status === "Resolved" ? "good" : "new"}`}>{selectedConv.status}</span>
                  {selectedCareExpired && <CareExpiredIcon />}
                  <ServiceInboxPills c={selectedConv} hideHold />
                </div>
                <div className="wa-assign-row wa-assign-actions">
                  {selectedConv.hold_clock ? (
                    <span className="wa-hold-clock" title={holdClockTitle(selectedConv.hold_since)}>
                      On hold {selectedConv.hold_clock}
                    </span>
                  ) : null}
                  <div className="spacer" />
                  <div className="wa-assign-btns">
                    <button type="button" className={`wa-mode-chip ${mode === "reply" ? "on" : ""}`} onClick={() => setMode("reply")}>Reply</button>
                    <button type="button" className={`wa-mode-chip ${mode === "note" ? "on" : ""}`} onClick={() => setMode("note")}>Note</button>
                    <button
                      type="button"
                      className="wa-mode-chip wa-spam-chip"
                      onClick={openSpamModal}
                      title="Hide this chat from the inbox and drop unread counts"
                    >
                      Mark as Spam
                    </button>
                  </div>
                </div>
              </div>
              <div className="wa-messages" key={selectedConv.id}>
                {!messages.length ? (
                  <div className="wa-empty" style={{ padding: "24px 16px", textAlign: "center", opacity: 0.75 }}>
                    No messages in CRM yet. Requesting WhatsApp history…
                    <br />
                    Live messages appear instantly; older history depends on WhatsApp delivering peer sync.
                  </div>
                ) : null}
                {messages.map((m, i) => {
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
                  const dayKey = istDayKey(m.created_at);
                  const prevDayKey = i > 0 ? istDayKey(messages[i - 1]?.created_at) : "";
                  const showDayChip = Boolean(dayKey && dayKey !== prevDayKey);
                  return (
                    <Fragment key={m.id}>
                      {showDayChip ? <div className="wa-date-chip">{fmtDayChip(m.created_at)}</div> : null}
                    <div className={`wa-msg ${m.direction === "out" ? "out" : m.direction === "note" ? "note" : "in"}`}>
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
                        const voiceLang = kind === "audio" ? voiceSpokenLanguageLabel(m.spoken_language) : "";
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
                            {voiceLang ? (
                              <span className="wa-voice-lang" title={`Spoken language: ${voiceLang}`}>
                                {voiceLang}
                              </span>
                            ) : null}
                          </div>
                        ) : null}
                        {src && (kind === "audio" || kind === "video") ? (
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
                        {kind !== "audio" && kind !== "video" && m.transcript && m.transcript !== m.body ? (
                          <div className="wa-transcript wa-translation">
                            <p className="wa-transcript-text">{m.transcript}</p>
                          </div>
                        ) : kind !== "audio" &&
                          kind !== "video" &&
                          showCaption &&
                          /pending|processing/i.test(m.transcription_status || "") ? (
                          <div className="wa-transcript wa-translation">
                            <p className="wa-transcript-status">Translating…</p>
                          </div>
                        ) : null}
                        {!src && !showCaption && !unsupported ? (
                          <div className="wa-bubble-text wa-media-fallback">{mediaPlaceholderLabel(kind, m)}</div>
                        ) : null}
                        <div className="wa-bubble-meta">
                          <span>{fmtMsgTime(m.created_at)}</span>
                          {m.direction === "out" && <WaTicks status={ticks || m.status} />}
                        </div>
                      </div>
                        );
                      })()}
                    </div>
                    </Fragment>
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
                      className="wa-icon wa-icon-lead"
                      title="Create as new lead"
                      aria-label="Create as new lead"
                      disabled={!selectedConv}
                      onClick={openCreateLeadFromChat}
                    >
                      <PlusIcon size={18} />
                    </button>
                    <button
                      type="button"
                      className={`wa-icon ${attachOpen ? "on" : ""}`}
                      title="Upload"
                      aria-label="Upload"
                      disabled={selectedCareBlocked && mode === "reply"}
                      onClick={() => setAttachOpen((v) => !v)}
                    >
                      <PaperclipIcon size={17} />
                    </button>
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
                            if (sendInFlightRef.current || sending) return;
                            if (!draft.trim() && mode === "reply") return;
                            void send();
                          }
                        }}
                        disabled={
                          (selectedConv.status === "Resolved" && mode === "reply") ||
                          (selectedCareBlocked && mode === "reply")
                        }
                      />
                    </div>
                    {draft.trim() || mode === "note" ? (
                      <>
                        {canSendCloneVoice && mode === "reply" ? (
                          <button
                            type="button"
                            className="wa-icon"
                            disabled={sending || selectedCareBlocked}
                            onClick={sendAsCloneVoice}
                            title="Send as voice with my clone"
                          >
                            🗣
                          </button>
                        ) : null}
                        <button
                          type="button"
                          className="wa-send"
                          disabled={sending || (selectedCareBlocked && mode === "reply")}
                          onClick={send}
                          title="Send"
                        >➤</button>
                      </>
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
          {lightbox?.scoped ? (
            <WaLightboxOverlay
              lightbox={lightbox}
              onClose={() => setLightbox(null)}
              onToast={showToast}
            />
          ) : null}
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
              delinkingDealer={delinkingDealer}
              openLinkDealerPanel={openLinkDealerPanel}
              linkSelectedDealer={linkSelectedDealer}
              delinkDealer={delinkDealer}
              onEditContact={() => {
                if (!selectedConv) return;
                setEditName(selectedConv.customer_name || selectedConv.display_name || "");
                setEditRegion(selectedConv.region || "");
                setEditLanguage(selectedConv.language || "");
                setEditOpen(true);
              }}
              onMarkSpam={openSpamModal}
              onToast={showToast}
              onRefreshConv={async () => {
                if (!selected) return;
                const gen = msgGenRef.current;
                await enrichConversation(selected, gen);
                await loadMessages(selected, gen);
              }}
              onUseAiReply={(text) => {
                setDraft(text);
                setMode("reply");
              }}
              fmtTime={fmtTime}
              onViewContactPhoto={(photo, name) => openContactPhotoLightbox(setLightbox, photo, name)}
            />
          </>
        ) : null}
      </section>

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

      {spamModalOpen && selectedConv && (
        <div
          className="emp-overlay"
          onClick={(e) => {
            if (e.target === e.currentTarget) closeSpamModal();
          }}
        >
          <section
            className="emp-drawer wa-spam-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="wa-spam-title"
            style={{ maxWidth: 420, margin: "auto", height: "auto", borderRadius: 12 }}
          >
            <div className="emp-drawer-head">
              <div>
                <h2 id="wa-spam-title">Mark as spam?</h2>
                <p>
                  This hides{" "}
                  <strong>{selectedConv.display_name || selectedConv.customer_phone}</strong> from the
                  inbox and lowers unreplied / unread counts. Messages are kept (recoverable in DB).
                </p>
              </div>
              <button type="button" className="iconbtn" onClick={closeSpamModal} disabled={spamSubmitting}>
                ✕
              </button>
            </div>
            <div className="emp-formbody">
              <label className="wa-spam-confirm">
                <input
                  type="checkbox"
                  checked={spamConfirmed}
                  onChange={(e) => setSpamConfirmed(e.target.checked)}
                  disabled={spamSubmitting}
                />
                <span>Confirm it is spam</span>
              </label>
              <div className="actions" style={{ justifyContent: "flex-end", marginTop: 16 }}>
                <button type="button" className="btn" onClick={closeSpamModal} disabled={spamSubmitting}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn danger"
                  disabled={!spamConfirmed || spamSubmitting}
                  onClick={() => void confirmMarkSpam()}
                >
                  {spamSubmitting ? "Marking…" : "Mark as Spam"}
                </button>
              </div>
            </div>
          </section>
        </div>
      )}

      {lightbox && !lightbox.scoped ? (
        <WaLightboxOverlay
          lightbox={lightbox}
          onClose={() => setLightbox(null)}
          onToast={showToast}
          onCreateLead={(prefill) => {
            navigate("/leads", { state: { createLead: prefill } });
          }}
        />
      ) : null}

      {toast && <div className="emp-toast" role="status">{toast}</div>}
    </div>
  );
}
