import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Health, type Message, type ProjectDetail, type Task } from "../api";
import Markdown from "../Markdown";
import { Camera, Gear } from "../Icons";
import { CallAudio, CallStage, useCall, type Call } from "./Call";
import { deliveryLabel } from "./ProjectList";

const TABS = ["chat", "brd", "backlog", "settings"] as const;
type Tab = (typeof TABS)[number];
const TAB_LABEL: Record<Tab, string> = { chat: "Chat", brd: "BRD", backlog: "Backlog", settings: "Settings" };

function currentTab(): Tab {
  const t = window.location.hash.split("/")[3] as Tab;
  return TABS.includes(t) ? t : "chat";
}

export default function Workspace({ id }: { id: string }) {
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [thinking, setThinking] = useState(false);
  const [docVersion, setDocVersion] = useState(0);
  const [tab, setTab] = useState<Tab>(currentTab());
  const [error, setError] = useState("");
  const [voice, setVoice] = useState<Health["voice"] | null>(null);
  const call = useCall(id);

  const loadProject = useCallback(() => api.project(id).then(setProject).catch((e) => setError(e.message)), [id]);
  const loadMessages = useCallback(() => api.messages(id).then(setMessages), [id]);

  useEffect(() => {
    api.health().then((h) => setVoice(h.voice)).catch(() => setVoice({ ok: false, detail: "Server unreachable" }));
  }, []);

  useEffect(() => {
    const onHash = () => setTab(currentTab());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    loadProject();
    loadMessages();
    const es = new EventSource(`/api/projects/${id}/stream`);
    es.onmessage = (e) => {
      const ev = JSON.parse(e.data);
      if (ev.type === "deleted") {
        window.location.hash = "#/";
        return;
      }
      if (ev.type === "message") loadMessages();
      else if (ev.type === "agent") setThinking(ev.status === "thinking");
      else if (ev.type === "document") setDocVersion((v) => v + 1);
      loadProject();
    };
    return () => es.close();
  }, [id, loadProject, loadMessages]);

  if (error) return <div className="banner blocked">{error}</div>;
  if (!project) return <div className="loading"><Gear size={22} spin /> Loading project...</div>;

  return (
    <section>
      <div className="page-head">
        <div>
          <p className="eyebrow"><a href="#/">Factory floor</a> / {project.repo_owner}/{project.repo_name}</p>
          <h1><Gear size={26} spin={(project.task_counts.dispatched ?? 0) + (project.task_counts.reviewing ?? 0)
            + (project.task_counts.remediating ?? 0) > 0} /> {project.name}</h1>
          <p className="muted">{project.description}</p>
        </div>
        <div className="stack-right">
          <span className="pill">{deliveryLabel(project)}</span>
          {call.active && tab !== "chat" && (
            <a className="pill on-call" href={`#/p/${id}/chat`}><span className="lamp" /> On a call · return</a>
          )}
          <span className="muted small">
            {project.last_sync_at ? `Last GitHub sync ${new Date(project.last_sync_at).toLocaleTimeString()}` : "Not synced yet"}
          </span>
        </div>
      </div>
      {project.setup_status !== "ready" && (
        <div className={`banner ${project.setup_status === "blocked" ? "blocked" : "warn"}`}>
          {project.setup_status === "blocked" ? "Setup is blocked. " : "Setup has not run yet. "}
          <a href={`#/p/${id}/settings`}>Open settings</a> to run the connection checklist.
        </div>
      )}
      <BlockedBanner project={project} />
      <nav className="tabs">
        {TABS.map((t) => (
          <a key={t} href={`#/p/${id}/${t}`} className={tab === t ? "active" : ""}>{TAB_LABEL[t]}</a>
        ))}
      </nav>
      {tab === "chat" && (
        <Chat id={id} project={project} messages={messages} thinking={thinking} onSent={loadMessages}
          call={call} voice={voice} />
      )}
      {tab === "brd" && <Brd id={id} version={docVersion} />}
      {tab === "backlog" && <Backlog project={project} />}
      {tab === "settings" && <Settings project={project} onChange={loadProject} />}
      <CallAudio stream={call.audio} />
    </section>
  );
}

