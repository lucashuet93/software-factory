import json
import re
import shutil
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    repo_owner TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    repo_node_id TEXT,
    board_mode TEXT,
    board_id TEXT,
    board_number INTEGER,
    board_url TEXT,
    status_field_id TEXT,
    status_options TEXT,
    setup_status TEXT NOT NULL DEFAULT 'draft',
    checklist TEXT,
    delivery_started INTEGER NOT NULL DEFAULT 0,
    parallel_limit INTEGER NOT NULL DEFAULT 3,
    last_sync_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    requirement_ids TEXT NOT NULL DEFAULT '[]',
    depends_on TEXT NOT NULL DEFAULT '[]',
    issue_number INTEGER,
    issue_node_id TEXT,
    board_item_id TEXT,
    state TEXT NOT NULL DEFAULT 'todo',
    pr_number INTEGER,
    head_sha TEXT,
    remediation_count INTEGER NOT NULL DEFAULT 0,
    awaiting_sha TEXT,
    review_requested_sha TEXT,
    blocked_reason TEXT,
    detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, key)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    url TEXT,
    created_at TEXT NOT NULL
);
"""

TASK_STATES = ("todo", "dispatched", "reviewing", "remediating", "merged", "blocked")
ACTIVE_STATES = ("dispatched", "reviewing", "remediating")
MESSAGE_ROLES = ("user", "assistant", "event")  # "event" = delivery notice from Software Factory, not the customer

# Task columns added after the first release; applied to new and existing databases alike.
_TASK_COLUMNS = (
    ("attempt", "INTEGER NOT NULL DEFAULT 1"),
    ("abandoned_prs", "TEXT NOT NULL DEFAULT '[]'"),
    ("attempt_notes", "TEXT NOT NULL DEFAULT '[]'"),
    ("deferred_findings", "TEXT NOT NULL DEFAULT '[]'"),
    ("attempt_report", "TEXT"),
    ("remediation_started_at", "TEXT"),
    ("follow_up_of", "TEXT"),  # key of the task whose deferred Low findings this task addresses
)
_JSON_TASK_FIELDS = ("requirement_ids", "depends_on", "abandoned_prs", "attempt_notes", "deferred_findings")

_DOC_NAME = re.compile(r"^(BRD\.md|memory\.md|documents/[A-Za-z0-9][A-Za-z0-9 _.-]{0,80}\.md)$")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class DocumentPathError(ValueError):
    pass


class Store:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.projects_dir = home / "projects"
        self.secrets_dir = home / "secrets"
        for d in (home, self.projects_dir, self.secrets_dir, home / "logs"):
            d.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(home / "factory.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        existing = {r["name"] for r in self._db.execute("PRAGMA table_info(tasks)")}
        for name, ddl in _TASK_COLUMNS:
            if name not in existing:
                self._db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
        self._db.commit()

    # ---------- generic helpers ----------
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(sql, params).fetchone()
            return dict(row) if row else None

    # ---------- projects ----------
    def create_project(self, name: str, description: str, repo_owner: str, repo_name: str, brd: str) -> dict:
        pid = uuid.uuid4().hex[:12]
        ts = now()
        self._exec(
            "INSERT INTO projects (id, name, description, repo_owner, repo_name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, name, description, repo_owner, repo_name, ts, ts),
        )
        pdir = self.project_dir(pid)
        (pdir / "documents").mkdir(parents=True, exist_ok=True)
        (pdir / "BRD.md").write_text(brd, encoding="utf-8")
        (pdir / "memory.md").write_text("# Project memory\n", encoding="utf-8")
        self._write_project_json(pid)
        return self.get_project(pid)

    def get_project(self, pid: str) -> dict | None:
        p = self._one("SELECT * FROM projects WHERE id = ?", (pid,))
        if p:
            p["status_options"] = json.loads(p["status_options"]) if p["status_options"] else {}
            p["checklist"] = json.loads(p["checklist"]) if p["checklist"] else []
            p["delivery_started"] = bool(p["delivery_started"])
        return p

    def list_projects(self) -> list[dict]:
        return [self.get_project(r["id"]) for r in self._all("SELECT id FROM projects ORDER BY created_at")]

    def update_project(self, pid: str, **fields: Any) -> dict:
        if not fields:
            return self.get_project(pid)
        for k in ("status_options", "checklist"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        if "delivery_started" in fields:
            fields["delivery_started"] = int(bool(fields["delivery_started"]))
        fields["updated_at"] = now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE projects SET {cols} WHERE id = ?", (*fields.values(), pid))
        self._write_project_json(pid)
        return self.get_project(pid)

    def project_dir(self, pid: str) -> Path:
        return self.projects_dir / pid

    def project_exists(self, pid: str) -> bool:
        return self._one("SELECT 1 AS x FROM projects WHERE id = ?", (pid,)) is not None

    def delete_project(self, pid: str) -> bool:
        """Remove all local data for a project. Returns False if it did not exist."""
        with self._lock:
            if not self.project_exists(pid):
                return False
            for table in ("messages", "tasks", "events"):
                self._db.execute(f"DELETE FROM {table} WHERE project_id = ?", (pid,))
            self._db.execute("DELETE FROM projects WHERE id = ?", (pid,))
            self._db.commit()
        self.secrets_dir.joinpath(f"{pid}.pat").unlink(missing_ok=True)
        pdir = self.project_dir(pid)
        if pdir.is_dir() and not pdir.is_symlink():
            shutil.rmtree(pdir)
        return True

    def _write_project_json(self, pid: str) -> None:
        p = self.get_project(pid)
        if not p or not self.project_dir(pid).is_dir():
            return
        public = {k: v for k, v in p.items() if k not in ("checklist",)}
        (self.project_dir(pid) / "project.json").write_text(json.dumps(public, indent=2), encoding="utf-8")

    # ---------- secrets ----------
    def set_pat(self, pid: str, pat: str) -> None:
        (self.secrets_dir / f"{pid}.pat").write_text(pat.strip(), encoding="utf-8")

    def get_pat(self, pid: str) -> str | None:
        f = self.secrets_dir / f"{pid}.pat"
        return f.read_text(encoding="utf-8").strip() if f.exists() else None

    # ---------- documents ----------
    def _doc_path(self, pid: str, name: str) -> Path:
        name = name.replace("\\", "/").strip()
        if not _DOC_NAME.match(name):
            raise DocumentPathError(
                "Document name must be BRD.md, memory.md, or documents/<name>.md"
            )
        root = self.project_dir(pid).resolve()
        path = (root / name).resolve()
        if root not in path.parents:
            raise DocumentPathError("Document path escapes the project directory")
        # Reject symlinks anywhere between the project root and the target.
        probe = root / name
        for part in [probe, *probe.parents]:
            if part == root:
                break
            if part.is_symlink():
                raise DocumentPathError("Symbolic links are not allowed in document paths")
        return path

    def read_document(self, pid: str, name: str) -> str | None:
        path = self._doc_path(pid, name)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def write_document(self, pid: str, name: str, content: str) -> None:
        path = self._doc_path(pid, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

    def list_documents(self, pid: str) -> list[str]:
        root = self.project_dir(pid)
        names = [n for n in ("BRD.md", "memory.md") if (root / n).exists()]
        docs = root / "documents"
        if docs.exists():
            names += [f"documents/{p.name}" for p in sorted(docs.glob("*.md"))]
        return names

    # ---------- messages ----------
    # Background threads (agent turns, the orchestrator) may finish after a project is deleted, so inserts for a
    # missing project are skipped rather than leaving orphaned rows.
    def add_message(self, pid: str, role: str, content: str) -> dict | None:
        if role not in MESSAGE_ROLES:
            raise ValueError(f"Unknown message role {role}")
        with self._lock:
            if not self.project_exists(pid):
                return None
            cur = self._exec(
                "INSERT INTO messages (project_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (pid, role, content, now()),
            )
            return self._one("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,))

    def list_messages(self, pid: str, limit: int | None = None) -> list[dict]:
        if limit:
            rows = self._all(
                "SELECT * FROM messages WHERE project_id = ? ORDER BY id DESC LIMIT ?", (pid, limit)
            )
            return list(reversed(rows))
        return self._all("SELECT * FROM messages WHERE project_id = ? ORDER BY id", (pid,))

    # ---------- tasks ----------
    def _task(self, row: dict | None) -> dict | None:
        if row:
            for k in _JSON_TASK_FIELDS:
                row[k] = json.loads(row[k])
        return row

    def next_task_key(self, pid: str) -> str:
        row = self._one("SELECT COUNT(*) AS n FROM tasks WHERE project_id = ?", (pid,))
        return f"T{row['n'] + 1}"

    def create_task(self, pid: str, title: str, body: str, requirement_ids: list[str], depends_on: list[str]) -> dict:
        with self._lock:
            if not self.project_exists(pid):
                raise ValueError("Project not found; it may have been deleted.")
            key = self.next_task_key(pid)
            ts = now()
            self._exec(
                "INSERT INTO tasks (project_id, key, title, body, requirement_ids, depends_on, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, key, title, body, json.dumps(requirement_ids), json.dumps(depends_on), ts, ts),
            )
            return self.get_task(pid, key)

    def get_task(self, pid: str, key: str) -> dict | None:
        return self._task(self._one("SELECT * FROM tasks WHERE project_id = ? AND key = ?", (pid, key)))

    def list_tasks(self, pid: str) -> list[dict]:
        return [self._task(r) for r in self._all("SELECT * FROM tasks WHERE project_id = ? ORDER BY id", (pid,))]

    def update_task(self, pid: str, key: str, **fields: Any) -> dict:
        for k in _JSON_TASK_FIELDS:
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        fields["updated_at"] = now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE tasks SET {cols} WHERE project_id = ? AND key = ?", (*fields.values(), pid, key))
        return self.get_task(pid, key)

    # ---------- events ----------
    def add_event(self, pid: str, kind: str, message: str, url: str | None = None) -> dict | None:
        with self._lock:
            if not self.project_exists(pid):
                return None
            cur = self._exec(
                "INSERT INTO events (project_id, kind, message, url, created_at) VALUES (?, ?, ?, ?, ?)",
                (pid, kind, message, url, now()),
            )
            return self._one("SELECT * FROM events WHERE id = ?", (cur.lastrowid,))

    def list_events(self, pid: str, limit: int = 100) -> list[dict]:
        rows = self._all("SELECT * FROM events WHERE project_id = ? ORDER BY id DESC LIMIT ?", (pid, limit))
        return list(reversed(rows))
