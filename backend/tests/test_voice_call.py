import asyncio
import json
import sqlite3
import time

from fastapi.testclient import TestClient

from software_factory import agent as agent_mod
from software_factory.agent import EVENT_PREFIX, LeadAgent
from software_factory.api import create_app
from software_factory.bus import EventBus
from software_factory.config import Settings
from software_factory.orchestrator import Orchestrator
from software_factory.store import Store
from software_factory.voice import CallManager

ICE = [{"urls": ["turn:relay.example:3478"], "username": "u", "credential": "c"}]


class FakeVoiceLive:
    """Stands in for the Voice Live WebSocket. Created on the app's event loop; the test thread emits through it."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["type"] == "session.update":
            await self.queue.put({"type": "session.updated", "session": {"avatar": {"ice_servers": ICE}}})

    def emit(self, event: dict) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, event)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        event = await self.queue.get()
        if event is None:
            raise StopAsyncIteration
        return json.dumps(event)

    async def close(self) -> None:
        self.closed = True
        await self.queue.put(None)


def make_env(tmp_path):
    settings = Settings(project_endpoint="https://res.services.ai.azure.com/api/projects/p", model_deployment="m",
                        tenant_id="", agent_name="t", home=tmp_path, log_level="INFO", poll_seconds=60,
                        host="127.0.0.1", port=0)
    store, bus = Store(tmp_path), EventBus()
    orch = Orchestrator(store, bus, 60)
    lead = LeadAgent(settings, store, orch, bus)
    lead.tool_calls = []
    lead.run_tool = lambda pid, name, args, customer_turn=False: (
        lead.tool_calls.append((name, customer_turn)) or {"ok": True, "tasks": []})
    lead.chats = []
    lead.chat_async = lead.chats.append
    upstreams: list[FakeVoiceLive] = []

    async def connect(url: str, token: str) -> FakeVoiceLive:
        upstreams.append(FakeVoiceLive())
        return upstreams[-1]

    async def token() -> str:
        return "token"

    calls = CallManager(settings, store, bus, lead, connect=connect, token=token)
    app = create_app(settings, store, bus, orch, lead, start_worker=False, calls=calls)
    p = store.create_project("demo", "", "o", "r", "# BRD\nFR-001: Say hello.")
    store.add_message(p["id"], "user", "Please plan the work.")
    return TestClient(app), store, lead, calls, upstreams, p["id"]


def receive(ws, kind: str) -> dict:
    while True:
        msg = ws.receive_json()
        if msg["type"] == kind:
            return msg


def wait_for(check, timeout: float = 5.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_call_configures_session_runs_tools_and_records_transcript(tmp_path):
    client, store, lead, _, upstreams, pid = make_env(tmp_path)
    with client.websocket_connect(f"/api/projects/{pid}/call") as ws:
        assert receive(ws, "ice")["servers"] == ICE
        up = upstreams[0]
        session = up.sent[0]["session"]
        assert "get_project_status" in [t["name"] for t in session["tools"]]
        assert session["avatar"]["character"] == "lisa"
        assert "Please plan the work." in session["instructions"]  # earlier chat carries into the call

        ws.send_json({"type": "text", "text": "What's the status?"})
        wait_for(lambda: up.sent[-1]["type"] == "response.create")
        up.emit({"type": "response.created"})
        up.emit({"type": "response.done", "response": {"status": "completed", "output": [
            {"type": "function_call", "name": "get_project_status", "call_id": "c1", "arguments": "{}"}]}})
        assert receive(ws, "tool") == {"type": "tool", "name": "get_project_status", "state": "running"}
        wait_for(lambda: up.sent[-1]["type"] == "response.create")
        output = next(m for m in up.sent if m.get("item", {}).get("type") == "function_call_output")
        assert output["item"]["call_id"] == "c1" and json.loads(output["item"]["output"])["ok"] is True
        up.emit({"type": "response.audio_transcript.done", "transcript": "Nothing is open yet."})
        assert receive(ws, "caption")["text"] == "Nothing is open yet."
        ws.send_json({"type": "hangup"})

    assert lead.tool_calls == [("get_project_status", True)]
    msgs = store.list_messages(pid)
    assert [(m["role"], m["channel"], m["content"]) for m in msgs[1:]] == [
        ("user", "call", "What's the status?"), ("assistant", "call", "Nothing is open yet.")]
    # The text agent never answers what the call already handled.
    assert agent_mod.pending_messages(msgs, seen_id=msgs[0]["id"]) == []
    wait_for(lambda: upstreams[0].closed)


def test_delivery_events_go_to_the_call_while_it_is_open(tmp_path):
    client, store, lead, calls, upstreams, pid = make_env(tmp_path)
    with client.websocket_connect(f"/api/projects/{pid}/call") as ws:
        receive(ws, "ice")
        store.add_message(pid, "event", "T2 is blocked after attempt 3.")
        calls.route_event(pid)
        wait_for(lambda: upstreams[0].sent[-1]["type"] == "response.create")
        injected = [m for m in upstreams[0].sent if m["type"] == "conversation.item.create"]
        assert injected[-1]["item"]["content"][0]["text"] == f"{EVENT_PREFIX}\nT2 is blocked after attempt 3."
        ws.send_json({"type": "hangup"})
    assert lead.chats == []
    wait_for(lambda: not calls.is_active(pid))
    calls.route_event(pid)
    assert lead.chats == [pid]


def test_only_one_call_per_project(tmp_path):
    client, *_, pid = make_env(tmp_path)
    with client.websocket_connect(f"/api/projects/{pid}/call") as first:
        receive(first, "ice")
        with client.websocket_connect(f"/api/projects/{pid}/call") as second:
            assert "already open" in receive(second, "error")["message"]
        first.send_json({"type": "hangup"})


def test_idle_call_ends_itself(tmp_path):
    client, store, _, calls, _, pid = make_env(tmp_path)
    calls.idle_timeout, calls.tick = 0.2, 0.05
    with client.websocket_connect(f"/api/projects/{pid}/call") as ws:
        assert "no conversation" in receive(ws, "ended")["reason"]
    wait_for(lambda: any("Video call ended" in e["message"] for e in store.list_events(pid)))


def test_messages_table_gains_channel_column(tmp_path):
    db = sqlite3.connect(tmp_path / "factory.db")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, "
               "role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)")
    db.execute("INSERT INTO messages (project_id, role, content, created_at) VALUES ('p', 'user', 'hi', 'now')")
    db.commit()
    db.close()
    assert Store(tmp_path).list_messages("p")[0]["channel"] == "chat"


def test_voice_url_uses_the_foundry_resource_unless_overridden(tmp_path):
    base = dict(model_deployment="m", tenant_id="", agent_name="t", home=tmp_path, log_level="INFO",
                poll_seconds=60, host="127.0.0.1", port=0)
    s = Settings(project_endpoint="https://res.services.ai.azure.com/api/projects/p", **base)
    assert s.voice_url.startswith("wss://res.services.ai.azure.com/voice-live/realtime?api-version=")
    assert "model=gpt-realtime" in s.voice_url
    other = Settings(project_endpoint="https://res.services.ai.azure.com/api/projects/p",
                     voice_endpoint="https://avatar-res.services.ai.azure.com", **base)
    assert other.voice_url.startswith("wss://avatar-res.services.ai.azure.com/")
    assert Settings(project_endpoint="", **base).voice_url == ""
