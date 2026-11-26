import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, formatApiError, getToken } from "../api/client";

const API_BASE = import.meta.env.VITE_API_URL || "";

/** Malayalam product pitch — default Common TTS Preview; editable. */
const DEFAULT_TTS_SAMPLE =
  "ലേബൽ പ്രിന്റിങ് സ്കെയിലുകൾ, ബില്ലിങ് സ്കെയിലുകൾ, ഇൻഡസ്ട്രിയൽ വെയിങ് മെഷീനുകൾ, ടച്ച് സ്ക്രീൻ സംവിധാനങ്ങൾ, ക്ലൗഡ് അധിഷ്ഠിത സോഫ്റ്റ്‌വെയറുകൾ എന്നിവയാണ് ഞങ്ങളുടെ പ്രധാന ഉൽപ്പന്നങ്ങൾ.\n\nമികച്ച സാങ്കേതികവിദ്യയും വിശ്വസനീയമായ സേവനവും ഉപയോഗിച്ച് നിങ്ങളുടെ ബിസിനസിനെ കൂടുതൽ സ്മാർട്ടാക്കാൻ ഞങ്ങൾ പ്രതിജ്ഞാബദ്ധരാണ്.";

const GLOBAL_TTS_KEY = "tts-global";

type InstanceRow = {
  id: number;
  public_id: string;
  display_name: string;
  employee_name: string;
  connected_phone_number: string;
  connection_status: string;
  provider: string;
  sarvam_voice_id: string;
  sarvam_voice_name: string;
  sarvam_voice_sample_url: string;
  sarvam_voice_sample_message_id: number | null;
  sarvam_voice_created_at: string | null;
  voice_tts_enabled: boolean;
};

type Sample = {
  message_id: number;
  media_url: string;
  media_filename: string;
  duration_s: number | null;
  score: number;
  transcript_preview: string;
  created_at: string | null;
};

type StatusPayload = {
  sarvam_configured: boolean;
  pricing_note: string;
  consent_note: string;
  target_duration_s: number[];
  acceptable_duration_s: number[];
  instances: InstanceRow[];
};

