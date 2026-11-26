import {
  fmtTime,
  isMediaPlaceholder,
  isUnsupportedPlaceholder,
  mediaKind,
  mediaPlaceholderLabel,
  voiceSpokenLanguageLabel,
  type WaMsg,
} from "../../lib/waMessageUtils";

function WaTicks({ status }: { status?: string }) {
  const s = (status || "").toLowerCase();
  if (s === "failed") return <span className="wa-tick failed" title="Failed">!</span>;
  if (s === "sending" || s === "queued") {
    return (
      <svg className="wa-ticks" viewBox="0 0 16 11" width="16" height="11" aria-label="Sending">
        <path
          d="M11.071.653a.457.457 0 0 0-.304-.102.493.493 0 0 0-.381.178l-6.19 7.636-2.405-2.272a.463.463 0 0 0-.336-.146.47.47 0 0 0-.349.146l-.39.397a.484.484 0 0 0 0 .694l3.127 2.96c.10.0.0.2.10.0.1.5.266.10.0.1.1 0 .27-.04.38-.13.03-.024.075-.061.11-.105L12.58 1.97a.497.497 0 0 0-.03-.695L11.07.653z"
          fill="currentColor"
          opacity=".5"
        />
      </svg>
    );
  }
  if (s === "sent") {
    return (
      <svg className="wa-ticks" viewBox="0 0 16 11" width="16" height="11" aria-label="Sent">
        <path
          d="M11.071.653a.457.457 0 0 0-.304-.102.493.493 0 0 0-.381.178l-6.19 7.636-2.405-2.272a.463.463 0 0 0-.336-.146.47.47 0 0 0-.349.146l-.39.397a.484.484 0 0 0 0 .694l3.127 2.96c.10.0.0.2.10.0.1.5.266.10.0.1.1 0 .27-.04.38-.13.03-.024.075-.061.11-.105L12.58 1.97a.497.497 0 0 0-.03-.695L11.07.653z"
          fill="currentColor"
        />
      </svg>
    );
  }
  const cls = s === "read" ? "wa-ticks read" : "wa-ticks";
  return (
    <svg className={cls} viewBox="0 0 16 11" width="16" height="11" aria-label={s === "read" ? "Read" : "Delivered"}>
      <path
        d="M11.071.653a.457.457 0 0 0-.304-.102.493.493 0 0 0-.381.178l-6.19 7.636-2.405-2.272a.463.463 0 0 0-.336-.146.47.47 0 0 0-.349.146l-.39.397a.484.484 0 0 0 0 .694l3.127 2.96c.10.0.0.2.10.0.1.5.266.10.0.1.1 0 .27-.04.38-.13.03-.024.075-.061.11-.105L12.58 1.97a.497.497 0 0 0-.03-.695L11.07.653z"
        fill="currentColor"
      />
      <path
        d="M14.6.653a.457.457 0 0 0-.304-.102.493.493 0 0 0-.381.178l-6.19 7.636-.91-.86a.47.47 0 0 0-.34-.14.48.48 0 0 0-.35.15l-.27.27a.48.48 0 0 0 0 .69l1.61 1.52c.10.0.0.2.10.0.1.5.27.10.0.0.2 0 .27-.04.38-.13.03-.02.07-.06.11-.1L15.11 1.97a.5.5 0 0 0-.03-.7L14.6.653z"
        fill="currentColor"
      />
    </svg>
  );
}

type Props = {
  m: WaMsg;
  onImageClick?: (src: string, name: string) => void;
  onRetryTranscribe?: (messageId: string) => void;
};

export default function WaMessageBubble({ m, onImageClick, onRetryTranscribe }: Props) {
  const rowKind = m.direction === "out" ? "out" : m.direction === "note" ? "note" : "in";
  const kind = mediaKind(m);
  const src = m.media_url || "";
  const unsupported = isUnsupportedPlaceholder(m.body, m.media_type);
  const showCaption = Boolean(m.body && !isMediaPlaceholder(m.body) && !unsupported);
  const voiceLang = kind === "audio" ? voiceSpokenLanguageLabel(m.spoken_language) : "";

  return (
    <div className={`wa-msg ${rowKind}`}>
      {m.sender_name && rowKind !== "in" ? <div className="wa-sender">{m.sender_name}</div> : null}
      <div
        className={`wa-bubble ${kind === "image" || kind === "sticker" || kind === "audio" || kind === "video" ? "has-media" : ""}`}
      >
        {m.direction === "note" ? <div className="wa-note-label">Internal note</div> : null}
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
            onClick={() => onImageClick?.(src, m.media_name || "Photo")}
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
            <span className="wa-voice-mic" aria-hidden>
              🎤
            </span>
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
                <p className="wa-transcript-status">{m.transcription_error || "Could not transcribe"}</p>
                {onRetryTranscribe ? (
                  <button type="button" className="wa-transcript-retry" onClick={() => onRetryTranscribe(m.id)}>
                    Retry
                  </button>
                ) : null}
              </div>
            ) : onRetryTranscribe ? (
              <button type="button" className="wa-transcript-retry" onClick={() => onRetryTranscribe(m.id)}>
                Transcribe
              </button>
            ) : null}
          </div>
        ) : null}
        {src && kind === "document" ? (
          <a className="wa-doc-card" href={src} target="_blank" rel="noreferrer">
            <span className="wa-doc-ico">📄</span>
            <span className="wa-doc-name">{m.media_name || "Document"}</span>
          </a>
        ) : null}
        {!src && m.media_name && kind === "document" ? (
          <div className="wa-attach">
            📎 {m.media_name}
          </div>
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
          <span>{fmtTime(m.created_at)}</span>
          {m.direction === "out" ? <WaTicks status={m.status} /> : null}
        </div>
      </div>
    </div>
  );
}