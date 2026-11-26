import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { flushSync } from "react-dom";
import { useSearchParams } from "react-router-dom";
import { api, formatApiError, mediaAssetUrl } from "../api/client";
import { LEAD_LANGUAGE_MARKETS, leadLanguageFilterDisplayName } from "../data/leadLanguageMarkets";

type TabId = "media" | "campaign";
type MediaKind = "video" | "image" | "document";

const MEDIA_PAGE_SIZE = 12;

type CampaignRunItem = {
  id: number;
  uid?: number | null;
  campaign_number?: number | null;
  campaign_id: number | null;
  batch_id: string;
  tool_name: string;
  agent_name: string;
  media_name: string;
  media_id: number | null;
  audience_name: string;
  total_count: number;
  success_count: number;
  failed_count: number;
  skipped_count: number;
  replied_count?: number;
  from_instance_public_id?: string;
  note: string;
  status?: string;
  total_hours?: number;
  occurred_at: string;
  date: string;
  time: string;
  created_at: string;
  estimated_end_at?: string;
  can_restart?: boolean;
};

type SendInstance = {
  public_id: string;
  display_name: string;
  employee_name: string;
  employee_code: string;
  connected_phone: string;
  connection_status: string;
  label: string;
};

function parseCampaignsTab(raw: string | null): TabId {
  return raw === "campaign" ? "campaign" : "media";
}

function MediaPager({
  page,
  pages,
  total,
  onPage,
  placement,
}: {
  page: number;
  pages: number;
  total: number;
  onPage: (n: number) => void;
  placement: "top" | "bottom";
}) {
  if (total <= MEDIA_PAGE_SIZE) return null;
  const nums = Array.from({ length: pages }, (_, i) => i);
  return (
    <div
      className={`ml-pager ml-pager--${placement}`}
      role="navigation"
      aria-label={placement === "top" ? "Media pagination (top)" : "Media pagination (bottom)"}
    >
      <span>
        {total} · {page + 1}/{pages}
      </span>
      <button type="button" disabled={page <= 0} onClick={() => onPage(page - 1)}>
        Prev
      </button>
      {nums.map((n) => (
        <button
          key={n}
          type="button"
          className={n === page ? "active" : ""}
          aria-current={n === page ? "page" : undefined}
          onClick={() => onPage(n)}
        >
          {n + 1}
        </button>
      ))}
      <button type="button" disabled={page >= pages - 1} onClick={() => onPage(page + 1)}>
        Next
      </button>
    </div>
  );
}

function runIsLive(status?: string): boolean {
  const s = (status || "completed").toLowerCase();
  return s === "running" || s === "queued";
}

function runCanRestart(run: CampaignRunItem): boolean {
  if (run.can_restart === true) return true;
  const s = (run.status || "").toLowerCase();
  return s === "failed" || s === "stopped";
}

function runStatusLabel(status?: string): string {
  const s = (status || "completed").toLowerCase();
  // Live runs use an icon next to the date — no text badge.
  if (s === "running" || s === "queued") return "";
  if (s === "failed") return "Failed";
  if (s === "stopped") return "Stopped";
  return "";
}

function runStatusClass(status?: string): string {
  const s = (status || "completed").toLowerCase();
  if (s === "failed") return "camp-run-badge is-failed";
  if (s === "stopped") return "camp-run-badge is-stopped";
  return "camp-run-badge";
}

function runPublicId(run: CampaignRunItem): number | null {
  const n = run.uid ?? run.campaign_number;
  if (n == null || !Number.isFinite(n)) return null;
  return Math.trunc(n);
}

function IconRunLive({ size = 14 }: { size?: number }) {
  return (
    <svg
      className="camp-run-live-svg"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.25"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <polygon points="6 4 20 12 6 20 6 4" fill="currentColor" stroke="none" />
    </svg>
  );
}

type MediaUsageCampaign = {
  run_id: number;
  campaign_number: number | null;
  campaign_id: number | null;
  status: string;
  details: string;
  note: string;
  total_hours: number;
  occurred_at: string;
  audience_name: string;
  audience_uid: number | null;
  audience_size: number;
  total_sent: number;
  failed_count: number;
  skipped_count: number;
  from_agent: string;
  from_number: string;
  from_instance_public_id: string;
};

type MediaUsage = {
  media_id: number;
  media_uid: number | null;
  media_title: string;
  total_sent: number;
  campaigns: MediaUsageCampaign[];
};

type MediaItem = {
  id: number;
  uid?: number | null;
  title: string;
  topic?: string;
  file_name: string;
  mime_type: string;
  media_kind?: MediaKind | string;
  tags?: string[];
  folder_id?: number | null;
  duration_seconds?: number;
  file_size: number;
  uploaded_by: number | null;
  uploaded_by_name: string;
  created_at: string;
  play_url: string;
  cover_url?: string;
  has_cover?: boolean;
  language?: string;
  /** Successful sends from campaign report (sum of Success for this media). */
  count?: number;
};

const KNOWN_MEDIA_LANG_CODES = new Set(LEAD_LANGUAGE_MARKETS.map((m) => m.code));

function mediaLanguageCode(m: MediaItem): string {
  return (m.language || "").trim().toLowerCase();
}

function mediaMatchesLanguageFilter(m: MediaItem, filter: string): boolean {
  if (!filter) return true;
  const code = mediaLanguageCode(m);
  if (!code) return false;
  return code === filter;
}

const ACCEPT_MEDIA =
  "video/mp4,video/webm,video/quicktime,video/ogg,.mp4,.webm,.mov,.ogg,.ogv,.avi,.mkv," +
  "image/jpeg,image/png,image/webp,image/gif,.jpg,.jpeg,.png,.webp,.gif," +
  "application/pdf,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt";

