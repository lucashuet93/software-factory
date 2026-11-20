export type ChecklistItem = { key: string; label: string; status: "ok" | "warn" | "blocked" | "pending"; detail: string };

export type Task = {
  key: string;
  title: string;
  state: "todo" | "dispatched" | "reviewing" | "remediating" | "merged" | "blocked";
  issue_number: number | null;
  pr_number: number | null;
  remediation_count: number;
  attempt: number;
  abandoned_prs: number[];
  deferred_findings: string[];
  depends_on: string[];
  detail: string | null;
  blocked_reason: string | null;
  updated_at: string;
};

export type ActivityEvent = { id: number; kind: string; message: string; url: string | null; created_at: string };

export type Project = {
  id: string;
  name: string;
  description: string;
  repo_owner: string;
  repo_name: string;
  repo_url: string;
  board_mode: "board" | "labels" | null;
  board_url: string | null;
  setup_status: "draft" | "ready" | "blocked";
  checklist: ChecklistItem[];
  delivery_started: boolean;
  parallel_limit: number;
  last_sync_at: string | null;
  pat_configured: boolean;
  task_counts: Record<string, number>;
  max_attempts: number;
  needs_attention: boolean;
  created_at: string;
};

export type ProjectDetail = Project & { tasks: Task[]; events: ActivityEvent[]; documents: string[] };
export type Message = { id: number; role: "user" | "assistant" | "event"; content: string; created_at: string };
export type Health = { foundry: { ok: boolean; detail: string }; data_dir: string };

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* keep status text */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  health: () => request<Health>("GET", "/api/health"),
  recheckFoundry: () => request<Health["foundry"]>("POST", "/api/health/foundry"),
  projects: () => request<Project[]>("GET", "/api/projects"),
  project: (id: string) => request<ProjectDetail>("GET", `/api/projects/${id}`),
  createProject: (body: { name: string; description: string; repo_url: string; pat: string; brd: string }) =>
    request<Project>("POST", "/api/projects", body),
  patchProject: (id: string, body: { parallel_limit?: number; delivery_started?: boolean }) =>
    request<Project>("PATCH", `/api/projects/${id}`, body),
  deleteProject: (id: string) => request<void>("DELETE", `/api/projects/${id}`),
  updatePat: (id: string, pat: string) => request<{ pat_configured: boolean }>("PUT", `/api/projects/${id}/pat`, { pat }),
  runSetup: (id: string) => request<Project>("POST", `/api/projects/${id}/setup`),
  refresh: (id: string) => request<{ ok: boolean }>("POST", `/api/projects/${id}/refresh`),
  messages: (id: string) => request<Message[]>("GET", `/api/projects/${id}/messages`),
  sendMessage: (id: string, content: string) => request<Message>("POST", `/api/projects/${id}/messages`, { content }),
  document: (id: string, name: string) =>
    request<{ name: string; content: string }>("GET", `/api/projects/${id}/documents/${name}`),
  saveDocument: (id: string, name: string, content: string) =>
    request<{ name: string }>("PUT", `/api/projects/${id}/documents/${name}`, { content }),
};
