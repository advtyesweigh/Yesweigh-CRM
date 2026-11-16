import { useEffect, useState } from "react";
import { api } from "../api/client";

type Work = { id: number; title: string; status: string; priority: string; description: string };
const empty = { title: "", description: "", status: "todo", priority: "medium" };

export default function Work() {
  const [rows, setRows] = useState<Work[]>([]);
  const [form, setForm] = useState(empty);
  async function load() {
    setRows(await api<Work[]>("/api/work"));
  }
  useEffect(() => {
    load().catch(console.error);
  }, []);
  async function create(e: React.FormEvent) {
    e.preventDefault();
    await api("/api/work", { method: "POST", body: JSON.stringify(form) });
    setForm(empty);
    await load();
  }
  return (
    <>
      <h1>Work management</h1>
      <div className="panel">
        <form onSubmit={create} className="grid2">
          <input placeholder="Task title" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} required />
          <select value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })}>
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
          </select>
          <textarea placeholder="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          <button type="submit">Add task</button>
        </form>
      </div>
      <div className="panel">
        <table>
          <thead><tr><th>Title</th><th>Status</th><th>Priority</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}><td>{r.title}</td><td>{r.status}</td><td>{r.priority}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
