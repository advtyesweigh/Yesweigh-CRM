import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import ImportDeviceModal from "../components/ImportDeviceModal";

type Emp = {
  id: number;
  code: string;
  name: string;
  phone: string;
  email: string;
  department: string;
  designation: string;
  branch: string;
  join_date?: string | null;
  status: string;
  adms_employee_code?: string;
  account_status?: string;
  crm_role?: string;
};

type Page = {
  items: Emp[];
  total: number;
  page: number;
  page_size: number;
  stats: { total: number; active: number; present_today: number; absent_today: number; on_leave: number };
};

type Options = {
  departments: string[];
  branches: string[];
  statuses: string[];
  employment_types: string[];
  blood_groups: string[];
  roles: string[];
  languages: string[];
  document_types: string[];
};

type FormState = {
  name: string;
  phone: string;
  whatsapp: string;
  email: string;
  dob: string;
  gender: string;
  blood_group: string;
  aadhaar: string;
  pan: string;
  languages: string;
  address: string;
  state: string;
  district: string;
  city: string;
  pin: string;
  emergency_name: string;
  emergency_phone: string;
  emergency_relation: string;
  code: string;
  company: string;
  branch: string;
  department: string;
  designation: string;
  reporting_manager: string;
  join_date: string;
  employment_type: string;
  shift: string;
  working_hours: string;
  weekly_holiday: string;
  salary: string;
  probation_months: string;
  adms_employee_code: string;
  adms_device: string;
  status: string;
  enable_crm_login: boolean;
  login_email: string;
  temporary_password: string;
  password: string;
  crm_role: string;
  account_status: string;
  branch_access: string;
  lead_visibility: string;
  notes: string;
};

const emptyForm = (): FormState => ({
  name: "",
  phone: "",
  whatsapp: "",
  email: "",
  dob: "",
  gender: "",
  blood_group: "",
  aadhaar: "",
  pan: "",
  languages: "English",
  address: "",
  state: "",
  district: "",
  city: "",
  pin: "",
  emergency_name: "",
  emergency_phone: "",
  emergency_relation: "",
  code: "",
  company: "YesWeigh",
  branch: "",
  department: "",
  designation: "",
  reporting_manager: "",
  join_date: "",
  employment_type: "Permanent",
  shift: "General shift",
  working_hours: "",
  weekly_holiday: "",
  salary: "",
  probation_months: "",
  adms_employee_code: "",
  adms_device: "",
  status: "Active",
  enable_crm_login: false,
  login_email: "",
  temporary_password: "",
  password: "",
  crm_role: "Employee",
  account_status: "Invite pending",
  branch_access: "Assigned branch only",
  lead_visibility: "Assigned records only",
  notes: "",
});

const TABS = ["personal", "employment", "access", "documents"] as const;

function initials(name: string) {
  return name
    .split(" ")
    .map((p) => p[0])
    .filter(Boolean)
    .slice(0, 2)
    .join("")
    .toUpperCase() || "?";
}

