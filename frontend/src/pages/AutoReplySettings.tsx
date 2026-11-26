import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";

type Config = {
  mode: string;
  auto_send_enabled: boolean;
  emergency_stop: boolean;
  enabled_topics: string[];
  require_approved_kb: boolean;
  max_auto_replies_per_day: number;
  max_auto_replies_per_conversation: number;
};

type Row = {
  instance: {
    public_id: string;
    display_name: string;
    provider: string;
    connection_status: string;
  };
  config: Config;
};

const MODES = [
  { id: "disabled", label: "Disabled" },
  { id: "suggest", label: "Suggest Reply (default)" },
  { id: "rule_based", label: "Rule-Based" },
  { id: "hybrid", label: "Hybrid" },
] as const;

export default function AutoReplySettings() {
  const [items, setItems] = useState<Row[]>([]);
  const [err, setErr] = useState("");
  const [busyId, setBusyId] = useState("");
  const [analytics, setAnalytics] = useState<Record<string, unknown> | null>(null);

  const load = useCallback(async () => {
    setErr("");
    try {
      const [cfg, an] = await Promise.all([
        api<{ items: Row[] }>("/api/ai/auto-reply/configs"),
        api<Record<string, unknown>>("/api/ai/learning/analytics"),
      ]);
      setItems(cfg.items || []);
      setAnalytics(an);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load auto-reply config");
    }
  }, []);

  useEffect(() => {
    load().catch(() => null);
  }, [load]);

  async function patch(publicId: string, body: Partial<Config>) {
    setBusyId(publicId);
    setErr("");
    try {
      await api(`/api/ai/auto-reply/configs/${publicId}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Update failed");
    } finally {
      setBusyId("");
    }
  }

  return (
    <div className="ar-page">
      <div className="panelhead" style={{ padding: "0 0 12px" }}>
        <div>
          <h2 style={{ margin: 0 }}>Selective Auto-Reply</h2>
          <p style={{ margin: "4px 0 0" }}>
            Default is <b>Suggest Reply</b>. Customer auto-send stays <b>OFF</b> until you enable it per instance.
          </p>
        </div>
      </div>

      {err ? <div className="err" style={{ marginBottom: 10 }}>{err}</div> : null}

      {analytics ? (
        <div className="kb-stats">
          <div className="kb-stat"><b>{String((analytics.kb as { approved?: number } | undefined)?.approved ?? 0)}</b><span>KB approved</span></div>
          <div className="kb-stat"><b>{String(analytics.suggestions_total ?? 0)}</b><span>Suggestions</span></div>
          <div className="kb-stat"><b>{String(analytics.auto_replies_sent ?? 0)}</b><span>Auto-sent</span></div>
          <div className="kb-stat"><b>{String(analytics.handovers ?? 0)}</b><span>Handovers</span></div>
          <div className="kb-stat"><b>{String(analytics.corrections ?? 0)}</b><span>Corrections</span></div>
        </div>
      ) : null}

      <section className="panel">
        <div className="panelhead"><h2>Per WhatsApp instance</h2></div>
        <div className="ar-rows">
          {!items.length ? <p className="wa-hint" style={{ padding: 16 }}>No instances.</p> : null}
          {items.map((row) => {
            const c = row.config;
            const id = row.instance.public_id;
            const busy = busyId === id;
            return (
              <div key={id} className="ar-row">
                <div className="ar-row-head">
                  <div>
                    <b>{row.instance.display_name}</b>
                    <span className="wa-hint">
                      {" "}· {row.instance.provider} · {row.instance.connection_status}
                    </span>
                  </div>
                  {c.emergency_stop ? <span className="kb-pill warn">emergency stop</span> : null}
                </div>
                <div className="ar-grid">
                  <label>
                    Mode
                    <select
                      value={c.mode}
                      disabled={busy}
                      onChange={(e) => patch(id, { mode: e.target.value })}
                    >
                      {MODES.map((m) => (
                        <option key={m.id} value={m.id}>{m.label}</option>
                      ))}
                    </select>
                  </label>
                  <label className="ar-check">
                    <input
                      type="checkbox"
                      checked={!!c.auto_send_enabled}
                      disabled={busy || c.mode === "disabled" || c.mode === "suggest"}
                      onChange={(e) => patch(id, { auto_send_enabled: e.target.checked })}
                    />
                    Auto-send to customers
                    <span className="wa-hint"> (off while mode is Suggest)</span>
                  </label>
                  <label className="ar-check">
                    <input
                      type="checkbox"
                      checked={!!c.emergency_stop}
                      disabled={busy}
                      onChange={(e) => patch(id, { emergency_stop: e.target.checked })}
                    />
                    Emergency stop
                  </label>
                  <label className="ar-check">
                    <input
                      type="checkbox"
                      checked={!!c.require_approved_kb}
                      disabled={busy}
                      onChange={(e) => patch(id, { require_approved_kb: e.target.checked })}
                    />
                    Require approved KB
                  </label>
                </div>
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