function BlockedBanner({ project }: { project: ProjectDetail }) {
  const blocked = project.tasks.filter((t) => t.state === "blocked");
  if (blocked.length === 0) return null;
  const needsYou = blocked.filter((t) => t.attempt >= project.max_attempts);
  const reworking = blocked.filter((t) => t.attempt < project.max_attempts);
  return (
    <div className={`banner ${needsYou.length ? "blocked" : "warn"}`}>
      {needsYou.length > 0 && (
        <div>
          <strong>Needs your input:</strong> {needsYou.map((t) => `${t.key} ${t.title}`).join(", ")} used all{" "}
          {project.max_attempts} Copilot attempts. <a href={`#/p/${project.id}/chat`}>Chat with the lead engineer</a>{" "}
          to agree on a rewrite or new tasks.
        </div>
      )}
      {reworking.length > 0 && (
        <div>
          <strong>Being reworked:</strong> {reworking.map((t) => `${t.key} (attempt ${t.attempt} of ${project.max_attempts})`).join(", ")}{" "}
          failed review. The lead engineer is rewriting the brief for a fresh attempt.
        </div>
      )}
    </div>
  );
}

function EventNote({ content }: { content: string }) {
  const [open, setOpen] = useState(false);
  const [summary, ...rest] = content.split("\n");
  const details = rest.join("\n").trim();
  return (
    <div className="msg event">
      <div className="who"><Gear size={12} /> Software Factory</div>
      <Markdown text={summary} />
      {details && (
        <>
          <button className="link small" onClick={() => setOpen(!open)}>{open ? "Hide details" : "Show details"}</button>
          {open && <Markdown text={details} />}
        </>
      )}
    </div>
  );
}

type ChatProps = {
  id: string;
  project: ProjectDetail;
  messages: Message[];
  thinking: boolean;
  onSent: () => void;
  call: Call;
  voice: Health["voice"] | null;
};

function Chat({ id, project, messages, thinking, onSent, call, voice }: ChatProps) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  // Scroll only the message list, so new messages never scroll the whole page past the header.
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messages, thinking]);

  async function send() {
    const content = text.trim();
    if (!content) return;
    if (call.active) {
      call.sendText(content);
      setText("");
      return;
    }
    setBusy(true);
    try {
      await api.sendMessage(id, content);
      setText("");
      onSent();
    } finally {
      setBusy(false);
    }
  }

  const callBlocked = !voice ? "Checking whether video calls are available..."
    : !voice.ok ? `Video calls are unavailable. ${voice.detail}`
      : project.call_active && !call.active ? "A call with this project is already open in another tab." : "";
  const lastCall = call.error || (call.endReason && call.endReason !== "Call ended" ? `Call ended: ${call.endReason}` : "");

  return (
    <div className={`chat panel ${call.active ? "in-call" : ""}`}>
      <div className="chat-bar">
        <span className="chat-title"><Gear size={14} /> Lead engineer</span>
        {!call.active && lastCall && <span className={`small ${call.error ? "error-text" : "muted"}`}>{lastCall}</span>}
        {!call.active && (
          <button className="call-btn" onClick={call.start} disabled={Boolean(callBlocked)}
            title={callBlocked || "Start a video call with the lead engineer"}>
            <Camera /> Video call
          </button>
        )}
      </div>
      <div className="chat-body">
        {call.active && <CallStage call={call} />}
        <div className="messages" ref={listRef}>
          {messages.length === 0 && (
            <div className="empty">
              <Gear size={48} teeth={10} />
              <p>Start by asking the lead engineer to review your BRD and propose a plan, or start a video call.</p>
            </div>
          )}
          {messages.map((m) => m.role === "event" ? <EventNote key={m.id} content={m.content} /> : (
            <div key={m.id} className={`msg ${m.role} ${m.channel === "call" ? "via-call" : ""}`}>
              <div className="who">
                {m.role === "user" ? "You" : <><Gear size={12} /> Lead engineer</>}
                {m.channel === "call" && <span className="call-tag"><Camera size={12} /> on call</span>}
              </div>
              <Markdown text={m.content} />
            </div>
          ))}
          {thinking && <div className="msg assistant thinking"><Gear size={18} spin /> Lead engineer is working...</div>}
        </div>
      </div>
      <div className="composer">
        <textarea rows={call.active ? 2 : 3} value={text}
          placeholder={call.active ? "Speak, or type a line to the lead engineer on the call" : "Message the lead engineer"}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
        <button className="primary" onClick={send} disabled={busy || !text.trim()}>Send</button>
      </div>
    </div>
  );
}