function mediaTopic(m: MediaItem): string {
  return (m.topic || m.title || m.file_name || "").trim();
}

function mediaKindOf(m: MediaItem): MediaKind {
  const k = (m.media_kind || "").toLowerCase();
  if (k === "image" || k === "document" || k === "video") return k;
  const mime = (m.mime_type || "").toLowerCase();
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("video/") || !mime) return "video";
  return "document";
}

function fmtBytes(n: number): string {
  if (!n || n < 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function fmtReportDate(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "Asia/Kolkata",
  });
}

function fmtReportTime(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
    timeZone: "Asia/Kolkata",
  });
}

/** Combined date + time for Campaign Report (e.g. `22 Nov 2026, 10:35 pm`). */
function fmtReportDateTime(iso: string): string {
  if (!iso) return "—";
  const date = fmtReportDate(iso);
  const time = fmtReportTime(iso);
  if (date === "—" && time === "—") return "—";
  if (date === "—") return time;
  if (time === "—") return date;
  return `${date}, ${time}`;
}

function fmtDdMmYy(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const parts = new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
    timeZone: "Asia/Kolkata",
  }).formatToParts(d);
  const dd = parts.find((p) => p.type === "day")?.value || "";
  const mm = parts.find((p) => p.type === "month")?.value || "";
  const yy = parts.find((p) => p.type === "year")?.value || "";
  return dd && mm && yy ? `${dd}/${mm}/${yy}` : "";
}

function fmtUid(uid?: number | null): string {
  if (uid == null || !Number.isFinite(uid)) return "";
  return String(Math.trunc(uid)).padStart(4, "0");
}

function fmtDuration(sec?: number): string {
  const s = Math.max(0, Math.floor(sec || 0));
  if (!s) return "";
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}

function docLabel(m: MediaItem): string {
  const ext = (m.file_name || "").split(".").pop()?.toUpperCase() || "DOC";
  if (ext === "PDF") return "PDF";
  if (["DOC", "DOCX"].includes(ext)) return "DOC";
  if (["XLS", "XLSX"].includes(ext)) return "XLS";
  if (["PPT", "PPTX"].includes(ext)) return "PPT";
  return ext.slice(0, 4);
}