async function fetchAuthedBlobUrl(path: string): Promise<string> {
  const token = getToken();
  const res = await fetch(`${API_BASE}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    credentials: "include",
  });
  if (!res.ok) {
    if (res.status === 401 && typeof window !== "undefined") {
      localStorage.removeItem("crm_token");
      if (!window.location.pathname.startsWith("/login")) window.location.assign("/login");
    }
    const text = await res.text().catch(() => "");
    throw new Error(formatApiError(res.status, text, "Could not load audio"));
  }
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}

function IconPlay({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M8 5v14l11-7L8 5z" />
    </svg>
  );
}

function IconPause({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M6 5h4v14H6V5zm8 0h4v14h-4V5z" />
    </svg>
  );
}

function IconMic({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
      <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <line x1="12" y1="19" x2="12" y2="23" />
      <line x1="8" y1="23" x2="16" y2="23" />
    </svg>
  );
}

function IconInfo({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="16" x2="12" y2="12" />
      <line x1="12" y1="8" x2="12.01" y2="8" />
    </svg>
  );
}

function IconUpload({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" aria-hidden>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="17 8 12 3 7 8" />
      <line x1="12" y1="3" x2="12" y2="15" />
    </svg>
  );
}

function formatPhone(raw: string): string {
  const digits = (raw || "").replace(/\D/g, "");
  if (digits.length === 12 && digits.startsWith("91")) {
    return `+91 ${digits.slice(2, 7)} ${digits.slice(7)}`;
  }
  if (digits.length === 10) {
    return `+91 ${digits.slice(0, 5)} ${digits.slice(5)}`;
  }
  return raw || "—";
}

function formatUpdatedAt(iso: string | null): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function sampleFilename(url: string, name: string): string {
  const fromUrl = (url || "").split("/").pop() || "";
  if (fromUrl) return fromUrl;
  const slug = (name || "voice").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
  return `sample_${slug || "voice"}.ogg`;
}

function initialOf(name: string): string {
  const t = (name || "").trim();
  return t ? t[0]!.toUpperCase() : "?";
}

function isConnected(status: string): boolean {
  return (status || "").toLowerCase() === "connected";
}

function hasClone(row: InstanceRow): boolean {
  return !!(row.sarvam_voice_id || "").trim();
}

const WAVE_BARS = [28, 52, 38, 70, 44, 62, 34, 78, 48, 66, 40, 58, 32, 72, 46, 64, 36, 54];

function Waveform({ active }: { active: boolean }) {
  return (
    <div className={`vc-wave${active ? " is-playing" : ""}`} aria-hidden>
      {WAVE_BARS.map((h, i) => (
        <span key={i} style={{ height: `${h}%`, animationDelay: `${i * 0.05}s` }} />
      ))}
    </div>
  );
}

export default function VoiceCloneSettings() {
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [samplesFor, setSamplesFor] = useState<number | null>(null);
  const [samples, setSamples] = useState<Sample[]>([]);
  const [sampleCounts, setSampleCounts] = useState<Record<number, number>>({});
  const [globalPreviewText, setGlobalPreviewText] = useState(DEFAULT_TTS_SAMPLE);
  const [globalPreviewInst, setGlobalPreviewInst] = useState<number | null>(null);

  const [playingKey, setPlayingKey] = useState<string | null>(null);
  const [loadingKey, setLoadingKey] = useState<string | null>(null);
  const blobUrlsRef = useRef<Record<string, string>>({});
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const durationCacheRef = useRef<Record<string, number>>({});
  const [durations, setDurations] = useState<Record<string, number>>({});

  const load = useCallback(async () => {
    setErr("");
    try {
      const data = await api<StatusPayload>("/api/voice-clones/status");
      setStatus(data);
      const withClone = (data.instances || []).filter((i) => (i.sarvam_voice_id || "").trim());
      setGlobalPreviewInst((prev) => {
        if (prev && withClone.some((i) => i.id === prev)) return prev;
        return withClone[0]?.id ?? null;
      });
      // Best-effort sample counts for cards (non-blocking)
      void Promise.all(
        (data.instances || []).map(async (inst) => {
          try {
            const res = await api<{ samples: Sample[] }>(`/api/voice-clones/samples/${inst.id}?limit=12`);
            setSampleCounts((prev) => ({ ...prev, [inst.id]: (res.samples || []).length }));
          } catch {
            /* ignore — count falls back to mapped sample */
          }
        }),
      );
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load voice clones");
    }
  }, []);

  useEffect(() => {
    load().catch(() => null);
  }, [load]);

  useEffect(() => {
    return () => {
      const el = audioRef.current;
      if (el) {
        el.pause();
        el.removeAttribute("src");
      }
      Object.values(blobUrlsRef.current).forEach((url) => URL.revokeObjectURL(url));
      blobUrlsRef.current = {};
    };
  }, []);

  function stopPlayback() {
    const el = audioRef.current;
    if (el) {
      el.pause();
      try {
        el.currentTime = 0;
      } catch {
        /* ignore */
      }
    }
    setPlayingKey(null);
  }

  async function ensureBlob(key: string, mediaPath: string): Promise<string> {
    const cached = blobUrlsRef.current[key];
    if (cached) return cached;
    const url = await fetchAuthedBlobUrl(mediaPath);
    if (blobUrlsRef.current[key]) {
      URL.revokeObjectURL(url);
      return blobUrlsRef.current[key];
    }
    blobUrlsRef.current[key] = url;
    return url;
  }

  async function rememberDuration(key: string, el: HTMLAudioElement) {
    const known = durationCacheRef.current[key];
    if (known && Number.isFinite(known) && known > 0) return;
    const d = el.duration;
    if (Number.isFinite(d) && d > 0) {
      durationCacheRef.current[key] = d;
      setDurations((prev) => ({ ...prev, [key]: d }));
    } else {
      await new Promise<void>((resolve) => {
        const onMeta = () => {
          el.removeEventListener("loadedmetadata", onMeta);
          const md = el.duration;
          if (Number.isFinite(md) && md > 0) {
            durationCacheRef.current[key] = md;
            setDurations((prev) => ({ ...prev, [key]: md }));
          }
          resolve();
        };
        el.addEventListener("loadedmetadata", onMeta);
        window.setTimeout(() => {
          el.removeEventListener("loadedmetadata", onMeta);
          resolve();
        }, 1500);
      });
    }
  }

  async function togglePlay(key: string, mediaPath: string) {
    if (!mediaPath) return;
    if (playingKey === key) {
      stopPlayback();
      return;
    }

    setErr("");
    setLoadingKey(key);
    try {
      stopPlayback();
      const url = await ensureBlob(key, mediaPath);
      let el = audioRef.current;
      if (!el) {
        el = new Audio();
        audioRef.current = el;
        el.addEventListener("ended", () => setPlayingKey(null));
      }
      if (el.getAttribute("src") !== url) {
        el.src = url;
      }
      await rememberDuration(key, el);
      await el.play();
      setPlayingKey(key);
    } catch (e) {
      setPlayingKey(null);
      setErr(e instanceof Error ? e.message : "Could not play sample");
    } finally {
      setLoadingKey(null);
    }
  }

  async function loadSamples(instanceId: number) {
    setBusy(`samples-${instanceId}`);
    setErr("");
    try {
      const data = await api<{ samples: Sample[] }>(`/api/voice-clones/samples/${instanceId}?limit=12`);
      setSamplesFor(instanceId);
      setSamples(data.samples || []);
      setSampleCounts((prev) => ({ ...prev, [instanceId]: (data.samples || []).length }));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load samples");
    } finally {
      setBusy("");
    }
  }

  async function createClone(instanceId: number, messageId?: number) {
    setBusy(`create-${instanceId}`);
    setErr("");
    stopPlayback();
    try {
      await api("/api/voice-clones/create", {
        method: "POST",
        body: JSON.stringify({
          whatsapp_instance_id: instanceId,
          message_id: messageId || null,
          replace_existing: true,
          language: "ml-IN",
          gender: "male",
        }),
      });
      await load();
      setSamplesFor(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Clone create failed");
    } finally {
      setBusy("");
    }
  }

  async function patch(instanceId: number, body: Record<string, unknown>) {
    setBusy(`patch-${instanceId}`);
    setErr("");
    try {
      await api(`/api/voice-clones/instances/${instanceId}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Update failed");
    } finally {
      setBusy("");
    }
  }

  function revokeBlobKey(key: string) {
    const prev = blobUrlsRef.current[key];
    if (prev) {
      URL.revokeObjectURL(prev);
      delete blobUrlsRef.current[key];
    }
  }

  async function synthesizeAndPlay(opts: {
    instanceId: number;
    displayName: string;
    text: string;
    playKey: string;
    busyKey: string;
  }) {
    const text = opts.text.trim();
    if (!text) {
      setErr("Enter text to synthesize before playing TTS.");
      return;
    }
    if (!opts.instanceId) {
      setErr("Select a WhatsApp clone voice first.");
      return;
    }

    if (playingKey === opts.playKey && blobUrlsRef.current[opts.playKey]) {
      stopPlayback();
      return;
    }

    setBusy(opts.busyKey);
    setLoadingKey(opts.playKey);
    setErr("");
    stopPlayback();
    try {
      const data = await api<{ audio_data_url?: string; media_url?: string }>(
        "/api/voice-clones/preview-tts",
        {
          method: "POST",
          body: JSON.stringify({
            whatsapp_instance_id: opts.instanceId,
            text,
            language_code: "ml-IN",
          }),
        },
      );

      revokeBlobKey(opts.playKey);
      let url = "";
      const mediaPath = (data.media_url || "").trim();
      if (mediaPath.startsWith("/api/")) {
        url = await fetchAuthedBlobUrl(mediaPath);
      } else if (data.audio_data_url) {
        const blob = await (await fetch(data.audio_data_url)).blob();
        url = URL.createObjectURL(blob);
      }
      if (!url) {
        throw new Error("TTS returned no audio — try again or recreate the clone.");
      }
      blobUrlsRef.current[opts.playKey] = url;

      let el = audioRef.current;
      if (!el) {
        el = new Audio();
        audioRef.current = el;
        el.addEventListener("ended", () => setPlayingKey(null));
      }
      el.src = url;
      await el.play();
      setPlayingKey(opts.playKey);
    } catch (e) {
      setPlayingKey(null);
      const msg = e instanceof Error ? e.message : "Preview TTS failed";
      setErr(`${opts.displayName}: ${msg}`);
    } finally {
      setBusy("");
      setLoadingKey(null);
    }
  }

  async function playGlobalPreview() {
    if (playingKey === GLOBAL_TTS_KEY) {
      stopPlayback();
      return;
    }
    const list = status?.instances || [];
    const inst = list.find((i) => i.id === globalPreviewInst);
    if (!inst) {
      setErr("Create a clone on a WhatsApp line first, then play TTS.");
      return;
    }
    if (!(inst.sarvam_voice_id || "").trim()) {
      setErr(`${inst.display_name}: no clone mapped — create a clone first.`);
      return;
    }
    await synthesizeAndPlay({
      instanceId: inst.id,
      displayName: inst.display_name,
      text: globalPreviewText,
      playKey: GLOBAL_TTS_KEY,
      busyKey: "preview-global",
    });
  }

  async function playCloneVoice(row: InstanceRow) {
    if (!hasClone(row)) return;
    const playKey = `tts-${row.id}`;
    if (playingKey === playKey) {
      stopPlayback();
      return;
    }
    await synthesizeAndPlay({
      instanceId: row.id,
      displayName: row.display_name,
      text: globalPreviewText,
      playKey,
      busyKey: `preview-${row.id}`,
    });
  }

  const instances = status?.instances || [];
  const clonedInstances = instances.filter((i) => hasClone(i));
  const connectedCount = instances.filter((i) => isConnected(i.connection_status)).length;
  const clonesReady = clonedInstances.length;
  const pending = Math.max(0, instances.length - clonesReady);
  const globalPlaying = playingKey === GLOBAL_TTS_KEY;
  const globalLoading = loadingKey === GLOBAL_TTS_KEY || busy === "preview-global";

  const stats = useMemo(
    () => [
      { label: "Total Instances", value: instances.length, tone: "blue" as const, icon: "stack" },
      { label: "Clones Ready", value: clonesReady, tone: "green" as const, icon: "check" },
      { label: "Pending", value: pending, tone: "orange" as const, icon: "clock" },
      { label: "Connected", value: connectedCount, tone: "blue" as const, icon: "link" },
    ],
    [instances.length, clonesReady, pending, connectedCount],
  );

  return (
    <div className="vc-page">
      <div className="vc-hero">
        <div className="vc-hero-copy">
          <div className="vc-title-row">
            <span className="vc-title-icon" aria-hidden>
              <IconMic />
            </span>
            <h2>Voice Cloning</h2>
          </div>
          <p>Manage voice clones for WhatsApp instances and test text-to-speech (TTS) quality.</p>
        </div>
        {status ? (
          <div className="vc-stats">
            {stats.map((s) => (
              <div key={s.label} className={`vc-stat vc-stat-${s.tone}`}>
                <div className="vc-stat-top">
                  <span className="vc-stat-label">{s.label}</span>
                  <span className={`vc-stat-ico vc-stat-ico-${s.icon}`} aria-hidden />
                </div>
                <b>{s.value}</b>
              </div>
            ))}
          </div>
        ) : null}
      </div>

      {err ? <div className="err" style={{ marginBottom: 12 }}>{err}</div> : null}

      <section className="vc-card vc-preview-card">
        <div className="vc-card-head">
          <h3>Common TTS Preview Text</h3>
          <span className="vc-pill">Applies to all voices</span>
        </div>
        <div className="vc-preview-body">
          <textarea
            className="vc-textarea"
            value={globalPreviewText}
            onChange={(e) => setGlobalPreviewText(e.target.value)}
            rows={3}
            maxLength={1000}
            disabled={!!busy}
            aria-label="Common TTS preview text"
          />
          <div className="vc-preview-side">
            {clonedInstances.length > 1 ? (
              <label className="vc-voice-pick">
                <span>Play as</span>
                <select
                  value={globalPreviewInst ?? ""}
                  onChange={(e) => {
                    const v = e.target.value ? Number(e.target.value) : null;
                    setGlobalPreviewInst(v);
                    if (playingKey === GLOBAL_TTS_KEY) stopPlayback();
                  }}
                  disabled={!!busy}
                >
                  {clonedInstances.map((i) => (
                    <option key={i.id} value={i.id}>
                      {i.display_name}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            <button
              type="button"
              className="btn primary vc-play-tts"
              disabled={
                !!busy ||
                !!loadingKey ||
                !status?.sarvam_configured ||
                !clonedInstances.length ||
                !globalPreviewInst ||
                !globalPreviewText.trim()
              }
              aria-label={globalPlaying ? "Pause TTS preview" : "Play TTS preview"}
              onClick={() => playGlobalPreview()}
            >
              {globalLoading ? (
                "Synthesizing…"
              ) : globalPlaying ? (
                <>
                  <IconPause /> Pause
                </>
              ) : (
                <>
                  <IconPlay /> Play TTS
                </>
              )}
            </button>
            <p className="vc-preview-note">
              <IconInfo /> Preview only. Not sent on WhatsApp.
            </p>
          </div>
        </div>
      </section>

      <div className="vc-section-label">Per WhatsApp instance</div>

      {!instances.length ? (
        <section className="vc-card">
          <p className="wa-hint" style={{ margin: 0, padding: 8 }}>No Evolution instances.</p>
        </section>
      ) : null}

      <div className="vc-instance-list">
        {instances.map((row) => {
          const ready = hasClone(row);
          const connected = isConnected(row.connection_status);
          const idBusy = busy.includes(String(row.id));
          const sampleUrl = (row.sarvam_voice_sample_url || "").trim();
          const sampleKey = `clone-${row.id}`;
          const samplePlaying = playingKey === sampleKey;
          const sampleLoading = loadingKey === sampleKey;
          const ttsKey = `tts-${row.id}`;
          const ttsPlaying = playingKey === ttsKey;
          const ttsLoading = loadingKey === ttsKey || busy === `preview-${row.id}`;
          const count =
            sampleCounts[row.id] ??
            (sampleUrl || row.sarvam_voice_sample_message_id ? 1 : 0);
          const durSec = durations[sampleKey];
          const durLabel =
            durSec && Number.isFinite(durSec)
              ? `${String(Math.floor(durSec / 60)).padStart(2, "0")}:${String(Math.floor(durSec % 60)).padStart(2, "0")}`
              : null;

          return (
            <section key={row.id} className="vc-card vc-instance">
              <div className="vc-instance-top">
                <div className="vc-identity">
                  <div className="vc-avatar" aria-hidden>
                    {initialOf(row.display_name)}
                  </div>
                  <div>
                    <div className="vc-name">{row.display_name}</div>
                    <div className="vc-phone">{formatPhone(row.connected_phone_number)}</div>
                  </div>
                </div>
                <div className="vc-badges">
                  <span className={`vc-status ${connected ? "is-on" : "is-off"}`}>
                    <span className="vc-dot" />
                    {connected ? "Connected" : row.connection_status || "Disconnected"}
                  </span>
                  {ready ? (
                    <span className="vc-clone-badge is-ready">
                      <span className="vc-clone-check" aria-hidden>✓</span> Clone Ready
                    </span>
                  ) : (
                    <span className="vc-clone-badge is-empty">○ No clone</span>
                  )}
                </div>
                <div className="vc-meta">
                  <span>Samples: {count}</span>
                  {row.sarvam_voice_created_at ? (
                    <span>Last updated: {formatUpdatedAt(row.sarvam_voice_created_at)}</span>
                  ) : null}
                </div>
              </div>

              <label className="vc-allow">
                <input
                  type="checkbox"
                  checked={!!row.voice_tts_enabled}
                  disabled={idBusy || !ready}
                  onChange={(e) => patch(row.id, { voice_tts_enabled: e.target.checked })}
                />
                <span>Allow send as clone voice in inbox</span>
                <span
                  className="vc-allow-info"
                  title="When enabled, inbox can send TTS as this line’s cloned voice (explicit confirm required)."
                >
                  <IconInfo />
                </span>
              </label>

              <div className="vc-grid">
                <button
                  type="button"
                  className="vc-upload"
                  disabled={!!busy || !status?.sarvam_configured}
                  onClick={() => loadSamples(row.id)}
                >
                  <IconUpload />
                  <strong>Upload voice sample</strong>
                  <span>OGG, MP3, or WAV · max 25 MB</span>
                  <em>Pick from outbound WhatsApp notes</em>
                </button>

                <div className={`vc-latest${sampleUrl ? "" : " is-empty"}`}>
                  {sampleUrl ? (
                    <>
                      <div className="vc-latest-label">Latest Sample</div>
                      <div className="vc-latest-file">{sampleFilename(sampleUrl, row.display_name)}</div>
                      <Waveform active={samplePlaying} />
                      <div className="vc-latest-controls">
                        <button
                          type="button"
                          className="vc-round-play"
                          disabled={!!busy || !!loadingKey}
                          aria-label={
                            samplePlaying
                              ? `Pause sample for ${row.display_name}`
                              : `Play sample for ${row.display_name}`
                          }
                          onClick={() => togglePlay(sampleKey, sampleUrl)}
                        >
                          {sampleLoading ? "…" : samplePlaying ? <IconPause size={16} /> : <IconPlay size={16} />}
                        </button>
                        <span className="vc-dur">{durLabel || "—:—"}</span>
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="vc-empty-wave" aria-hidden>
                        <Waveform active={false} />
                      </div>
                      <strong>No sample uploaded yet</strong>
                      <span>Upload a clear voice sample (10–15s) to create a clone.</span>
                    </>
                  )}
                </div>

                <div className="vc-actions">
                  <button
                    type="button"
                    className="btn primary vc-action-primary"
                    disabled={!!busy || !status?.sarvam_configured}
                    onClick={() => createClone(row.id)}
                  >
                    {busy === `create-${row.id}`
                      ? "Working…"
                      : ready
                        ? "Recreate clone (best sample)"
                        : "+ Create clone"}
                  </button>
                  <button
                    type="button"
                    className="btn vc-action-sec"
                    disabled={!!busy || !!loadingKey || !ready || !globalPreviewText.trim()}
                    onClick={() => playCloneVoice(row)}
                  >
                    {ttsLoading ? (
                      "Synthesizing…"
                    ) : ttsPlaying ? (
                      <>
                        <IconPause /> Pause clone voice
                      </>
                    ) : (
                      <>
                        <IconPlay /> Play clone voice
                      </>
                    )}
                  </button>
                  <button
                    type="button"
                    className="btn vc-action-sec"
                    disabled={!!busy || !ready}
                    onClick={() => {
                      if (window.confirm(`Delete Sarvam clone and clear mapping for ${row.display_name}?`)) {
                        patch(row.id, { clear_voice: true });
                      }
                    }}
                  >
                    Clear clone
                  </button>
                </div>
              </div>

              {samplesFor === row.id ? (
                <div className="vc-samples">
                  <div className="vc-samples-head">
                    <h4>Outbound samples</h4>
                    <button type="button" className="btn" onClick={() => setSamplesFor(null)}>
                      Hide
                    </button>
                  </div>
                  {!samples.length ? (
                    <p className="wa-hint">No suitable outbound samples (need ~8–22s on disk).</p>
                  ) : (
                    <ul className="vc-sample-list">
                      {samples.map((s) => {
                        const sKey = `sample-${row.id}-${s.message_id}`;
                        const sPlaying = playingKey === sKey;
                        const sLoading = loadingKey === sKey;
                        return (
                          <li key={s.message_id}>
                            <div className="vc-sample-meta">
                              <code>{s.duration_s}s</code>
                              <span>score {s.score}</span>
                              <span>msg #{s.message_id}</span>
                            </div>
                            <div className="wa-hint">{s.transcript_preview || "(no transcript)"}</div>
                            <div className="vc-sample-btns">
                              {s.media_url ? (
                                <button
                                  type="button"
                                  className="btn"
                                  disabled={!!busy || !!loadingKey}
                                  onClick={() => togglePlay(sKey, s.media_url)}
                                >
                                  {sLoading ? (
                                    "Loading…"
                                  ) : sPlaying ? (
                                    <>
                                      <IconPause /> Pause
                                    </>
                                  ) : (
                                    <>
                                      <IconPlay /> Play
                                    </>
                                  )}
                                </button>
                              ) : null}
                              <button
                                type="button"
                                className="btn primary"
                                disabled={!!busy}
                                onClick={() => createClone(row.id, s.message_id)}
                              >
                                Use this sample
                              </button>
                            </div>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>
              ) : null}
            </section>
          );
        })}
      </div>

      {status?.consent_note ? (
        <p className="vc-foot-note">{status.consent_note}</p>
      ) : null}
    </div>
  );
}
