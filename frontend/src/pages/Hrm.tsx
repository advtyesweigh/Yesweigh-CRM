import { useEffect, useState } from "react";
import { api } from "../api/client";

type Emp = { id: number; code: string; name: string; department: string; designation: string; status: string; adms_employee_code: string };
const empty = { code: "", name: "", department: "", designation: "", status: "active", adms_employee_code: "", phone: "", email: "", notes: "" };

export default function Hrm() {
  const [rows, setRows] = useState<Emp[]>([]);
  const [form, setForm] = useState(empty);
  async function load() {
    setRows(await api<Emp[]>("/api/employees"));
  }
  useEffect(() => {
    load().catch(console.error);
  }, []);
  async function create(e: React.FormEvent) {
    e.preventDefault();
    await api("/api/employees", { method: "POST", body: JSON.stringify(form) });
    setForm(empty);
    await load();
  }
  return (
    <>
      <h1>HRM</h1>
      <div className="panel">
        <form onSubmit={create} className="grid2">
          <input placeholder="Employee code" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} required />
          <input placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
          <input placeholder="Department" value={form.department} onChange={(e) => setForm({ ...form, department: e.target.value })} />
          <input placeholder="Designation" value={form.designation} onChange={(e) => setForm({ ...form, designation: e.target.value })} />
          <input placeholder="ADMS emp code" value={form.adms_employee_code} onChange={(e) => setForm({ ...form, adms_employee_code: e.target.value })} />
          <button type="submit">Add employee</button>
        </form>
      </div>
      <div className="panel">
        <table>
          <thead><tr><th>Code</th><th>Name</th><th>Dept</th><th>Role</th><th>ADMS</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}><td>{r.code}</td><td>{r.name}</td><td>{r.department}</td><td>{r.designation}</td><td>{r.adms_employee_code || "-"}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