function IconPlay({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M8 5v14l11-7L8 5z" />
    </svg>
  );
}
function IconPause({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M6 5h4v14H6V5zm8 0h4v14h-4V5z" />
    </svg>
  );
}
function IconStop({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <rect x="6" y="6" width="12" height="12" rx="1.5" />
    </svg>
  );
}
function IconMegaphone({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
      <path d="M3 11v2a4 4 0 0 0 4 4h1l5 3V4L8 7H7a4 4 0 0 0-4 4z" />
      <path d="M16 8.5a5 5 0 0 1 0 7" />
    </svg>
  );
}
function IconUpload({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
      <path d="M12 16V4" />
      <path d="M7 9l5-5 5 5" />
      <path d="M4 20h16" />
    </svg>
  );
}
function IconPlus({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" aria-hidden>
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}
function IconImage({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <circle cx="8.5" cy="10" r="1.5" />
      <path d="M21 16l-5-5-4 4-2-2-5 5" />
    </svg>
  );
}
function IconFile({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5z" />
      <path d="M14 3v5h5" />
    </svg>
  );
}
function IconEye({ size = 12 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinejoin="round"
      />
      <circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="2" />
    </svg>
  );
}

function IconPencil({ size = 12 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.25" aria-hidden>
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
    </svg>
  );
}
export default function Campaigns() {
  const [params, setParams] = useSearchParams();
  const tab = useMemo(() => parseCampaignsTab(params.get("tab")), [params]);
  const setTab = useCallback(
    (next: TabId) => {
      const p = new URLSearchParams(params);
      if (next === "media") p.delete("tab");
      else p.set("tab", next);
      setParams(p, { replace: true });
    },
    [params, setParams]
  );
  const [media, setMedia] = useState<MediaItem[]>([]);
  const [runs, setRuns] = useState<CampaignRunItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [runsLoading, setRunsLoading] = useState(false);
  const [err, setErr] = useState("");
  const [toast, setToast] = useState("");

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [mediaQuery, setMediaQuery] = useState("");
  const [languageFilter, setLanguageFilter] = useState("");
  const [mediaKindFilter, setMediaKindFilter] = useState<MediaKind>("video");
  const [mediaPage, setMediaPage] = useState(0);

  const [uploading, setUploading] = useState(false);
  const [uploadLanguage, setUploadLanguage] = useState("");
  const [uploadOpen, setUploadOpen] = useState(false);
  const [topic, setTopic] = useState("");
  const [topicErr, setTopicErr] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const cardVideoRef = useRef<HTMLVideoElement | null>(null);
  const mediaGridTopRef = useRef<HTMLDivElement | null>(null);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [playLoadingId, setPlayLoadingId] = useState<number | null>(null);

  const [usageOpen, setUsageOpen] = useState(false);
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageErr, setUsageErr] = useState("");
  const [usage, setUsage] = useState<MediaUsage | null>(null);

  const [editOpen, setEditOpen] = useState(false);
  const [editItem, setEditItem] = useState<MediaItem | null>(null);
  const [editTopic, setEditTopic] = useState("");
  const [editLanguage, setEditLanguage] = useState("");
  const [editSaving, setEditSaving] = useState(false);
  const [editErr, setEditErr] = useState("");

  const [actionBusyId, setActionBusyId] = useState<number | null>(null);
  const [resumeRun, setResumeRun] = useState<CampaignRunItem | null>(null);
  const [sendInstances, setSendInstances] = useState<SendInstance[]>([]);
  const [sendInstancesLoading, setSendInstancesLoading] = useState(false);
  const [resumeInstanceId, setResumeInstanceId] = useState("");
  const [resumeErr, setResumeErr] = useState("");
  const [resumeSaving, setResumeSaving] = useState(false);

  const showToast = useCallback((msg: string) => {
    setToast(msg);
    window.setTimeout(() => setToast(""), 3200);
  }, []);

  const loadMedia = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const m = await api<MediaItem[]>("/api/campaigns/media");
      setMedia(m || []);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load media");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadRuns = useCallback(async () => {
    setRunsLoading(true);
    try {
      const r = await api<CampaignRunItem[]>("/api/campaigns/runs");
      setRuns(r || []);
    } catch {
      setRuns([]);
    } finally {
      setRunsLoading(false);
    }
  }, []);

  const closeResumeModal = useCallback(() => {
    setResumeRun(null);
    setResumeInstanceId("");
    setResumeErr("");
    setSendInstances([]);
    setResumeSaving(false);
  }, []);

  const openResumeModal = useCallback(
    async (run: CampaignRunItem) => {
      setResumeRun(run);
      setResumeErr("");
      setResumeInstanceId("");
      setSendInstancesLoading(true);
      try {
        const items = await api<SendInstance[]>("/api/campaigns/send-instances");
        const current = (run.from_instance_public_id || "").trim();
        const list = (items || []).filter((i) => {
          const st = (i.connection_status || "").toLowerCase();
          if (st !== "connected") return false;
          // Prefer other numbers for "new instance"; keep current listed only if alone.
          return true;
        });
        setSendInstances(list);
        const preferred =
          list.find((i) => i.public_id !== current)?.public_id ||
          list.find((i) => i.public_id === current)?.public_id ||
          list[0]?.public_id ||
          "";
        setResumeInstanceId(preferred);
      } catch (e) {
        setResumeErr(e instanceof Error ? e.message : "Could not load WhatsApp instances");
        setSendInstances([]);
      } finally {
        setSendInstancesLoading(false);
      }
    },
    []
  );

  const onRestartRun = useCallback(
    async (run: CampaignRunItem) => {
      const publicId = runPublicId(run);
      const remaining = Math.max(
        0,
        (run.total_count || 0) - (run.success_count || 0) - (run.skipped_count || 0)
      );
      if (
        !window.confirm(
          `Restart campaign #${publicId ?? run.id} on ${run.agent_name || "same agent"}?\n\n` +
            `Keeps ${run.success_count} successful sends (no re-blast). ` +
            `Retries ~${remaining} remaining/failed recipients after WhatsApp reconnect.`
        )
      ) {
        return;
      }
      setActionBusyId(run.id);
      try {
        await api<CampaignRunItem>(`/api/campaigns/runs/${run.id}/restart`, {
          method: "POST",
          body: "{}",
        });
        showToast(`Campaign #${publicId ?? run.id} restarted`);
        await loadRuns();
      } catch (e) {
        showToast(e instanceof Error ? e.message : "Restart failed");
      } finally {
        setActionBusyId(null);
      }
    },
    [loadRuns, showToast]
  );

  const onConfirmResume = useCallback(async () => {
    if (!resumeRun) return;
    const pid = resumeInstanceId.trim();
    if (!pid) {
      setResumeErr("Pick a connected Evolution instance");
      return;
    }
    setResumeSaving(true);
    setResumeErr("");
    try {
      const updated = await api<CampaignRunItem>(`/api/campaigns/runs/${resumeRun.id}/resume`, {
        method: "POST",
        body: JSON.stringify({ from_instance_public_id: pid }),
      });
      const publicId = runPublicId(resumeRun);
      showToast(
        `Campaign #${publicId ?? resumeRun.id} resumed on ${updated.agent_name || "new instance"}`
      );
      closeResumeModal();
      await loadRuns();
    } catch (e) {
      setResumeErr(e instanceof Error ? e.message : "Resume failed");
    } finally {
      setResumeSaving(false);
    }
  }, [closeResumeModal, loadRuns, resumeInstanceId, resumeRun, showToast]);

  useEffect(() => {
    void loadMedia();
  }, [loadMedia]);

  useEffect(() => {
    if (tab !== "campaign") return;
    void loadRuns();
  }, [tab, loadRuns]);

  // Refresh Campaign Report while any run is in progress so Success/Failed/Skipped tick up.
  useEffect(() => {
    if (tab !== "campaign") return;
    const hasLive = runs.some((r) => {
      const s = (r.status || "").toLowerCase();
      return s === "running" || s === "queued";
    });
    if (!hasLive) return;
    const id = window.setInterval(() => {
      void api<CampaignRunItem[]>("/api/campaigns/runs")
        .then((r) => setRuns(r || []))
        .catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(id);
  }, [tab, runs]);

  const stats = useMemo(() => {
    let videos = 0;
    let images = 0;
    let documents = 0;
    media.forEach((m) => {
      const k = mediaKindOf(m);
      if (k === "video") videos += 1;
      else if (k === "image") images += 1;
      else documents += 1;
    });
    return { videos, images, documents };
  }, [media]);

  const filtered = useMemo(() => {
    const q = mediaQuery.trim().toLowerCase();
    return media.filter((m) => {
      if (mediaKindOf(m) !== mediaKindFilter) return false;
      if (!mediaMatchesLanguageFilter(m, languageFilter)) return false;
      if (!q) return true;
      const uid = fmtUid(m.uid);
      const name = mediaTopic(m).toLowerCase();
      return (uid && uid.includes(q.replace(/\s+/g, ""))) || name.includes(q);
    });
  }, [media, mediaQuery, languageFilter, mediaKindFilter]);

  useEffect(() => {
    setMediaPage(0);
  }, [mediaQuery, languageFilter, mediaKindFilter]);

  const mediaPages = Math.max(1, Math.ceil(filtered.length / MEDIA_PAGE_SIZE));
  const safeMediaPage = Math.min(mediaPage, mediaPages - 1);
  const pagedMedia = useMemo(
    () => filtered.slice(safeMediaPage * MEDIA_PAGE_SIZE, safeMediaPage * MEDIA_PAGE_SIZE + MEDIA_PAGE_SIZE),
    [filtered, safeMediaPage]
  );

  const goMediaPage = useCallback((n: number) => {
    setMediaPage(n);
    mediaGridTopRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, []);

  function closeUpload() {
    setUploadOpen(false);
    setTopic("");
    setTopicErr("");
    setUploadLanguage("");
    if (fileRef.current) fileRef.current.value = "";
  }

  function openUpload() {
    setTab("media");
    setUploadOpen(true);
    setTopicErr("");
  }

  async function openMediaUsage(item: MediaItem, e: MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    setUsageOpen(true);
    setUsageLoading(true);
    setUsageErr("");
    setUsage(null);
    try {
      const data = await api<MediaUsage>(`/api/campaigns/media/${item.id}/usage`);
      setUsage(data);
    } catch (ex) {
      setUsageErr(formatApiError(ex));
    } finally {
      setUsageLoading(false);
    }
  }

  function closeMediaUsage() {
    setUsageOpen(false);
    setUsageErr("");
    setUsage(null);
  }

  function openEditMedia(item: MediaItem) {
    setEditItem(item);
    setEditTopic(mediaTopic(item));
    setEditLanguage(mediaLanguageCode(item));
    setEditErr("");
    setEditOpen(true);
  }

  function closeEditMedia() {
    if (editSaving) return;
    setEditOpen(false);
    setEditItem(null);
    setEditTopic("");
    setEditLanguage("");
    setEditErr("");
  }

  async function saveEditMedia(e: React.FormEvent) {
    e.preventDefault();
    if (!editItem) return;
    const t = editTopic.trim();
    if (!t) {
      setEditErr("Topic is required");
      return;
    }
    setEditSaving(true);
    setEditErr("");
    try {
      const updated = await api<MediaItem>(`/api/campaigns/media/${editItem.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          topic: t,
          title: t,
          language: editLanguage.trim() ? editLanguage.trim() : "",
        }),
      });
      setMedia((prev) => prev.map((m) => (m.id === editItem.id ? { ...m, ...updated } : m)));
      showToast("Media updated");
      closeEditMedia();
    } catch (ex) {
      setEditErr(ex instanceof Error ? ex.message : "Update failed");
    } finally {
      setEditSaving(false);
    }
  }

  function onQuickEdit(item: MediaItem) {
    openEditMedia(item);
  }

  async function onUpload(file: File | null) {
    if (!file) return;
    const t = topic.trim();
    if (!t) {
      setTopicErr("Topic is required");
      if (fileRef.current) fileRef.current.value = "";
      return;
    }
    setUploading(true);
    setTopicErr("");
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("topic", t);
      fd.append("title", t);
      if (uploadLanguage.trim()) fd.append("language", uploadLanguage.trim());
      const created = await api<MediaItem>("/api/campaigns/media", { method: "POST", body: fd });
      closeUpload();
      showToast(`${mediaKindOf(created) === "video" ? "Video" : mediaKindOf(created) === "image" ? "Image" : "Document"} uploaded`);
      await loadMedia();
      setSelectedId(created.id);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  function chooseFile() {
    const t = topic.trim();
    if (!t) {
      setTopicErr("Topic is required");
      return;
    }
    setTopicErr("");
    fileRef.current?.click();
  }


  function resetCardVideoEl() {
    const el = cardVideoRef.current;
    if (!el) return;
    el.pause();
    el.removeAttribute("src");
    delete el.dataset.mediaSrc;
    el.load();
  }

  async function startInlinePlay(el: HTMLVideoElement, url: string) {
    el.controls = false;
    el.playsInline = true;
    el.setAttribute("playsinline", "true");
    el.setAttribute("webkit-playsinline", "true");
    if (el.dataset.mediaSrc !== url) {
      el.src = url;
      el.dataset.mediaSrc = url;
    }
    el.muted = false;
    try {
      await el.play();
      setIsPlaying(true);
      return;
    } catch {
      /* fall through — retry muted then unmute (autoplay policy after async fetch) */
    }
    el.muted = true;
    try {
      await el.play();
      el.muted = false;
      setIsPlaying(true);
    } catch {
      setIsPlaying(false);
      showToast("Could not play video — tap Play again");
    }
  }

  async function onCardPlay(m: MediaItem, e: MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    setSelectedId(m.id);

    if (activeId === m.id && cardVideoRef.current) {
      try {
        await cardVideoRef.current.play();
        setIsPlaying(true);
      } catch {
        setIsPlaying(false);
        showToast("Could not play video");
      }
      return;
    }

    if (activeId != null && activeId !== m.id) {
      resetCardVideoEl();
    }

    setPlayLoadingId(m.id);
    try {
      const url = mediaAssetUrl(m.play_url);
      flushSync(() => {
        setActiveId(m.id);
      });
      const el = cardVideoRef.current;
      if (!el) {
        showToast("Video player not ready");
        setIsPlaying(false);
        return;
      }
      await startInlinePlay(el, url);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Playback failed");
      setActiveId(null);
      setIsPlaying(false);
    } finally {
      setPlayLoadingId(null);
    }
  }

  function onCardPause(m: MediaItem, e: MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    if (activeId !== m.id) return;
    cardVideoRef.current?.pause();
    setIsPlaying(false);
  }

  function onCardStop(m: MediaItem, e: MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    if (activeId !== m.id) return;
    const el = cardVideoRef.current;
    if (el) {
      el.pause();
      try {
        el.currentTime = 0;
      } catch {
        /* ignore */
      }
    }
    setIsPlaying(false);
    setActiveId(null);
  }

  function renderThumb(m: MediaItem, opts?: { interactive?: boolean; showMenu?: boolean }) {
    const kind = mediaKindOf(m);
    const posterUrl = kind !== "document" && m.cover_url ? mediaAssetUrl(m.cover_url) : "";
    const interactive = Boolean(opts?.interactive);
    const showMenu = Boolean(opts?.showMenu);
    const isActive = activeId === m.id;
    const playing = isActive && isPlaying;
    const isLoading = playLoadingId === m.id;
    const topicLabel = mediaTopic(m);
    const langCode = mediaLanguageCode(m);
    const langLabel =
      langCode && KNOWN_MEDIA_LANG_CODES.has(langCode)
        ? leadLanguageFilterDisplayName(langCode)
        : "";
    const uidLabel = fmtUid(m.uid);
    const dateLabel = fmtDdMmYy(m.created_at);
    const sizeLabel = fmtBytes(m.file_size);
    const hideMeta = playing;

    const renameBtn = showMenu ? (
      <button
        type="button"
        className="ml-card-rename-btn"
        aria-label={`Rename ${topicLabel || "media"}`}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          void onQuickEdit(m);
        }}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <IconPencil size={12} />
      </button>
    ) : null;

    const sendCount = typeof m.count === "number" ? m.count : 0;
    const overlay = !hideMeta ? (
      <div className="ml-card-overlay" aria-hidden={!topicLabel && !uidLabel}>
        <div className="ml-card-overlay-uid-stack">
          {uidLabel ? <span className="ml-card-overlay-uid">UID:{uidLabel}</span> : null}
          <span className="ml-card-overlay-count">
            Count: {sendCount}
            <button
              type="button"
              className="ml-card-usage-btn"
              aria-label={`View campaigns for ${topicLabel || "media"}`}
              title="Campaign usage"
              onClick={(e) => {
                void openMediaUsage(m, e);
              }}
              onMouseDown={(e) => e.stopPropagation()}
            >
              <IconEye size={12} />
            </button>
          </span>
        </div>
        {dateLabel ? <span className="ml-card-overlay-date">{dateLabel}</span> : null}
        <div className="ml-card-overlay-name-row">
          <span className="ml-card-overlay-name">{topicLabel || "—"}</span>
          {renameBtn}
        </div>
        {langLabel ? <span className="ml-card-overlay-lang">{langLabel}</span> : null}
        <span className="ml-card-overlay-size">{sizeLabel}</span>
      </div>
    ) : null;

    if (kind === "document") {
      return (
        <div className="ml-card-thumb ml-card-thumb--doc">
          <span className="ml-doc-badge">{docLabel(m)}</span>
          {overlay}
        </div>
      );
    }
    return (
      <div className={`ml-card-thumb${playing ? " is-playing" : ""}${isActive ? " is-active" : ""}`}>
        {posterUrl && !isActive ? (
          <img src={posterUrl} alt="" loading="lazy" decoding="async" />
        ) : null}
        {!posterUrl && !isActive ? <div className="ml-card-thumb-empty" aria-hidden /> : null}
        {isActive && kind === "video" ? (
          <video
            ref={interactive ? cardVideoRef : undefined}
            className="ml-card-video"
            playsInline
            preload="none"
            poster={posterUrl || undefined}
            controls={false}
            onPlay={() => {
              if (activeId === m.id) setIsPlaying(true);
            }}
            onPause={() => {
              if (activeId === m.id) setIsPlaying(false);
            }}
            onEnded={() => {
              if (activeId === m.id) {
                setIsPlaying(false);
                setActiveId(null);
              }
            }}
          />
        ) : null}
        {kind === "video" && interactive ? (
          <div className="ml-card-controls" onClick={(e) => e.stopPropagation()}>
            {playing ? (
              <button
                type="button"
                className="ml-card-play-center is-pause"
                aria-label="Pause video"
                onClick={(e) => onCardPause(m, e)}
              >
                <IconPause size={20} />
              </button>
            ) : (
              <button
                type="button"
                className="ml-card-play-center"
                aria-label="Play video"
                disabled={isLoading}
                onClick={(e) => {
                  void onCardPlay(m, e);
                }}
              >
                <IconPlay size={22} />
              </button>
            )}
            {isActive ? (
              <button
                type="button"
                className="ml-card-stop"
                aria-label="Stop video"
                onClick={(e) => onCardStop(m, e)}
              >
                <IconStop size={16} />
              </button>
            ) : null}
          </div>
        ) : kind === "video" ? (
          <span className="ml-card-play-center" aria-hidden>
            <IconPlay size={22} />
          </span>
        ) : null}
        {overlay}
      </div>
    );
  }

  return (
    <>
      <div className="ml-page">
        <div className="ml-top">
          <div className="ml-tabs" role="tablist" aria-label="Campaigns">
            <button
              type="button"
              role="tab"
              aria-selected={tab === "media"}
              className={`ml-tab${tab === "media" ? " is-active" : ""}`}
              onClick={() => setTab("media")}
            >
              <IconPlay size={15} />
              Media
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "campaign"}
              className={`ml-tab${tab === "campaign" ? " is-active" : ""}`}
              onClick={() => setTab("campaign")}
            >
              <IconMegaphone size={15} />
              Campaign
            </button>
          </div>

          {tab === "media" && (
            <>
              <label className="ml-lang-filter">
                <span className="sr-only">Filter by language</span>
                <select
                  value={languageFilter}
                  onChange={(e) => setLanguageFilter(e.target.value)}
                  aria-label="Filter media by language"
                >
                  <option value="">All languages</option>
                  {LEAD_LANGUAGE_MARKETS.map((m) => (
                    <option key={m.code} value={m.code}>
                      {m.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="ml-top-search">
                <input
                  value={mediaQuery}
                  onChange={(e) => setMediaQuery(e.target.value)}
                  placeholder="UID or name"
                  aria-label="Search media by UID or name"
                />
              </label>
            </>
          )}

          {tab === "media" && (
            <div className="ml-top-actions">
              <button type="button" className="btn primary ml-btn-add" onClick={openUpload}>
                <IconPlus size={15} />
                Add Media
              </button>
            </div>
          )}
        </div>

        {err && (
          <div className="panel" style={{ padding: 16, color: "var(--red)" }}>
            {err}
          </div>
        )}

        {tab === "media" && (
          <div className="ml-shell">
            <div className="ml-main panel">
              <div className="ml-stats" role="group" aria-label="Filter by media type">
                <button
                  type="button"
                  className={`ml-stat ml-stat--filter${mediaKindFilter === "video" ? " is-active" : ""}`}
                  aria-pressed={mediaKindFilter === "video"}
                  onClick={() => setMediaKindFilter("video")}
                >
                  <span className="ml-stat-ico ml-stat-ico--video">
                    <IconPlay size={14} />
                  </span>
                  <div>
                    <b>{stats.videos}</b>
                    <span>Videos</span>
                  </div>
                </button>
                <button
                  type="button"
                  className={`ml-stat ml-stat--filter${mediaKindFilter === "image" ? " is-active" : ""}`}
                  aria-pressed={mediaKindFilter === "image"}
                  onClick={() => setMediaKindFilter("image")}
                >
                  <span className="ml-stat-ico ml-stat-ico--image">
                    <IconImage size={14} />
                  </span>
                  <div>
                    <b>{stats.images}</b>
                    <span>Images</span>
                  </div>
                </button>
                <button
                  type="button"
                  className={`ml-stat ml-stat--filter${mediaKindFilter === "document" ? " is-active" : ""}`}
                  aria-pressed={mediaKindFilter === "document"}
                  onClick={() => setMediaKindFilter("document")}
                >
                  <span className="ml-stat-ico ml-stat-ico--doc">
                    <IconFile size={14} />
                  </span>
                  <div>
                    <b>{stats.documents}</b>
                    <span>Documents</span>
                  </div>
                </button>
              </div>

              {filtered.length === 0 && !loading ? (
                <div className="camp-empty">
                  {media.length === 0 ? (
                    <>
                      No media yet. Click <b>+ Add Media</b> to upload a product clip, image, or document.
                    </>
                  ) : languageFilter ? (
                    <>
                      No{" "}
                      {mediaKindFilter === "video"
                        ? "videos"
                        : mediaKindFilter === "image"
                          ? "images"
                          : "documents"}{" "}
                      for <b>{leadLanguageFilterDisplayName(languageFilter)}</b>
                      {mediaQuery.trim() ? " matching your search" : ""}. Try{" "}
                      <b>All languages</b> or set language on a clip via the pencil icon.
                    </>
                  ) : mediaQuery.trim() ? (
                    "No media matches your search."
                  ) : (
                    <>
                      No{" "}
                      {mediaKindFilter === "video"
                        ? "videos"
                        : mediaKindFilter === "image"
                          ? "images"
                          : "documents"}{" "}
                      yet. Click <b>+ Add Media</b> to upload.
                    </>
                  )}
                </div>
              ) : (
                <>
                  <div ref={mediaGridTopRef}>
                    <MediaPager
                      page={safeMediaPage}
                      pages={mediaPages}
                      total={filtered.length}
                      onPage={goMediaPage}
                      placement="top"
                    />
                  </div>
                  <div className="ml-grid">
                    {pagedMedia.map((m) => {
                      const isSel = selectedId === m.id;
                      return (
                        <article
                          key={m.id}
                          className={`ml-card${isSel ? " is-selected" : ""}`}
                          onClick={() => {
                            if (activeId != null && activeId !== m.id) {
                              resetCardVideoEl();
                              setActiveId(null);
                              setIsPlaying(false);
                            }
                            setSelectedId(m.id);
                          }}
                        >
                          {renderThumb(m, { interactive: true, showMenu: true })}
                        </article>
                      );
                    })}
                  </div>
                  <MediaPager
                    page={safeMediaPage}
                    pages={mediaPages}
                    total={filtered.length}
                    onPage={goMediaPage}
                    placement="bottom"
                  />
                </>
              )}
            </div>
          </div>
        )}

        {tab === "campaign" && (
          <div className="panel">
            <div className="panelhead">
              <div>
                <h2>Report</h2>
                <p>WhatsApp campaign blast results</p>
              </div>
            </div>
            {runsLoading && runs.length === 0 ? (
              <p className="camp-hint">Loading campaign report…</p>
            ) : runs.length === 0 ? (
              <div className="camp-empty">No campaign runs yet.</div>
            ) : (
              <div className="camp-report-wrap">
                <table className="camp-report-table">
                  <thead>
                    <tr>
                      <th className="num">ID</th>
                      <th>Date</th>
                      <th>Agent name</th>
                      <th>Media name</th>
                      <th>Audience name</th>
                      <th className="num">Total</th>
                      <th className="num">Success</th>
                      <th className="num">Failed</th>
                      <th className="num">Skipped</th>
                      <th className="num">Replied</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((run) => {
                      const live = runIsLive(run.status);
                      const badge = runStatusLabel(run.status);
                      const publicId = runPublicId(run);
                      const canAct = runCanRestart(run);
                      const busy = actionBusyId === run.id;
                      return (
                        <tr
                          key={run.id}
                          className={live ? "camp-report-row is-live" : undefined}
                          title={run.note || undefined}
                        >
                          <td className="num mono camp-report-id">{publicId ?? "—"}</td>
                          <td>
                            <div className="camp-report-date-stack">
                              <span className="camp-report-date-cell">
                                {fmtReportDateTime(run.occurred_at)}
                                {live ? (
                                  <span className="camp-run-live-icon" title="Running" aria-label="Running">
                                    <IconRunLive size={13} />
                                  </span>
                                ) : badge ? (
                                  <span className={runStatusClass(run.status)}>{badge}</span>
                                ) : null}
                              </span>
                              {live && run.estimated_end_at ? (
                                <div className="camp-report-est-end" title="Estimated completion (IST pacing rules)">
                                  Est. end: {fmtReportDateTime(run.estimated_end_at)}
                                </div>
                              ) : null}
                              {canAct ? (
                                <div className="camp-report-actions">
                                  <button
                                    type="button"
                                    className="btn camp-report-action"
                                    disabled={busy || resumeSaving}
                                    title="Restart on the same Evolution instance after WhatsApp reconnects. Skips already-successful sends."
                                    onClick={() => void onRestartRun(run)}
                                  >
                                    {busy ? "…" : "Restart"}
                                  </button>
                                  <button
                                    type="button"
                                    className="btn camp-report-action"
                                    disabled={busy || resumeSaving}
                                    title="Continue remaining recipients on a different Evolution / WhatsApp number (e.g. after 24h block)."
                                    onClick={() => void openResumeModal(run)}
                                  >
                                    Resume on new instance
                                  </button>
                                </div>
                              ) : null}
                            </div>
                          </td>
                          <td>{run.agent_name || "—"}</td>
                          <td>{run.media_name || "—"}</td>
                          <td>{run.audience_name || "—"}</td>
                          <td className="num mono">{run.total_count}</td>
                          <td className="num mono camp-report-ok">{run.success_count}</td>
                          <td className="num mono camp-report-fail">{run.failed_count}</td>
                          <td className="num mono">{run.skipped_count}</td>
                          <td className="num mono">{run.replied_count ?? 0}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>

      {resumeRun && (
        <div
          className="camp-modal-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget && !resumeSaving) closeResumeModal();
          }}
        >
          <div
            className="camp-modal camp-modal--resume"
            role="dialog"
            aria-modal="true"
            aria-labelledby="camp-resume-title"
          >
            <div className="camp-modal-head">
              <h2 id="camp-resume-title">
                Resume on new instance · #{runPublicId(resumeRun) ?? resumeRun.id}
              </h2>
              <button type="button" className="btn" disabled={resumeSaving} onClick={closeResumeModal}>
                Close
              </button>
            </div>
            <div className="camp-modal-body">
              <p className="camp-hint">
                Continues <strong>{resumeRun.audience_name || "audience"}</strong> with media{" "}
                <strong>{resumeRun.media_name || "—"}</strong>. Already-successful sends (
                {resumeRun.success_count}) stay skipped — only remaining / failed recipients are queued
                on the new WhatsApp number.
              </p>
              <p className="camp-hint">
                Current agent: {resumeRun.agent_name || "—"}
                {resumeRun.from_instance_public_id
                  ? ` · ${resumeRun.from_instance_public_id.slice(0, 8)}…`
                  : ""}
              </p>
              <label className="camp-field">
                <span>Connected Evolution instance</span>
                {sendInstancesLoading ? (
                  <p className="camp-hint">Loading instances…</p>
                ) : sendInstances.length === 0 ? (
                  <p className="camp-field-err">No connected Evolution instances available.</p>
                ) : (
                  <select
                    value={resumeInstanceId}
                    disabled={resumeSaving}
                    onChange={(e) => setResumeInstanceId(e.target.value)}
                  >
                    {sendInstances.map((inst) => (
                      <option key={inst.public_id} value={inst.public_id}>
                        {inst.label || inst.employee_name || inst.display_name}
                        {inst.public_id === resumeRun.from_instance_public_id ? " (current)" : ""}
                      </option>
                    ))}
                  </select>
                )}
              </label>
              {resumeErr ? <p className="camp-field-err">{resumeErr}</p> : null}
              <div className="camp-upload-actions">
                <button
                  type="button"
                  className="btn primary"
                  disabled={resumeSaving || sendInstancesLoading || !resumeInstanceId}
                  onClick={() => void onConfirmResume()}
                >
                  {resumeSaving ? "Resuming…" : "Resume campaign"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {usageOpen && (
        <div
          className="camp-modal-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget) closeMediaUsage();
          }}
        >
          <div
            className="camp-modal camp-modal--usage"
            role="dialog"
            aria-modal="true"
            aria-labelledby="camp-usage-title"
          >
            <div className="camp-modal-head">
              <h2 id="camp-usage-title">
                {usage?.media_title
                  ? `Campaigns · ${usage.media_title}`
                  : usage?.media_uid
                    ? `Campaigns · UID ${usage.media_uid}`
                    : "Campaign usage"}
              </h2>
              <button type="button" className="btn" onClick={closeMediaUsage}>
                Close
              </button>
            </div>
            <div className="camp-modal-body camp-usage-body">
              {usageLoading ? (
                <p className="camp-hint">Loading…</p>
              ) : usageErr ? (
                <p className="camp-field-err">{usageErr}</p>
              ) : !usage || usage.campaigns.length === 0 ? (
                <p className="camp-hint">No campaigns have used this media yet.</p>
              ) : (
                <>
                  <p className="camp-usage-summary">
                    {usage.campaigns.length} campaign{usage.campaigns.length === 1 ? "" : "s"}
                    {" · "}
                    {usage.total_sent} total sent
                    {usage.media_uid != null ? ` · UID ${usage.media_uid}` : ""}
                  </p>
                  <ul className="camp-usage-list">
                    {usage.campaigns.map((c) => (
                      <li key={c.run_id} className="camp-usage-item">
                        <div className="camp-usage-item-head">
                          <span className="camp-usage-id">
                            #{c.campaign_number ?? c.run_id}
                          </span>
                          <span
                            className={`camp-usage-status${
                              runIsLive(c.status) ? " is-live" : c.status === "failed" ? " is-failed" : ""
                            }`}
                          >
                            {c.details || c.status}
                          </span>
                        </div>
                        <dl className="camp-usage-meta">
                          <div>
                            <dt>Audience</dt>
                            <dd>
                              {c.audience_name || "—"}
                              {c.audience_size > 0 ? (
                                <span className="camp-usage-muted"> · {c.audience_size}</span>
                              ) : null}
                              {c.audience_uid != null ? (
                                <span className="camp-usage-muted"> · aud {c.audience_uid}</span>
                              ) : null}
                            </dd>
                          </div>
                          <div>
                            <dt>Sent</dt>
                            <dd className="mono">{c.total_sent}</dd>
                          </div>
                          <div>
                            <dt>From</dt>
                            <dd>{c.from_number || c.from_agent || "—"}</dd>
                          </div>
                        </dl>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {uploadOpen && (
        <div
          className="camp-modal-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget && !uploading) closeUpload();
          }}
        >
          <div className="camp-modal" role="dialog" aria-modal="true" aria-labelledby="camp-upload-title">
            <div className="camp-modal-head">
              <h2 id="camp-upload-title">Upload media</h2>
              <button type="button" className="btn" disabled={uploading} onClick={closeUpload}>
                Close
              </button>
            </div>
            <div className="camp-modal-body">
              <label className="camp-field">
                <span>Topic</span>
                <input
                  value={topic}
                  onChange={(e) => {
                    setTopic(e.target.value);
                    if (topicErr && e.target.value.trim()) setTopicErr("");
                  }}
                  placeholder="e.g. BENCH PRO product reel"
                  autoFocus
                  required
                />
                {topicErr ? <em className="camp-field-err">{topicErr}</em> : null}
                <span className="camp-hint">UID is unique; topic may match other media.</span>
              </label>
              <label className="camp-field">
                <span>Language (optional)</span>
                <select
                  value={uploadLanguage}
                  onChange={(e) => setUploadLanguage(e.target.value)}
                >
                  <option value="">— Not set —</option>
                  {LEAD_LANGUAGE_MARKETS.map((m) => (
                    <option key={m.code} value={m.code}>
                      {m.label}
                    </option>
                  ))}
                </select>
              </label>
              <p className="camp-hint">
                Videos, images, or documents. Video covers use the first frame.
              </p>
              <div className="camp-upload-actions">
                <input
                  ref={fileRef}
                  type="file"
                  accept={ACCEPT_MEDIA}
                  disabled={uploading}
                  onChange={(e) => {
                    const f = e.target.files?.[0] || null;
                    void onUpload(f);
                  }}
                />
                <button className="btn primary" type="button" disabled={uploading} onClick={chooseFile}>
                  {uploading ? "Uploading…" : "Choose file"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {editOpen && editItem ? (
        <div
          className="camp-modal-backdrop camp-modal-backdrop--top"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget && !editSaving) closeEditMedia();
          }}
        >
          <form
            className="camp-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="camp-edit-title"
            onSubmit={(e) => {
              void saveEditMedia(e);
            }}
          >
            <div className="camp-modal-head">
              <h2 id="camp-edit-title">Edit media</h2>
              <button type="button" className="btn" disabled={editSaving} onClick={closeEditMedia}>
                Close
              </button>
            </div>
            <div className="camp-modal-body">
              <label className="camp-field">
                <span>Topic</span>
                <input
                  value={editTopic}
                  onChange={(e) => {
                    setEditTopic(e.target.value);
                    if (editErr && e.target.value.trim()) setEditErr("");
                  }}
                  autoFocus
                  required
                />
                <span className="camp-hint">UID is unique; topic may match other media.</span>
              </label>
              <label className="camp-field">
                <span>Language</span>
                <select
                  value={editLanguage}
                  onChange={(e) => setEditLanguage(e.target.value)}
                >
                  <option value="">— Not set —</option>
                  {LEAD_LANGUAGE_MARKETS.map((m) => (
                    <option key={m.code} value={m.code}>
                      {m.label}
                    </option>
                  ))}
                </select>
              </label>
              {editErr ? <p className="camp-field-err">{editErr}</p> : null}
              <div className="camp-upload-actions">
                <button className="btn primary" type="submit" disabled={editSaving}>
                  {editSaving ? "Saving…" : "Save"}
                </button>
              </div>
            </div>
          </form>
        </div>
      ) : null}

      {toast && (
        <div className="emp-toast" role="status">
          {toast}
        </div>
      )}
    </>
  );
}
