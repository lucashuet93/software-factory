import asyncio
import json
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import LeadAgent
from .bus import EventBus
from .config import Settings
from .orchestrator import MAX_ATTEMPTS, Orchestrator
from .store import DocumentPathError, Store
from .voice import CallManager

REPO_URL = re.compile(r"^https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")
FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


class NewProject(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    repo_url: str
    pat: str = Field(min_length=10)
    brd: str = ""


class ProjectPatch(BaseModel):
    parallel_limit: int | None = Field(default=None, ge=1, le=10)
    delivery_started: bool | None = None


class PatUpdate(BaseModel):
    pat: str = Field(min_length=10)


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


class DocumentIn(BaseModel):
    content: str


def create_app(settings: Settings, store: Store, bus: EventBus, orch: Orchestrator, agent: LeadAgent,
               start_worker: bool = True, is_shutting_down: Callable[[], bool] = lambda: False,
               calls: CallManager | None = None) -> FastAPI:
    foundry_status: dict = {"ok": False, "detail": "Not checked yet"}
    calls = calls or CallManager(settings, store, bus, agent)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async def check() -> None:
            ok, detail = await asyncio.to_thread(agent.check_connection)
            foundry_status.update(ok=ok, detail=detail)
        task = asyncio.create_task(check())
        if start_worker:
            orch.start()
        yield
        orch.stop()
        task.cancel()

    app = FastAPI(title="Software Factory", lifespan=lifespan)

    def project_or_404(pid: str) -> dict:
        p = store.get_project(pid)
        if not p:
            raise HTTPException(404, "Project not found")
        return p

    def public(p: dict) -> dict:
        tasks = store.list_tasks(p["id"])
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t["state"]] = counts.get(t["state"], 0) + 1
        return {**p, "pat_configured": store.get_pat(p["id"]) is not None,
                "repo_url": f"https://github.com/{p['repo_owner']}/{p['repo_name']}",
                "task_counts": counts, "max_attempts": MAX_ATTEMPTS, "call_active": calls.is_active(p["id"]),
                "needs_attention": p["setup_status"] == "blocked" or counts.get("blocked", 0) > 0}

    @app.get("/api/health")
    def health() -> dict:
        return {"foundry": foundry_status, "voice": calls.status(), "data_dir": str(settings.home)}

    @app.post("/api/health/foundry")
    def recheck_foundry() -> dict:
        ok, detail = agent.check_connection()
        foundry_status.update(ok=ok, detail=detail)
        return foundry_status

    @app.get("/api/projects")
    def list_projects() -> list[dict]:
        return [public(p) for p in store.list_projects()]

    @app.post("/api/projects", status_code=201)
    def create_project(body: NewProject) -> dict:
        m = REPO_URL.match(body.repo_url.strip())
        if not m:
            raise HTTPException(422, "Repository URL must look like https://github.com/<owner>/<repo>")
        p = store.create_project(body.name.strip(), body.description.strip(), m.group(1), m.group(2), body.brd)
        store.set_pat(p["id"], body.pat)
        store.add_event(p["id"], "project", "Project created")
        return public(p)

    @app.get("/api/projects/{pid}")
    def get_project(pid: str) -> dict:
        p = project_or_404(pid)
        return {**public(p), "tasks": store.list_tasks(pid), "events": store.list_events(pid),
                "documents": store.list_documents(pid)}

    @app.patch("/api/projects/{pid}")
    def patch_project(pid: str, body: ProjectPatch) -> dict:
        project_or_404(pid)
        fields = body.model_dump(exclude_none=True)
        p = store.update_project(pid, **fields)
        if body.delivery_started:
            orch.event(pid, "delivery", "Delivery started from settings")
            orch.wake()
        orch.notify(pid)
        return public(p)

    @app.put("/api/projects/{pid}/pat")
    def update_pat(pid: str, body: PatUpdate) -> dict:
        project_or_404(pid)
        store.set_pat(pid, body.pat)
        store.add_event(pid, "settings", "GitHub token updated")
        return {"pat_configured": True}

    @app.post("/api/projects/{pid}/setup")
    async def run_setup(pid: str) -> dict:
        project_or_404(pid)
        try:
            p = await asyncio.to_thread(orch.run_setup, pid)
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        return public(p)

    @app.delete("/api/projects/{pid}", status_code=204)
    async def delete_project(pid: str) -> None:
        project_or_404(pid)
        # Runs off the event loop because it may wait for an in-flight orchestrator tick to finish.
        await asyncio.to_thread(orch.delete_project, pid)

    @app.post("/api/projects/{pid}/refresh")
    def refresh(pid: str) -> dict:
        project_or_404(pid)
        orch.wake()
        return {"ok": True}

    @app.get("/api/projects/{pid}/messages")
    def messages(pid: str) -> list[dict]:
        project_or_404(pid)
        return store.list_messages(pid)

    @app.post("/api/projects/{pid}/messages", status_code=202)
    def post_message(pid: str, body: MessageIn) -> dict:
        project_or_404(pid)
        msg = store.add_message(pid, "user", body.content)
        bus.publish(pid, {"type": "message"})
        agent.chat_async(pid)
        return msg

    @app.websocket("/api/projects/{pid}/call")
    async def call(pid: str, websocket: WebSocket) -> None:
        await calls.handle(pid, websocket, is_shutting_down)

    @app.get("/api/projects/{pid}/documents/{name:path}")
    def read_document(pid: str, name: str) -> dict:
        project_or_404(pid)
        try:
            content = store.read_document(pid, name)
        except DocumentPathError as e:
            raise HTTPException(400, str(e)) from e
        if content is None:
            raise HTTPException(404, "Document not found")
        return {"name": name, "content": content}

    @app.put("/api/projects/{pid}/documents/{name:path}")
    def write_document(pid: str, name: str, body: DocumentIn) -> dict:
        project_or_404(pid)
        try:
            store.write_document(pid, name, body.content)
        except DocumentPathError as e:
            raise HTTPException(400, str(e)) from e
        store.add_event(pid, "document", f"Customer edited {name}")
        bus.publish(pid, {"type": "document", "name": name})
        return {"name": name}

    @app.get("/api/projects/{pid}/stream")
    async def stream(pid: str, request: Request) -> StreamingResponse:
        project_or_404(pid)
        q = bus.subscribe(pid)

        async def gen():
            idle = 0.0
            try:
                yield "event: ready\ndata: {}\n\n"
                # End promptly on shutdown; otherwise uvicorn waits forever for this open stream.
                while not is_shutting_down() and not await request.is_disconnected():
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=1)
                        idle = 0.0
                        yield f"data: {json.dumps(ev)}\n\n"
                    except TimeoutError:
                        idle += 1
                        if idle >= 15:
                            idle = 0.0
                            yield ": keepalive\n\n"
            finally:
                bus.unsubscribe(pid, q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    if FRONTEND_DIST.exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise HTTPException(404)
            # Asset names are content-hashed, so only index.html must revalidate to pick up new builds.
            return FileResponse(FRONTEND_DIST / "index.html", headers={"Cache-Control": "no-cache"})

    return app
