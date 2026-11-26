import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";

type Suggestion = {
  id: string;
  intent: string;
  product: string;
  reply_text: string;
  reply_language: string;
  matched_knowledge_title: string;
  confidence: string;
  needs_handover: boolean;
  next_action: string;
  status: string;
  feedback: string;
  block_reason?: string;
};

type Props = {
  conversationId: string | null;
  onUseReply: (text: string) => void;
  onToast: (msg: string) => void;
};

export default function AiReplyAssistant({ conversationId, onUseReply, onToast }: Props) {
  const [suggestion, setSuggestion] = useState<Suggestion | null>(null);
  const [humanTakeover, setHumanTakeover] = useState(false);
  const [loading, setLoading] = useState(false);
  const [draft, setDraft] = useState("");
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    if (!conversationId) {
      setSuggestion(null);
      return;
    }
    try {
      const res = await api<{
        suggestion: Suggestion | null;
        human_takeover: boolean;
      }>(`/api/ai/whatsapp/conversations/${conversationId}/suggest-reply`);
      setSuggestion(res.suggestion);
      setHumanTakeover(!!res.human_takeover);
      setDraft(res.suggestion?.reply_text || "");
    } catch {
      /* keep quiet when endpoint not ready */
    }
  }, [conversationId]);

  useEffect(() => {
    setErr("");
    load().catch(() => null);
  }, [load]);

  async function generate(force = false) {
    if (!conversationId) return;
    setLoading(true);
    setErr("");
    try {
      const res = await api<{ ok: boolean; error?: string; suggestion?: Suggestion; handover?: boolean }>(
        `/api/ai/whatsapp/conversations/${conversationId}/suggest-reply`,
        { method: "POST", body: JSON.stringify({ force }) },
      );
      if (!res.ok) {
        setErr(res.error || "Could not suggest reply");
        return;
      }
      setSuggestion(res.suggestion || null);
      setDraft(res.suggestion?.reply_text || "");
      if (res.handover || res.suggestion?.needs_handover) {
        onToast("AI recommends human handover");
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Suggest failed");
    } finally {
      setLoading(false);
    }
  }

  async function saveEdit() {
    if (!suggestion) return;
    try {
      const row = await api<Suggestion>(`/api/ai/whatsapp/suggestions/${suggestion.id}`, {
        method: "PATCH",
        body: JSON.stringify({ reply_text: draft }),
      });
      setSuggestion(row);
      onToast("Suggestion updated");
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Update failed");
    }
  }

  async function feedback(kind: "helpful" | "incorrect") {
    if (!suggestion) return;
    try {
      const row = await api<Suggestion>(`/api/ai/whatsapp/suggestions/${suggestion.id}`, {
        method: "PATCH",
        body: JSON.stringify({ feedback: kind }),
      });
      setSuggestion(row);
      onToast(kind === "helpful" ? "Marked helpful" : "Marked incorrect");
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Feedback failed");
    }
  }

  async function toggleTakeover(active: boolean) {
    if (!conversationId) return;
    try {
      const res = await api<{ human_takeover: boolean }>(
        `/api/ai/whatsapp/conversations/${conversationId}/takeover`,
        { method: "POST", body: JSON.stringify({ active }) },
      );
      setHumanTakeover(!!res.human_takeover);
      onToast(active ? "Human takeover on" : "AI suggestions resumed");
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Takeover failed");
    }
  }

  if (!conversationId) return null;

  return (
    <section className="wa-card ai-reply-card">
      <div className="wa-card-head">
        <h4>AI Reply Assistant</h4>
        <button type="button" className="wa-text-btn" disabled={loading} onClick={() => generate(true)}>
          {loading ? "…" : suggestion ? "Regenerate" : "Suggest AI Reply"}
        </button>
      </div>

      {humanTakeover ? (
        <p className="wa-hint">Human takeover active — auto suggestions paused.</p>
      ) : null}
      {err ? <p className="err" style={{ margin: "0 0 8px" }}>{err}</p> : null}

      {suggestion ? (
        <>
          <div className="ai-reply-meta">
            <span>Intent: {suggestion.intent || "—"}</span>
            <span>Product: {suggestion.product || "—"}</span>
            <span>Confidence: {suggestion.confidence || "—"}</span>
          </div>
          {suggestion.matched_knowledge_title ? (
            <p className="wa-hint">Knowledge: {suggestion.matched_knowledge_title}</p>
          ) : null}
          {suggestion.needs_handover ? (
            <p className="wa-hint">Next: handover{suggestion.block_reason ? ` — ${suggestion.block_reason}` : ""}</p>
          ) : (
            <p className="wa-hint">Next: {suggestion.next_action || "suggest_send"}</p>
          )}
          <textarea
            className="ai-reply-draft"
            rows={4}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Recommended reply"
          />
          <div className="ai-reply-actions">
            <button type="button" className="btn primary" disabled={!draft.trim()} onClick={() => onUseReply(draft)}>
              Use in composer
            </button>
            <button type="button" className="btn" onClick={() => saveEdit()}>Save edit</button>
            <button type="button" className="btn" onClick={() => feedback("helpful")}>Helpful</button>
            <button type="button" className="btn" onClick={() => feedback("incorrect")}>Incorrect</button>
          </div>
        </>
      ) : (
        <p className="wa-hint">Generate a suggested reply from approved knowledge. Nothing is auto-sent.</p>
      )}

      <div className="ai-reply-actions" style={{ marginTop: 8 }}>
        <button type="button" className="btn" onClick={() => toggleTakeover(!humanTakeover)}>
          {humanTakeover ? "Clear takeover" : "Hand over (stop AI)"}
        </button>
      </div>
    </section>
  );
}