function fmtDate(s?: string | null) {
  if (!s) return "—";
  const d = new Date(`${s}T12:00:00`);
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

function statusClass(status: string) {
  const s = status.toLowerCase();
  if (s.includes("inactive") || s.includes("terminat") || s.includes("resign")) return "inactive";
  if (s.includes("leave")) return "leave";
  return "";
}

export default function Employees({ embedded = false }: { embedded?: boolean }) {
  const nav = useNavigate();
  const [data, setData] = useState<Page | null>(null);
  const [opts, setOpts] = useState<Options | null>(null);
  const [q, setQ] = useState("");
  const [department, setDepartment] = useState("");
  const [branch, setBranch] = useState("");
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [toast, setToast] = useState("");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerMode, setDrawerMode] = useState<"add" | "edit" | "view">("add");
  const [editingId, setEditingId] = useState<number | null>(null);
  const [tab, setTab] = useState(0);
  const [form, setForm] = useState<FormState>(emptyForm());
  const [saving, setSaving] = useState(false);
  const [fetchAdms, setFetchAdms] = useState(false);
  const [fetchingAdms, setFetchingAdms] = useState(false);
  const [availableAdmsIds, setAvailableAdmsIds] = useState<string[]>([]);
  const [availableAdmsUsers, setAvailableAdmsUsers] = useState<{ employee_code: string; name: string }[]>([]);
  const [loadingAdmsIds, setLoadingAdmsIds] = useState(false);
  const [importModalOpen, setImportModalOpen] = useState(false);
  const [docType, setDocType] = useState("Resume / CV");
  const [docs, setDocs] = useState<{ id: number; doc_type: string; file_name: string; file_size: number; verification_status: string; created_at: string }[]>([]);

  async function load() {
    setLoading(true);
    setErr("");
    try {
      const params = new URLSearchParams({
        page: String(page),
        page_size: "20",
        sort: "display_order",
        order: "asc",
      });
      if (q.trim()) params.set("q", q.trim());
      if (department) params.set("department", department);
      if (branch) params.set("branch", branch);
      if (status) params.set("status", status);
      const res = await api<Page>(`/api/employees?${params}`);
      setData(res);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load employees");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    api<Options>("/api/employees/meta/options").then(setOpts).catch(console.error);
  }, []);

  useEffect(() => {
    load().catch(console.error);
  }, [page, department, branch, status]);

  useEffect(() => {
    const t = setTimeout(() => {
      if (page === 1) load().catch(console.error);
      else setPage(1);
    }, 300);
    return () => clearTimeout(t);
  }, [q]);

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(""), 3500);
  }

  async function loadAvailableAdmsIds() {
    setLoadingAdmsIds(true);
    try {
      const res = await api<{
        available: string[];
        available_users?: { employee_code: string; name: string }[];
      }>("/api/integrations/adms/available-employee-ids");
      setAvailableAdmsIds(res.available || []);
      setAvailableAdmsUsers(res.available_users || []);
    } catch (ex) {
      console.error(ex);
      setAvailableAdmsIds([]);
      setAvailableAdmsUsers([]);
      showToast(ex instanceof Error ? ex.message : "Failed to load ADMS IDs");
    } finally {
      setLoadingAdmsIds(false);
    }
  }

  function openAdd() {
    setDrawerMode("add");
    setEditingId(null);
    setForm(emptyForm());
    setDocs([]);
    setTab(0);
    setFetchAdms(false);
    setDrawerOpen(true);
    void loadAvailableAdmsIds();
  }

  async function lookupAdms(code?: string) {
    const admsId = (code ?? form.adms_employee_code).trim();
    if (!fetchAdms || !admsId) return;
    setFetchingAdms(true);
    try {
      const res = await api<{
        found: boolean;
        employee_code?: string;
        name?: string;
        phone?: string;
        email?: string;
        gender?: string;
        dob?: string | null;
        aadhaar?: string;
        whatsapp?: string;
        message?: string;
      }>(`/api/integrations/adms/employee/${encodeURIComponent(admsId)}`);
      if (!res.found) {
        showToast(res.message || "No ADMS employee found for that ID");
        return;
      }
      setForm((f) => {
        const nextAadhaar = (res.aadhaar || f.aadhaar || "").replace(/\D/g, "").slice(0, 12);
        const passwordStillDefault = !f.password || f.password === f.aadhaar;
        return {
          ...f,
          adms_employee_code: res.employee_code || admsId,
          name: res.name || f.name,
          phone: res.phone || f.phone,
          email: res.email || f.email,
          gender: res.gender || f.gender,
          dob: res.dob || f.dob,
          aadhaar: nextAadhaar,
          password: passwordStillDefault && nextAadhaar.length === 12 ? nextAadhaar : f.password,
          whatsapp: res.whatsapp || f.whatsapp,
        };
      });
      showToast("ADMS data loaded");
    } catch (ex) {
      showToast(ex instanceof Error ? ex.message : "ADMS lookup failed");
    } finally {
      setFetchingAdms(false);
    }
  }

  async function openEdit(id: number, mode: "edit" | "view") {
    const emp = await api<Record<string, unknown>>(`/api/employees/${id}`);
    setEditingId(id);
    setDrawerMode(mode);
    setForm({
      ...emptyForm(),
      name: String(emp.name || ""),
      phone: String(emp.phone || ""),
      whatsapp: String(emp.whatsapp || ""),
      email: String(emp.email || ""),
      dob: String(emp.dob || ""),
      gender: String(emp.gender || ""),
      blood_group: String(emp.blood_group || ""),
      aadhaar: String(emp.aadhaar || ""),
      pan: String(emp.pan || ""),
      languages: String(emp.languages || "English"),
      address: String(emp.address || ""),
      state: String(emp.state || ""),
      district: String(emp.district || ""),
      city: String(emp.city || ""),
      pin: String(emp.pin || ""),
      emergency_name: String(emp.emergency_name || ""),
      emergency_phone: String(emp.emergency_phone || ""),
      emergency_relation: String(emp.emergency_relation || ""),
      code: String(emp.code || ""),
      company: String(emp.company || "YesWeigh"),
      branch: String(emp.branch || ""),
      department: String(emp.department || ""),
      designation: String(emp.designation || ""),
      reporting_manager: String(emp.reporting_manager || ""),
      join_date: String(emp.join_date || ""),
      employment_type: String(emp.employment_type || "Permanent"),
      shift: String(emp.shift || "General shift"),
      working_hours: String(emp.working_hours || ""),
      weekly_holiday: String(emp.weekly_holiday || ""),
      salary: emp.salary != null ? String(emp.salary) : "",
      probation_months: emp.probation_months != null ? String(emp.probation_months) : "",
      adms_employee_code: String(emp.adms_employee_code || ""),
      adms_device: String(emp.adms_device || ""),
      status: String(emp.status || "Active"),
      enable_crm_login: Boolean(emp.user_id),
      login_email: String(emp.aadhaar || emp.email || ""),
      temporary_password: "",
      password: "",
      crm_role: String(emp.crm_role || "Employee"),
      account_status: String(emp.account_status || ""),
      branch_access: String(emp.branch_access || "Assigned branch only"),
      lead_visibility: String(emp.lead_visibility || "Assigned records only"),
      notes: String(emp.notes || ""),
    });
    try {
      setDocs(await api(`/api/employees/${id}/documents`));
    } catch {
      setDocs([]);
    }
    setTab(0);
    setDrawerOpen(true);
  }

  function setField<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((f) => {
      if (key === "aadhaar" && typeof value === "string") {
        const digits = value.replace(/\D/g, "").slice(0, 12);
        const passwordStillDefault = !f.password || f.password === f.aadhaar;
        const nextPassword = passwordStillDefault ? digits : f.password;
        return {
          ...f,
          aadhaar: digits,
          password: nextPassword,
          temporary_password: passwordStillDefault ? digits : f.temporary_password,
        };
      }
      if (key === "password" && typeof value === "string") {
        return { ...f, password: value, temporary_password: value };
      }
      if (key === "temporary_password" && typeof value === "string") {
        return { ...f, temporary_password: value, password: value };
      }
      return { ...f, [key]: value };
    });
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    const isAdd = drawerMode === "add";
    if (!form.name.trim() || !form.phone.trim()) {
      showToast("Name and phone are required");
      setTab(0);
      return;
    }
    const aadhaarDigits = form.aadhaar.replace(/\D/g, "");
    if (!/^\d{12}$/.test(aadhaarDigits)) {
      showToast("Aadhaar is required (exactly 12 digits)");
      setTab(0);
      return;
    }
    const loginPassword = (
      isAdd
        ? form.password || aadhaarDigits
        : form.password || form.temporary_password || ""
    ).trim();
    if (isAdd && loginPassword.length < 12) {
      showToast("Password must be at least 12 characters");
      return;
    }
    if (!isAdd && loginPassword && loginPassword.length < 12) {
      showToast("Password must be at least 12 characters");
      setTab(0);
      return;
    }
    if (!isAdd && (!form.branch || !form.department || !form.designation || !form.join_date)) {
      showToast("Fill required fields: branch, department, designation, joining date");
      setTab(1);
      return;
    }
    setSaving(true);
    try {
      const payload = {
        ...form,
        aadhaar: aadhaarDigits,
        code: form.code || null,
        salary: form.salary ? Number(form.salary) : null,
        probation_months: form.probation_months ? Number(form.probation_months) : null,
        dob: form.dob || null,
        join_date: form.join_date || null,
        password: loginPassword,
        temporary_password: loginPassword,
        login_email: aadhaarDigits,
        enable_crm_login: isAdd ? true : form.enable_crm_login || Boolean(loginPassword),
        crm_role: form.crm_role || "Employee",
      };
      if (drawerMode === "edit" && editingId) {
        await api(`/api/employees/${editingId}`, { method: "PATCH", body: JSON.stringify(payload) });
        showToast("Employee updated");
      } else {
        await api("/api/employees", { method: "POST", body: JSON.stringify(payload) });
        showToast("Employee saved");
      }
      setDrawerOpen(false);
      await load();
    } catch (ex) {
      showToast(ex instanceof Error ? ex.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function deactivate(id: number) {
    if (!confirm("Deactivate this employee? CRM login will be disabled.")) return;
    await api(`/api/employees/${id}/status`, { method: "PATCH", body: JSON.stringify({ status: "Inactive" }) });
    showToast("Employee deactivated");
    await load();
  }

  async function resetPassword(id: number) {
    const res = await api<{ temporary_password: string }>(`/api/employees/${id}/reset-password`, {
      method: "POST",
      body: JSON.stringify({ temporary_password: "" }),
    });
    showToast(`Temp password: ${res.temporary_password}`);
  }

  async function uploadDoc(file: File) {
    if (!editingId) {
      showToast("Save the employee first, then upload documents");
      return;
    }
    const fd = new FormData();
    fd.append("doc_type", docType);
    fd.append("file", file);
    await api(`/api/employees/${editingId}/documents`, { method: "POST", body: fd });
    setDocs(await api(`/api/employees/${editingId}/documents`));
    showToast("Document uploaded");
  }

  function exportCsv() {
    const rows = data?.items || [];
    const keys = ["code", "name", "phone", "department", "designation", "branch", "join_date", "status"] as const;
    const csv = [
      keys.join(","),
      ...rows.map((r) => keys.map((k) => `"${String(r[k] ?? "").split('"').join('""')}"`).join(",")),
    ].join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = "employees.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  }

  const pages = useMemo(() => Math.max(1, Math.ceil((data?.total || 0) / (data?.page_size || 20))), [data]);
  const readOnly = drawerMode === "view";

  return (
    <div className={embedded ? undefined : "emp-page"}>
      <ImportDeviceModal
        open={importModalOpen}
        onClose={() => setImportModalOpen(false)}
        onDone={() => {
          void load();
          void loadAvailableAdmsIds();
        }}
      />
      {!embedded && (
        <div className="heading">
          <div>
            <h1>Employees</h1>
            <p>Manage your staff, access permissions and employee records.</p>
          </div>
          <div className="actions">
            <button className="btn" type="button" onClick={exportCsv}>Export</button>
            <button className="btn" type="button" onClick={() => setImportModalOpen(true)}>
              Import from Device
            </button>
            <button className="btn primary" type="button" onClick={openAdd}>+ Add Employee</button>
          </div>
        </div>
      )}
      {embedded && (
        <div className="heading" style={{ marginBottom: 16 }}>
          <div>
            <h1 style={{ fontSize: 22, margin: 0 }}>Employees</h1>
            <p>Staff directory, permissions and records.</p>
          </div>
          <div className="actions">
            <button className="btn" type="button" onClick={exportCsv}>Export</button>
            <button className="btn" type="button" onClick={() => setImportModalOpen(true)}>
              Import from Device
            </button>
            <button className="btn primary" type="button" onClick={openAdd}>+ Add Employee</button>
          </div>
        </div>
      )}

      <div className="emp-stats">
        {[
          ["Total Employees", data?.stats.total ?? "…"],
          ["Active Employees", data?.stats.active ?? "…"],
          ["Present Today", data?.stats.present_today ?? "…"],
          ["Absent Today", data?.stats.absent_today ?? "…"],
          ["On Leave", data?.stats.on_leave ?? "…"],
        ].map(([label, value]) => (
          <div className="emp-stat" key={label}>
            <small>{label}</small>
            <strong>{typeof value === "number" ? String(value).padStart(2, "0") : value}</strong>
          </div>
        ))}
      </div>

      <section className="panel">
        {err && <div className="err" style={{ padding: "12px 16px" }}>{err}</div>}
        {loading && !data ? <div className="emp-empty">Loading employees…</div> : null}
        {!loading && data && data.items.length === 0 ? <div className="emp-empty">No employees match the filters.</div> : null}

        {data && data.items.length > 0 && (
          <div className="tablewrap">
            <table>
              <thead>
                <tr>
                  <th>Employee</th>
                  <th>Phone</th>
                  <th>Department / Role</th>
                  <th>Branch</th>
                  <th>Joined</th>
                  <th>Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((e, i) => (
                  <tr key={e.id}>
                    <td>
                      <div className="emp-person">
                        <div className="emp-avatar" style={{ background: ["#eaf0ff", "#e9f4ff", "#e9f2ed", "#f0edff"][i % 4] }}>
                          {initials(e.name)}
                        </div>
                        <div>
                          <b>{e.name}</b>
                          <small>{e.code}</small>
                        </div>
                      </div>
                    </td>
                    <td>{e.phone || "—"}</td>
                    <td>
                      <div>{e.department || "—"}</div>
                      <small style={{ color: "var(--muted)" }}>{e.designation || "—"}</small>
                    </td>
                    <td>{e.branch || "—"}</td>
                    <td>{fmtDate(e.join_date)}</td>
                    <td><span className={`emp-tag ${statusClass(e.status)}`}>● {e.status}</span></td>
                    <td>
                      <div className="emp-actions">
                        <button type="button" className="iconbtn" title="View profile" onClick={() => nav(`/employees/${e.id}`)}>👁</button>
                        <button type="button" className="iconbtn" title="Edit" onClick={() => openEdit(e.id, "edit")}>✎</button>
                        <button type="button" className="iconbtn" title="Reset password" onClick={() => resetPassword(e.id)}>🔑</button>
                        <button type="button" className="iconbtn" title="Deactivate" onClick={() => deactivate(e.id)}>⏻</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="emp-foot">
          <span>
            {data?.total
              ? `Showing ${(page - 1) * (data.page_size) + 1}–${Math.min(page * data.page_size, data.total)} of ${data.total} employees`
              : "0 employees"}
          </span>
          <div className="emp-page-btns">
            <button type="button" className="btn" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>‹</button>
            <button type="button" className="btn selected">{page}</button>
            <button type="button" className="btn" disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>›</button>
          </div>
        </div>
      </section>

      {drawerOpen && (
        <div className="emp-overlay emp-overlay--sheet" onClick={(e) => { if (e.target === e.currentTarget) setDrawerOpen(false); }}>
          <section className="emp-drawer emp-drawer--sheet" role="dialog" aria-modal="true">
            <div className="emp-drawer-head">
              <div>
                <h2>{drawerMode === "add" ? "Add Employee" : drawerMode === "view" ? "Employee Profile" : "Edit Employee"}</h2>
                <p>{drawerMode === "add" ? "Enter minimum staff details to create a profile." : "Update staff profile and CRM access."}</p>
              </div>
              <button type="button" className="iconbtn" aria-label="Close" onClick={() => setDrawerOpen(false)}>✕</button>
            </div>
            {drawerMode !== "add" && (
              <div className="emp-tabs">
                {["Personal details", "Employment", "Login & access", "Documents"].map((label, i) => (
                  <button key={label} type="button" className={tab === i ? "active" : ""} onClick={() => setTab(i)}>{label}</button>
                ))}
              </div>
            )}
            <form className="emp-form" onSubmit={save}>
              <div className="emp-formbody">
                {drawerMode === "add" && (
                  <div className="emp-fields">
                    <div className="field">
                      <label>ADMS ID</label>
                      <div className="emp-adms-row">
                        <select
                          value={form.adms_employee_code}
                          disabled={loadingAdmsIds}
                          onChange={(e) => {
                            const code = e.target.value;
                            setField("adms_employee_code", code);
                            if (fetchAdms && code) void lookupAdms(code);
                          }}
                        >
                          <option value="">
                            {loadingAdmsIds
                              ? "Loading ADMS IDs…"
                              : availableAdmsIds.length
                                ? "Select ADMS Emp Number"
                                : "No available ADMS IDs"}
                          </option>
                          {(availableAdmsUsers.length
                            ? availableAdmsUsers
                            : availableAdmsIds.map((id) => ({ employee_code: id, name: "" }))
                          ).map((u) => (
                            <option key={u.employee_code} value={u.employee_code}>
                              {u.name ? `${u.employee_code} — ${u.name}` : u.employee_code}
                            </option>
                          ))}
                        </select>
                        <label className="emp-adms-fetch" title="Fetch ADMS data">
                          <input
                            type="checkbox"
                            checked={fetchAdms}
                            onChange={(e) => {
                              const on = e.target.checked;
                              setFetchAdms(on);
                              if (on && form.adms_employee_code.trim()) void lookupAdms(form.adms_employee_code);
                            }}
                          />
                          Fetch
                          {fetchingAdms ? <span className="emp-adms-fetch-loading">…</span> : null}
                        </label>
                      </div>
                    </div>
                    <div className="field"><label>Name *</label><input required value={form.name} onChange={(e) => setField("name", e.target.value)} /></div>
                    <div className="field"><label>Phone *</label><input required value={form.phone} onChange={(e) => setField("phone", e.target.value)} /></div>
                    <div className="field"><label>Email</label><input type="email" value={form.email} onChange={(e) => setField("email", e.target.value)} /></div>
                    <div className="field"><label>DOB</label><input type="date" value={form.dob} onChange={(e) => setField("dob", e.target.value)} /></div>
                    <div className="field"><label>Gender</label>
                      <select value={form.gender} onChange={(e) => setField("gender", e.target.value)}>
                        <option value="">Optional</option>
                        <option>Male</option><option>Female</option><option>Other</option><option>Prefer not to say</option>
                      </select>
                    </div>
                    <div className="field">
                      <label>Aadhaar *</label>
                      <input
                        required
                        inputMode="numeric"
                        maxLength={12}
                        pattern="\d{12}"
                        placeholder="12-digit Aadhaar"
                        value={form.aadhaar}
                        onChange={(e) => setField("aadhaar", e.target.value)}
                        autoComplete="off"
                      />
                    </div>
                    <div className="field">
                      <label>Password *</label>
                      <input
                        required
                        type="text"
                        minLength={12}
                        placeholder="Defaults to Aadhaar"
                        value={form.password}
                        onChange={(e) => setField("password", e.target.value)}
                        autoComplete="new-password"
                      />
                    </div>
                    <div className="field">
                      <label>Access level *</label>
                      <select required value={form.crm_role} onChange={(e) => setField("crm_role", e.target.value)}>
                        {(opts?.roles || ["Employee"]).map((r) => (
                          <option key={r} value={r}>{r}</option>
                        ))}
                      </select>
                    </div>
                    <div className="field"><label>Alternative contact</label><input value={form.whatsapp} onChange={(e) => setField("whatsapp", e.target.value)} placeholder="WhatsApp or alternate number" /></div>
                  </div>
                )}

                {drawerMode !== "add" && tab === 0 && (
                  <div className="emp-fields">
                    <div className="field"><label>Full name *</label><input disabled={readOnly} required value={form.name} onChange={(e) => setField("name", e.target.value)} /></div>
                    <div className="field"><label>Mobile number *</label><input disabled={readOnly} required value={form.phone} onChange={(e) => setField("phone", e.target.value)} /></div>
                    <div className="field"><label>WhatsApp number</label><input disabled={readOnly} value={form.whatsapp} onChange={(e) => setField("whatsapp", e.target.value)} /></div>
                    <div className="field"><label>Email address</label><input disabled={readOnly} type="email" value={form.email} onChange={(e) => setField("email", e.target.value)} /></div>
                    <div className="field"><label>Date of birth</label><input disabled={readOnly} type="date" value={form.dob} onChange={(e) => setField("dob", e.target.value)} /></div>
                    <div className="field"><label>Gender</label>
                      <select disabled={readOnly} value={form.gender} onChange={(e) => setField("gender", e.target.value)}>
                        <option value="">Optional</option>
                        <option>Male</option><option>Female</option><option>Other</option><option>Prefer not to say</option>
                      </select>
                    </div>
                    <div className="field"><label>Blood group</label>
                      <select disabled={readOnly} value={form.blood_group} onChange={(e) => setField("blood_group", e.target.value)}>
                        <option value="">Optional</option>
                        {(opts?.blood_groups || []).map((b) => <option key={b}>{b}</option>)}
                      </select>
                    </div>
                    <div className="field"><label>Preferred language</label>
                      <select disabled={readOnly} value={form.languages} onChange={(e) => setField("languages", e.target.value)}>
                        {(opts?.languages || ["English"]).map((l) => <option key={l}>{l}</option>)}
                      </select>
                    </div>
                    <div className="field full"><label>Residential address</label><textarea disabled={readOnly} value={form.address} onChange={(e) => setField("address", e.target.value)} /></div>
                    <div className="field"><label>State</label><input disabled={readOnly} value={form.state} onChange={(e) => setField("state", e.target.value)} /></div>
                    <div className="field"><label>District</label><input disabled={readOnly} value={form.district} onChange={(e) => setField("district", e.target.value)} /></div>
                    <div className="field"><label>City</label><input disabled={readOnly} value={form.city} onChange={(e) => setField("city", e.target.value)} /></div>
                    <div className="field"><label>PIN code</label><input disabled={readOnly} value={form.pin} onChange={(e) => setField("pin", e.target.value)} /></div>
                    <div className="field">
                      <label>Aadhaar *</label>
                      <input
                        disabled={readOnly}
                        required
                        inputMode="numeric"
                        maxLength={12}
                        pattern="\d{12}"
                        placeholder="12-digit Aadhaar"
                        value={form.aadhaar}
                        onChange={(e) => setField("aadhaar", e.target.value)}
                        autoComplete="off"
                      />
                    </div>
                    <div className="field">
                      <label>Password</label>
                      <input
                        disabled={readOnly}
                        type="text"
                        minLength={12}
                        placeholder="Leave blank to keep · or set new (min 12)"
                        value={form.password}
                        onChange={(e) => setField("password", e.target.value)}
                        autoComplete="new-password"
                      />
                    </div>
                    <div className="field">
                      <label>Access level *</label>
                      <select disabled={readOnly} required value={form.crm_role} onChange={(e) => setField("crm_role", e.target.value)}>
                        {(opts?.roles || ["Employee"]).map((r) => (
                          <option key={r} value={r}>{r}</option>
                        ))}
                      </select>
                    </div>
                    <div className="field"><label>PAN</label><input disabled={readOnly} value={form.pan} onChange={(e) => setField("pan", e.target.value)} /></div>
                    <div className="field"><label>Emergency contact name</label><input disabled={readOnly} value={form.emergency_name} onChange={(e) => setField("emergency_name", e.target.value)} /></div>
                    <div className="field"><label>Emergency contact number</label><input disabled={readOnly} value={form.emergency_phone} onChange={(e) => setField("emergency_phone", e.target.value)} /></div>
                    <div className="field"><label>Relationship</label><input disabled={readOnly} value={form.emergency_relation} onChange={(e) => setField("emergency_relation", e.target.value)} /></div>
                  </div>
                )}

                {drawerMode !== "add" && tab === 1 && (
                  <div className="emp-fields">
                    <div className="field"><label>Employee ID</label><input disabled={readOnly} value={form.code} onChange={(e) => setField("code", e.target.value)} placeholder="Auto-generated if blank" /></div>
                    <div className="field"><label>Company</label><input disabled={readOnly} value={form.company} onChange={(e) => setField("company", e.target.value)} /></div>
                    <div className="field"><label>Branch *</label>
                      <select disabled={readOnly} required value={form.branch} onChange={(e) => setField("branch", e.target.value)}>
                        <option value="">Select branch</option>
                        {(opts?.branches || []).map((b) => <option key={b}>{b}</option>)}
                      </select>
                    </div>
                    <div className="field"><label>Department *</label>
                      <select disabled={readOnly} required value={form.department} onChange={(e) => setField("department", e.target.value)}>
                        <option value="">Select department</option>
                        {(opts?.departments || []).map((d) => <option key={d}>{d}</option>)}
                      </select>
                    </div>
                    <div className="field"><label>Designation *</label><input disabled={readOnly} required value={form.designation} onChange={(e) => setField("designation", e.target.value)} /></div>
                    <div className="field"><label>Reporting manager</label><input disabled={readOnly} value={form.reporting_manager} onChange={(e) => setField("reporting_manager", e.target.value)} /></div>
                    <div className="field"><label>Joining date *</label><input disabled={readOnly} required type="date" value={form.join_date} onChange={(e) => setField("join_date", e.target.value)} /></div>
                    <div className="field"><label>Employment type</label>
                      <select disabled={readOnly} value={form.employment_type} onChange={(e) => setField("employment_type", e.target.value)}>
                        {(opts?.employment_types || []).map((t) => <option key={t}>{t}</option>)}
                      </select>
                    </div>
                    <div className="field"><label>Working shift</label><input disabled={readOnly} value={form.shift} onChange={(e) => setField("shift", e.target.value)} /></div>
                    <div className="field"><label>Working hours</label><input disabled={readOnly} value={form.working_hours} onChange={(e) => setField("working_hours", e.target.value)} /></div>
                    <div className="field"><label>Weekly holiday</label><input disabled={readOnly} value={form.weekly_holiday} onChange={(e) => setField("weekly_holiday", e.target.value)} /></div>
                    <div className="field"><label>Monthly salary</label><input disabled={readOnly} type="number" min={0} value={form.salary} onChange={(e) => setField("salary", e.target.value)} /></div>
                    <div className="field"><label>Probation (months)</label><input disabled={readOnly} type="number" min={0} value={form.probation_months} onChange={(e) => setField("probation_months", e.target.value)} /></div>
                    <div className="field"><label>ADMS employee ID</label><input disabled={readOnly} value={form.adms_employee_code} onChange={(e) => setField("adms_employee_code", e.target.value)} /></div>
                    <div className="field"><label>Attendance device</label><input disabled={readOnly} value={form.adms_device} onChange={(e) => setField("adms_device", e.target.value)} /></div>
                    <div className="field"><label>Employment status</label>
                      <select disabled={readOnly} value={form.status} onChange={(e) => setField("status", e.target.value)}>
                        {(opts?.statuses || []).map((s) => <option key={s}>{s}</option>)}
                      </select>
                    </div>
                  </div>
                )}

                {drawerMode !== "add" && tab === 2 && (
                  <>
                    <label className="emp-switch">
                      <input disabled={readOnly} type="checkbox" checked={form.enable_crm_login} onChange={(e) => setField("enable_crm_login", e.target.checked)} />
                      Enable CRM Login
                    </label>
                    {form.enable_crm_login && (
                      <div className="emp-fields">
                        <div className="field"><label>Login username (Aadhaar / email)</label><input disabled={readOnly} value={form.login_email} onChange={(e) => setField("login_email", e.target.value)} placeholder="Usually full Aadhaar" /></div>
                        <div className="field"><label>Access level</label>
                          <select disabled={readOnly} value={form.crm_role} onChange={(e) => setField("crm_role", e.target.value)}>
                            {(opts?.roles || []).map((r) => <option key={r}>{r}</option>)}
                          </select>
                        </div>
                        <div className="field"><label>Password</label><input disabled={readOnly} type="password" minLength={12} value={form.temporary_password || form.password} onChange={(e) => setField("password", e.target.value)} placeholder="Min 12 chars · leave blank to keep" /></div>
                        <div className="field"><label>Account status</label>
                          <select disabled={readOnly} value={form.account_status} onChange={(e) => setField("account_status", e.target.value)}>
                            <option>Invite pending</option><option>Enabled</option><option>Disabled</option>
                          </select>
                        </div>
                        <div className="field"><label>Branch access</label>
                          <select disabled={readOnly} value={form.branch_access} onChange={(e) => setField("branch_access", e.target.value)}>
                            <option>Assigned branch only</option><option>All authorized branches</option>
                          </select>
                        </div>
                        <div className="field"><label>Lead visibility</label>
                          <select disabled={readOnly} value={form.lead_visibility} onChange={(e) => setField("lead_visibility", e.target.value)}>
                            <option>Assigned records only</option><option>Department records</option><option>Branch records</option><option>All company records</option>
                          </select>
                        </div>
                      </div>
                    )}
                    <div className="emp-notice">Prefer a temporary password. Require a password change on first login. Passwords are never stored in plain text.</div>
                  </>
                )}

                {drawerMode !== "add" && tab === 3 && (
                  <div>
                    <p style={{ color: "var(--muted)", marginBottom: 12, fontSize: 14 }}>Upload PDF, JPG or PNG. Documents are private and access-controlled.</p>
                    <div className="emp-fields" style={{ marginBottom: 12 }}>
                      <div className="field"><label>Document type</label>
                        <select value={docType} onChange={(e) => setDocType(e.target.value)}>
                          {(opts?.document_types || []).map((d) => <option key={d}>{d}</option>)}
                        </select>
                      </div>
                      <div className="field"><label>File</label>
                        <input type="file" accept=".pdf,.jpg,.jpeg,.png" disabled={readOnly} onChange={(e) => {
                          const f = e.target.files?.[0];
                          if (f) uploadDoc(f).catch((ex) => showToast(ex instanceof Error ? ex.message : "Upload failed"));
                        }} />
                      </div>
                    </div>
                    {docs.map((d) => (
                      <div className="emp-upload" key={d.id}>
                        <div>
                          <b>{d.doc_type}</b>
                          <small>{d.file_name} · {(d.file_size / 1024).toFixed(1)} KB · {d.verification_status}</small>
                        </div>
                      </div>
                    ))}
                    {docs.length === 0 && <div className="emp-empty">No documents uploaded yet.</div>}
                  </div>
                )}
              </div>
              <div className="emp-formfoot">
                <small>* Required fields</small>
                <div className="actions">
                  <button type="button" className="btn" onClick={() => setDrawerOpen(false)}>{readOnly ? "Close" : "Cancel"}</button>
                  {drawerMode !== "add" && tab > 0 && <button type="button" className="btn" onClick={() => setTab((t) => t - 1)}>Back</button>}
                  {drawerMode !== "add" && tab < TABS.length - 1 && <button type="button" className="btn" onClick={() => setTab((t) => t + 1)}>Next</button>}
                  {!readOnly && <button type="submit" className="btn primary" disabled={saving}>{saving ? "Saving…" : "Save Employee"}</button>}
                </div>
              </div>
            </form>
          </section>
        </div>
      )}

      {toast && <div className="emp-toast" role="status">{toast}</div>}
    </div>
  );
}
