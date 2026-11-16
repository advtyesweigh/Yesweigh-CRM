import { useEffect, useState } from "react";
import { api } from "../api/client";

type AppRow = {
  name: string;
  cpu_percent: number;
  mem_bytes: number;
  disk_bytes: number;
  containers?: { name: string; cpu_percent: number; mem_bytes: number }[];
};

type ClamAv = {
  ok?: boolean;
  enabled?: boolean | null;
  skip_clamd?: boolean;
  scanning?: boolean;
  mem_bytes?: number;
  approx_save_bytes?: number;
  error?: string;
};

type Health = {
  ok: boolean;
  hostname: string;
  os: string;
  uptime_human: string;
  processes: number;
  agent_ok?: boolean;
  cpu: {
    percent: number;
    cores: number;
    model: string;
    load_1: number;
    load_5: number;
    load_15: number;
  };
  memory: {
    total_bytes: number;
    used_bytes: number;
    available_bytes: number;
    percent: number;
  };
  storage: {
    path: string;
    total_bytes: number;
    used_bytes: number;
    free_bytes: number;
    percent: number;
  };
  apps?: AppRow[];
  clamav?: ClamAv;
};

function fmtBytes(n: number) {
  if (!n || n < 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 10 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

function tone(pct: number) {
  if (pct >= 90) return "crit";
  if (pct >= 75) return "warn";
  return "ok";
}

function Gauge({
  label,
  percent,
  detail,
  sub,
}: {
  label: string;
  percent: number;
  detail: string;
  sub: string;
}) {
  const r = 54;
  const c = 2 * Math.PI * r;
  const clamped = Math.max(0, Math.min(100, percent));
  const offset = c - (clamped / 100) * c;
  const t = tone(clamped);
  return (
    <article className={`vh-card vh-${t}`}>
      <div className="vh-gauge">
        <svg viewBox="0 0 140 140" aria-hidden="true">
          <circle className="vh-track" cx="70" cy="70" r={r} />
          <circle
            className="vh-arc"
            cx="70"
            cy="70"
            r={r}
            strokeDasharray={c}
            strokeDashoffset={offset}
            transform="rotate(-90 70 70)"
          />
        </svg>
        <div className="vh-gauge-val">
          <b>{clamped.toFixed(1)}%</b>
          <span>{label}</span>
        </div>
      </div>
      <div className="vh-meta">
        <strong>{detail}</strong>
        <span>{sub}</span>
      </div>
    </article>
  );
}

const POLL_MS = 12_000;

export default function VpsHealth() {
  const [data, setData] = useState<Health | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [clamBusy, setClamBusy] = useState(false);
  const [clamMsg, setClamMsg] = useState("");

  useEffect(() => {
    let alive = true;
    let inFlight = false;
    let timer = 0;

    async function load() {
      if (inFlight) return;
      inFlight = true;
      try {
        const h = await api<Health>("/api/admin/vps-health");
        if (!alive) return;
        setData(h);
        setErr("");
      } catch (e) {
        if (!alive) return;
        setErr(e instanceof Error ? e.message : String(e));
      } finally {
        inFlight = false;
        if (alive) setLoading(false);
      }
    }

    function schedule() {
      timer = window.setTimeout(async () => {
        await load();
        if (alive) schedule();
      }, POLL_MS);
    }

    load().then(() => {
      if (alive) schedule();
    });
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, []);

  async function toggleClamAv(next: boolean) {
    setClamBusy(true);
    setClamMsg(next ? "Starting ClamAV…" : "Stopping ClamAV to free RAM…");
    try {
      const r = await api<ClamAv>("/api/admin/vps-health/clamav", {
        method: "POST",
        body: JSON.stringify({ enabled: next }),
      });
      setData((prev) => (prev ? { ...prev, clamav: r } : prev));
      setClamMsg(next ? "ClamAV enabled" : "ClamAV off · ~1.1 GB RAM freed");
      window.setTimeout(() => setClamMsg(""), 4000);
    } catch (e) {
      setClamMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setClamBusy(false);
    }
  }

  if (loading && !data) {
    return (
      <section className="panel vh-panel">
        <p className="muted">Loading VPS metrics…</p>
      </section>
    );
  }

  if (err && !data) {
    return (
      <section className="panel vh-panel">
        <p className="err">{err}</p>
      </section>
    );
  }

  if (!data) return null;

  const clamOn = Boolean(data.clamav?.enabled);
  const apps = data.apps || [];

  return (
    <section className="panel vh-panel">
      <div className="vh-head">
        <div>
          <h2>VPS Health</h2>
          <p>
            Live host metrics · refreshes every 12s
            {err ? <span className="err"> · {err}</span> : null}
          </p>
        </div>
        <div className="vh-pill">
          <span className="dot" /> {data.hostname}
        </div>
      </div>

      <div className="vh-info">
        <div>
          <span>OS</span>
          <b>{data.os}</b>
        </div>
        <div>
          <span>Uptime</span>
          <b>{data.uptime_human}</b>
        </div>
        <div>
          <span>CPU</span>
          <b>
            {data.cpu.cores} cores · {data.cpu.model}
          </b>
        </div>
        <div>
          <span>Load</span>
          <b>
            {data.cpu.load_1.toFixed(2)} / {data.cpu.load_5.toFixed(2)} / {data.cpu.load_15.toFixed(2)}
          </b>
        </div>
        <div>
          <span>Processes</span>
          <b>{data.processes}</b>
        </div>
      </div>

      <div className="vh-grid">
        <Gauge
          label="Processor"
          percent={data.cpu.percent}
          detail={`${data.cpu.cores} cores active`}
          sub={`Load 1m ${data.cpu.load_1.toFixed(2)}`}
        />
        <Gauge
          label="Memory"
          percent={data.memory.percent}
          detail={`${fmtBytes(data.memory.used_bytes)} / ${fmtBytes(data.memory.total_bytes)}`}
          sub={`Available ${fmtBytes(data.memory.available_bytes)}`}
        />
        <Gauge
          label="Storage"
          percent={data.storage.percent}
          detail={`${fmtBytes(data.storage.used_bytes)} / ${fmtBytes(data.storage.total_bytes)}`}
          sub={`Free ${fmtBytes(data.storage.free_bytes)} · ${data.storage.path}`}
        />
      </div>

      <div className="vh-control">
        <div>
          <strong>ClamAV (Mailcow)</strong>
          <p>
            Mail antivirus · uses ~{fmtBytes(data.clamav?.mem_bytes || data.clamav?.approx_save_bytes || 0)} when on.
            Turn off to free ~1.1 GB RAM.
          </p>
          {clamMsg ? <p className={clamMsg.toLowerCase().includes("error") || clamMsg.includes("502") ? "err" : "muted"}>{clamMsg}</p> : null}
          {data.clamav?.error ? <p className="err">{data.clamav.error}</p> : null}
        </div>
        <label className={`vh-switch ${clamBusy ? "busy" : ""}`}>
          <input
            type="checkbox"
            checked={clamOn}
            disabled={clamBusy || data.clamav?.enabled == null}
            onChange={(e) => toggleClamAv(e.target.checked)}
          />
          <span className="vh-slider" />
          <span className="vh-switch-label">{clamBusy ? "…" : clamOn ? "On" : "Off"}</span>
        </label>
      </div>

      <div className="vh-apps-head">
        <h3>App consumption</h3>
        <p>CPU · RAM · storage by service</p>
      </div>
      {!data.agent_ok && (
        <p className="muted" style={{ marginBottom: 10 }}>
          Host agent offline — app split unavailable until agent is running.
        </p>
      )}
      {apps.length > 0 && (
        <div className="vh-apps">
          <table>
            <thead>
              <tr>
                <th>App</th>
                <th>CPU</th>
                <th>RAM</th>
                <th>Storage</th>
              </tr>
            </thead>
            <tbody>
              {apps.map((a) => (
                <tr key={a.name}>
                  <td>
                    <b>{a.name}</b>
                    {a.containers && a.containers.length > 0 && (
                      <small>{a.containers.length} unit{a.containers.length === 1 ? "" : "s"}</small>
                    )}
                  </td>
                  <td>{a.cpu_percent.toFixed(1)}%</td>
                  <td>{fmtBytes(a.mem_bytes)}</td>
                  <td>{fmtBytes(a.disk_bytes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
