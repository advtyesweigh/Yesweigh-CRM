import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";

type KbArticle = {
  id: string;
  title: string;
  question: string;
  answer: string;
  category: string;
  product: string;
  keywords: string;
  language: string;
  status: string;
  needs_review: boolean;
  usage_count: number;
  similar_to_id?: number | null;
  contradiction_of_id?: number | null;
  review_notes?: string;
  updated_at?: string | null;
  source_preview?: {
    conversation_id?: string | null;
    customer_name?: string;
    customer_phone?: string;
  };
  similar_article?: KbArticle | null;
  contradiction_article?: KbArticle | null;
};

type Stats = {
  approved: number;
  suggested: number;
  under_review: number;
  rejected: number;
  archived: number;
  needs_review: number;
  total: number;
};

const STATUSES = ["suggested", "under_review", "approved", "rejected", "archived"] as const;

export default function KnowledgeBase() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [items, setItems] = useState<KbArticle[]>([]);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState("suggested");
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<KbArticle | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [learnBusy, setLearnBusy] = useState(false);
  const [edit, setEdit] = useState({ title: "", question: "", answer: "", category: "other", notes: "" });

  const load = useCallback(async () => {
    setErr("");
    try {
      const [s, list] = await Promise.all([
        api<Stats>("/api/ai/kb/stats"),
        api<{ total: number; items: KbArticle[] }>(
          `/api/ai/kb/articles?status=${encodeURIComponent(status)}&limit=50${q.trim() ? `&q=${encodeURIComponent(q.trim())}` : ""}`,
        ),
      ]);
      setStats(s);
      setItems(list.items || []);
      setTotal(list.total || 0);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load knowledge base");
    }
  }, [status, q]);

  useEffect(() => {
    load().catch(() => null);
  }, [load]);

  async function openArticle(id: string) {
    setBusy(true);
    setErr("");
    try {
      const row = await api<KbArticle>(`/api/ai/kb/articles/${id}`);
      setSelected(row);
      setEdit({
        title: row.title || "",
        question: row.question || "",
        answer: row.answer || "",
        category: row.category || "other",
        notes: row.review_notes || "",
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to open article");
    } finally {
      setBusy(false);
    }
  }

  async function review(next: string) {
    if (!selected) return;
    setBusy(true);
    try {
      await api(`/api/ai/kb/articles/${selected.id}/review`, {
        method: "POST",
        body: JSON.stringify({
          status: next,
          notes: edit.notes,
          title: edit.title,
          question: edit.question,
          answer: edit.answer,
          category: edit.category,
        }),
      });
      setSelected(null);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Review failed");
    } finally {
      setBusy(false);
    }
  }

  async function runLearning() {
    setLearnBusy(true);
    setErr("");
    try {
      const res = await api<{ processed: number; created: number; skipped: number; failed: number }>(
        "/api/ai/learning/run",
        { method: "POST", body: JSON.stringify({ limit: 5, force: false }) },
      );
      await load();
      setErr(
        `Learning batch: processed ${res.processed}, created ${res.created}, skipped ${res.skipped}, failed ${res.failed}`,
      );
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Learning run failed");
    } finally {
      setLearnBusy(false);
    }
  }

  return (
    <div className="kb-page">
      <div className="panelhead" style={{ padding: "0 0 12px" }}>
        <div>
          <h2 style={{ margin: 0 }}>Knowledge Base</h2>
          <p style={{ margin: "4px 0 0" }}>
            AI suggestions from WhatsApp — approve before use in replies. Auto-publish is off.
          </p>
        </div>
        <button type="button" className="btn primary" disabled={learnBusy} onClick={() => runLearning()}>
          {learnBusy ? "Learning…" : "Run learning batch"}
        </button>
      </div>

      {stats ? (
        <div className="kb-stats">
          <div className="kb-stat"><b>{stats.approved}</b><span>Approved</span></div>
          <div className="kb-stat"><b>{stats.suggested}</b><span>Suggested</span></div>
          <div className="kb-stat"><b>{stats.under_review}</b><span>Under review</span></div>
          <div className="kb-stat"><b>{stats.needs_review}</b><span>Needs review</span></div>
          <div className="kb-stat"><b>{stats.rejected}</b><span>Rejected</span></div>
        </div>
      ) : null}

      <div className="kb-toolbar">
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          {STATUSES.map((s) => (
            <option key={s} value={s}>{s.replace(/_/g, " ")}</option>
          ))}
        </select>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search title / question / keywords"
          onKeyDown={(e) => {
            if (e.key === "Enter") load().catch(() => null);
          }}
        />
        <button type="button" className="btn" onClick={() => load()}>Search</button>
      </div>

      {err ? <div className="err" style={{ marginBottom: 10 }}>{err}</div> : null}

      <div className="kb-layout">
        <section className="panel kb-list">
          <div className="panelhead">
            <h2>{total} articles</h2>
          </div>
          <div className="kb-rows">
            {!items.length ? <p className="wa-hint" style={{ padding: 16 }}>No articles in this filter.</p> : null}
            {items.map((a) => (
              <button
                key={a.id}
                type="button"
                className={`kb-row ${selected?.id === a.id ? "on" : ""}`}
                onClick={() => openArticle(a.id)}
              >
                <b>{a.title || a.question || "Untitled"}</b>
                <span>{a.category} · {a.language}{a.needs_review ? " · review" : ""}</span>
              </button>
            ))}
          </div>
        </section>

        <section className="panel kb-detail">
          {!selected ? (
            <p className="wa-hint" style={{ padding: 20 }}>Select a suggestion to compare, edit, approve, or reject.</p>
          ) : (
            <div style={{ padding: 16 }}>
              <div className="kb-detail-meta">
                <span className={`kb-pill ${selected.status}`}>{selected.status}</span>
                {selected.needs_review ? <span className="kb-pill warn">needs review</span> : null}
                <span className="wa-hint">used {selected.usage_count || 0}×</span>
              </div>
              <label className="kb-label">Title</label>
              <input value={edit.title} onChange={(e) => setEdit((x) => ({ ...x, title: e.target.value }))} />
              <label className="kb-label">Question</label>
              <textarea rows={2} value={edit.question} onChange={(e) => setEdit((x) => ({ ...x, question: e.target.value }))} />
              <label className="kb-label">Suggested answer</label>
              <textarea rows={6} value={edit.answer} onChange={(e) => setEdit((x) => ({ ...x, answer: e.target.value }))} />
              <label className="kb-label">Category</label>
              <input value={edit.category} onChange={(e) => setEdit((x) => ({ ...x, category: e.target.value }))} />
              <label className="kb-label">Review notes</label>
              <input value={edit.notes} onChange={(e) => setEdit((x) => ({ ...x, notes: e.target.value }))} />

              {selected.source_preview?.conversation_id ? (
                <p className="wa-hint" style={{ marginTop: 10 }}>
                  Source: {selected.source_preview.customer_name || "—"}{" "}
                  {selected.source_preview.customer_phone || ""} · conv {selected.source_preview.conversation_id}
                </p>
              ) : null}

              {selected.similar_article ? (
                <div className="kb-compare">
                  <h4>Similar approved</h4>
                  <b>{selected.similar_article.title}</b>
                  <p>{selected.similar_article.answer}</p>
                </div>
              ) : null}
              {selected.contradiction_article ? (
                <div className="kb-compare warn">
                  <h4>Possible contradiction</h4>
                  <b>{selected.contradiction_article.title}</b>
                  <p>{selected.contradiction_article.answer}</p>
                </div>
              ) : null}

              <div className="kb-actions">
                <button type="button" className="btn primary" disabled={busy} onClick={() => review("approved")}>
                  Approve
                </button>
                <button type="button" className="btn" disabled={busy} onClick={() => review("under_review")}>
                  Under review
                </button>
                <button type="button" className="btn" disabled={busy} onClick={() => review("rejected")}>
                  Reject
                </button>
                <button type="button" className="btn" disabled={busy} onClick={() => review("archived")}>
                  Archive
                </button>
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
