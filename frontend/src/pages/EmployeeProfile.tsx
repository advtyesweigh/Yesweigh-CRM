import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";

type Emp = Record<string, unknown>;

const TABS = ["Overview", "Attendance", "Work", "Sales Performance", "Leave", "Documents", "Permissions", "Activity History"] as const;

function initials(name: string) {
  return name
    .split(" ")
    .map((p) => p[0])
    .filter(Boolean)
    .slice(0, 2)
    .join("")
    .toUpperCase() || "?";
}

export default function EmployeeProfile() {
  const { id } = useParams();
  const nav = useNavigate();
  const [emp, setEmp] = useState<Emp | null>(null);
  const [tab, setTab] = useState<(typeof TABS)[number]>("Overview");
  const [attendance, setAttendance] = useState<Record<string, unknown> | null>(null);
  const [performance, setPerformance] = useState<Record<string, unknown> | null>(null);
  const [activity, setActivity] = useState<{ activity_type: string; summary: string; created_at: string }[]>([]);
  const [docs, setDocs] = useState<{ id: number; doc_type: string; file_name: string; verification_status: string; created_at: string }[]>([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!id) return;
    api<Emp>(`/api/employees/${id}`)
      .then(setEmp)
      .catch((e) => setErr(e instanceof Error ? e.message : "Failed to load"));
    api(`/api/employees/${id}/attendance`).then(setAttendance).catch(() => null);
    api(`/api/employees/${id}/performance`).then(setPerformance).catch(() => null);
    api(`/api/employees/${id}/activity`).then(setActivity).catch(() => setActivity([]));
    api(`/api/employees/${id}/documents`).then(setDocs).catch(() => setDocs([]));
  }, [id]);

  if (err) return <div className="err">{err}</div>;
  if (!emp) return <div className="emp-empty">Loading profile…</div>;

  const name = String(emp.name || "");
  const isSales = Boolean((performance as { is_sales?: boolean } | null)?.is_sales);
  const sales = (performance as { sales?: Record<string, number> } | null)?.sales;
  const work = (performance as { work?: Record<string, number> } | null)?.work;
  const today = (attendance as { today?: Record<string, unknown>; month?: Record<string, number> } | null)?.today;
  const month = (attendance as { month?: Record<string, number> } | null)?.month;

  return (
    <div className="emp-page">
      <button type="button" className="btn" style={{ marginBottom: 14 }} onClick={() => nav("/settings?tab=employees")}>← Back to Settings · Employees</button>

      <div className="emp-profile-head panel">
        <div className="emp-person" style={{ gap: 16 }}>
          <div className="emp-avatar lg">{initials(name)}</div>
          <div>
            <h1 style={{ margin: 0, fontSize: 28 }}>{name}</h1>
            <p style={{ margin: "4px 0 0" }}>
              {String(emp.code || "")} · {String(emp.designation || "—")} · {String(emp.department || "—")} · {String(emp.branch || "—")}
            </p>
            <span className={`emp-tag ${String(emp.status).toLowerCase().includes("inactive") ? "inactive" : ""}`} style={{ marginTop: 8 }}>
              ● {String(emp.status)}
            </span>
          </div>
        </div>
        <button type="button" className="btn primary" onClick={() => nav("/settings?tab=employees")}>Edit in directory</button>
      </div>

      <div className="emp-profile-tabs">
        {TABS.filter((t) => t !== "Sales Performance" || isSales).map((t) => (
          <button key={t} type="button" className={tab === t ? "active" : ""} onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>

      <section className="panel" style={{ padding: 18 }}>
        {tab === "Overview" && (
          <div className="emp-fields">
            <div className="field"><label>Phone</label><div>{String(emp.phone || "—")}</div></div>
            <div className="field"><label>Email</label><div>{String(emp.email || "—")}</div></div>
            <div className="field"><label>Joining date</label><div>{String(emp.join_date || "—")}</div></div>
            <div className="field"><label>Employment type</label><div>{String(emp.employment_type || "—")}</div></div>
            <div className="field"><label>Reporting manager</label><div>{String(emp.reporting_manager || "—")}</div></div>
            <div className="field"><label>ADMS ID</label><div>{String(emp.adms_employee_code || "—")}</div></div>
            <div className="field"><label>Emergency contact</label><div>{String(emp.emergency_name || "—")} {emp.emergency_phone ? `· ${String(emp.emergency_phone)}` : ""}</div></div>
            <div className="field"><label>Address</label><div>{String(emp.address || "—")}</div></div>
          </div>
        )}

        {tab === "Attendance" && (
          <div className="emp-fields">
            <div className="field"><label>Today check-in</label><div>{String(today?.check_in ?? "—")}</div></div>
            <div className="field"><label>Today check-out</label><div>{String(today?.check_out ?? "—")}</div></div>
            <div className="field"><label>Present (month)</label><div>{month?.present ?? 0}</div></div>
            <div className="field"><label>Absent</label><div>{month?.absent ?? 0}</div></div>
            <div className="field"><label>Late</label><div>{month?.late ?? 0}</div></div>
            <div className="field"><label>Leave days</label><div>{month?.leave ?? 0}</div></div>
            <div className="field full"><label>Note</label><div style={{ color: "var(--muted)" }}>{String((attendance as { note?: string })?.note || "")}</div></div>
          </div>
        )}

        {tab === "Work" && (
          <div className="emp-fields">
            <div className="field"><label>Assigned</label><div>{work?.assigned ?? 0}</div></div>
            <div className="field"><label>Completed</label><div>{work?.completed ?? 0}</div></div>
            <div className="field"><label>Pending</label><div>{work?.pending ?? 0}</div></div>
            <div className="field"><label>Overdue</label><div>{work?.overdue ?? 0}</div></div>
          </div>
        )}

        {tab === "Sales Performance" && sales && (
          <div className="emp-fields">
            {Object.entries(sales).map(([k, v]) => (
              <div className="field" key={k}><label>{k.replaceAll("_", " ")}</label><div>{v}</div></div>
            ))}
          </div>
        )}

        {tab === "Leave" && <p style={{ color: "var(--muted)" }}>Leave balances and requests will connect to HR leave workflows in a later phase.</p>}

        {tab === "Documents" && (
          docs.length ? (
            <table>
              <thead><tr><th>Type</th><th>File</th><th>Status</th><th>Uploaded</th></tr></thead>
              <tbody>
                {docs.map((d) => (
                  <tr key={d.id}>
                    <td>{d.doc_type}</td>
                    <td>{d.file_name}</td>
                    <td>{d.verification_status}</td>
                    <td>{new Date(d.created_at).toLocaleDateString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p style={{ color: "var(--muted)" }}>No documents yet.</p>
        )}

        {tab === "Permissions" && (
          <div className="emp-fields">
            <div className="field"><label>CRM role</label><div>{String(emp.crm_role || "—")}</div></div>
            <div className="field"><label>Account status</label><div>{String(emp.account_status || "—")}</div></div>
            <div className="field"><label>Branch access</label><div>{String(emp.branch_access || "—")}</div></div>
            <div className="field"><label>Lead visibility</label><div>{String(emp.lead_visibility || "—")}</div></div>
          </div>
        )}

        {tab === "Activity History" && (
          activity.length ? (
            <table>
              <thead><tr><th>Type</th><th>Summary</th><th>When</th></tr></thead>
              <tbody>
                {activity.map((a, i) => (
                  <tr key={`${a.created_at}-${i}`}>
                    <td>{a.activity_type}</td>
                    <td>{a.summary}</td>
                    <td>{new Date(a.created_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p style={{ color: "var(--muted)" }}>No activity logged yet.</p>
        )}
      </section>
    </div>
  );
}
