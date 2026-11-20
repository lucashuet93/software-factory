import { useState, type FormEvent } from "react";
import { api } from "../api";
import { Gear } from "../Icons";

export default function NewProject() {
  const [form, setForm] = useState({ name: "", description: "", repo_url: "", pat: "", brd: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) setForm({ ...form, brd: await file.text() });
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const p = await api.createProject(form);
      window.location.hash = `#/p/${p.id}/settings`;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <section className="narrow">
      <div className="page-head">
        <div>
          <p className="eyebrow"><a href="#/">Factory floor</a> / new line</p>
          <h1><Gear size={26} /> New project</h1>
        </div>
      </div>
      <form onSubmit={submit} className="form panel">
        <label>Project name<input required value={form.name} onChange={set("name")} /></label>
        <label>Short description<input value={form.description} onChange={set("description")} /></label>
        <label>
          GitHub repository URL
          <input required placeholder="https://github.com/<you>/<empty-repo>" value={form.repo_url} onChange={set("repo_url")} />
          <span className="hint">An existing, empty repository owned by your personal GitHub account.</span>
        </label>
        <label>
          GitHub personal access token
          <input required type="password" autoComplete="off" value={form.pat} onChange={set("pat")} />
          <span className="hint">
            Classic PAT with the <code>repo</code> and <code>project</code> scopes. Stored locally and never shown again.
          </span>
        </label>
        <label>
          Initial BRD (Markdown)
          <textarea rows={14} value={form.brd} onChange={set("brd")}
            placeholder="Paste your business requirements, upload a Markdown file, or leave empty and build it with the agent in chat." />
          <input type="file" accept=".md,.markdown,.txt" onChange={onFile} />
        </label>
        {error && <div className="banner blocked">{error}</div>}
        <div className="row">
          <button className="primary" disabled={busy}>{busy ? "Creating..." : "Create project"}</button>
          <a className="button" href="#/">Cancel</a>
        </div>
      </form>
    </section>
  );
}