function Brd({ id, version }: { id: string; version: number }) {
  const [content, setContent] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    if (!editing) api.document(id, "BRD.md").then((d) => setContent(d.content)).catch(() => setContent(""));
  }, [id, version, editing]);

  async function save() {
    await api.saveDocument(id, "BRD.md", draft);
    setEditing(false);
  }

  if (content === null) return <div className="loading"><Gear size={22} spin /> Loading BRD...</div>;
  return (
    <div className="panel">
      <div className="row between">
        <h2>Business requirements</h2>
        {editing ? (
          <div className="row">
            <button className="primary" onClick={save}>Save</button>
            <button onClick={() => setEditing(false)}>Cancel</button>
          </div>
        ) : (
          <button onClick={() => { setDraft(content); setEditing(true); }}>Edit</button>
        )}
      </div>
      {editing ? <textarea className="editor" value={draft} onChange={(e) => setDraft(e.target.value)} />
        : <Markdown text={content || "_The BRD is empty. Describe your idea in chat._"} />}
    </div>
  );
}

const STATE_LABEL: Record<Task["state"], string> = {
  todo: "To do", dispatched: "Copilot coding", reviewing: "In review", remediating: "Fixing review findings",
  merged: "Merged", blocked: "Blocked",
};

function Backlog({ project }: { project: ProjectDetail }) {
  const repo = project.repo_url;
  return (
    <div className="grid2">
      <div className="panel">
        <div className="row between">
          <h2>Tasks</h2>
          <button onClick={() => api.refresh(project.id)}>Refresh from GitHub</button>
        </div>
        {!project.delivery_started && project.tasks.length > 0 && (
          <p className="hint">Delivery has not started. Tell the lead engineer to proceed when you are ready.</p>
        )}
        {project.tasks.length === 0 ? (
          <div className="empty"><Gear size={48} teeth={10} /><p>No tasks yet. Ask the lead engineer to plan the work.</p></div>
        ) : (
          <table>
            <thead><tr><th>Task</th><th>Status</th><th>GitHub</th><th>Detail</th></tr></thead>
            <tbody>
              {project.tasks.map((t) => (
                <tr key={t.key}>
                  <td><span className="task-key">{t.key}</span>{t.title}
                    {t.depends_on.length > 0 && <div className="muted small">after {t.depends_on.join(", ")}</div>}</td>
                  <td><span className={`pill state-${t.state}`}>{STATE_LABEL[t.state]}</span>
                    {(t.attempt > 1 || t.state === "blocked") && (
                      <div className="muted small">attempt {t.attempt} of {project.max_attempts}</div>)}
                    {t.remediation_count > 0 && <div className="muted small">{t.remediation_count} fix cycle(s)</div>}</td>
                  <td className="nowrap">
                    {t.issue_number && <a href={`${repo}/issues/${t.issue_number}`} target="_blank" rel="noreferrer">#{t.issue_number}</a>}
                    {t.pr_number && <> · <a href={`${repo}/pull/${t.pr_number}`} target="_blank" rel="noreferrer">PR #{t.pr_number}</a></>}
                    {t.abandoned_prs.length > 0 && (
                      <div className="muted small">abandoned {t.abandoned_prs.map((n, i) => (
                        <span key={n}>{i > 0 && ", "}<a href={`${repo}/pull/${n}`} target="_blank" rel="noreferrer">#{n}</a></span>
                      ))}</div>)}
                  </td>
                  <td className="small">{t.state === "blocked" ? t.blocked_reason : t.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="panel">
        <h2>Activity</h2>
        <ul className="activity">
          {[...project.events].reverse().map((e) => (
            <li key={e.id} className={`kind-${e.kind}`}>
              <span className="muted small">{new Date(e.created_at).toLocaleTimeString()}</span>{" "}
              {e.url ? <a href={e.url} target="_blank" rel="noreferrer">{e.message}</a> : e.message}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function Settings({ project, onChange }: { project: ProjectDetail; onChange: () => void }) {
  const [running, setRunning] = useState(false);
  const [pat, setPat] = useState("");
  const [limit, setLimit] = useState(project.parallel_limit);
  const [msg, setMsg] = useState("");

  async function setup() {
    setRunning(true);
    try { await api.runSetup(project.id); onChange(); } finally { setRunning(false); }
  }

  return (
    <div className="grid2">
      <div className="panel">
        <h2>Connection checklist</h2>
        {project.checklist.length === 0 && <p className="muted">Setup has not run yet. Running setup creates the
          main branch, a ruleset, labels, and the Projects board in your repository.</p>}
        <ul className="checklist">
          {project.checklist.map((c) => (
            <li key={c.key}><span className={`dot ${c.status}`} /> <strong>{c.label}</strong>
              {c.detail && <div className="small muted">{c.detail.startsWith("https://")
                ? <a href={c.detail} target="_blank" rel="noreferrer">{c.detail}</a> : c.detail}</div>}</li>
          ))}
        </ul>
        <button className="primary" onClick={setup} disabled={running}>
          <Gear size={16} spin={running} />
          {running ? "Running setup..." : project.checklist.length ? "Re-run setup" : "Run setup"}
        </button>
      </div>
      <div className="panel">
        <h2>Project</h2>
        <dl>
          <dt>Repository</dt><dd><a href={project.repo_url} target="_blank" rel="noreferrer">{project.repo_url}</a></dd>
          <dt>Board</dt><dd>{project.board_url ? <a href={project.board_url} target="_blank" rel="noreferrer">{project.board_url}</a>
            : project.board_mode === "labels" ? "Using issue labels (token cannot manage Projects)" : "Not created yet"}</dd>
          <dt>GitHub token</dt><dd>{project.pat_configured ? "Configured" : "Missing"}</dd>
          <dt>Delivery</dt><dd>{project.delivery_started ? "Started" : "Not started"}</dd>
        </dl>
        <label>Parallel task limit
          <div className="row">
            <input type="number" min={1} max={10} value={limit} onChange={(e) => setLimit(Number(e.target.value))} />
            <button onClick={async () => { await api.patchProject(project.id, { parallel_limit: limit }); setMsg("Saved"); onChange(); }}>Save</button>
          </div>
        </label>
        <label>Replace GitHub token
          <div className="row">
            <input type="password" autoComplete="off" value={pat} onChange={(e) => setPat(e.target.value)} />
            <button disabled={pat.length < 10} onClick={async () => { await api.updatePat(project.id, pat); setPat(""); setMsg("Token updated. Re-run setup."); onChange(); }}>Update</button>
          </div>
        </label>
        {msg && <p className="hint">{msg}</p>}
      </div>
      <DeleteProject project={project} />
    </div>
  );
}

function DeleteProject({ project }: { project: ProjectDetail }) {
  const [confirmName, setConfirmName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = (project.task_counts.dispatched ?? 0) + (project.task_counts.reviewing ?? 0)
    + (project.task_counts.remediating ?? 0);

  async function remove() {
    setBusy(true);
    setError("");
    try {
      await api.deleteProject(project.id);
      window.location.hash = "#/";
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <div className="panel danger">
      <h2>Delete project</h2>
      <p className="small">
        Permanently deletes this project's local data: chat history, BRD, memory, documents, task tracking, activity,
        and the stored GitHub token. Nothing on GitHub is deleted. The repository, board, issues, and pull requests
        stay as they are.
      </p>
      {active > 0 && (
        <p className="small warn-text">
          {active} task(s) are still in progress. Copilot sessions and CI already running on GitHub will continue,
          but Software Factory will stop reviewing and merging them.
        </p>
      )}
      <label><span>Type <code>{project.name}</code> to confirm</span>
        <div className="row">
          <input value={confirmName} onChange={(e) => setConfirmName(e.target.value)} autoComplete="off" />
          <button className="danger" disabled={busy || confirmName !== project.name} onClick={remove}>
            {busy ? "Deleting..." : "Delete project"}
          </button>
        </div>
      </label>
      {error && <div className="banner blocked">{error}</div>}
    </div>
  );
}
