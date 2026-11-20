"""Video calls with the lead engineer through Azure Voice Live and a talking avatar.

The browser sends microphone audio, typed lines, and the avatar's WebRTC offer over a local WebSocket. This module
relays them to Voice Live, runs the lead engineer's tools in-process, and records the conversation in the project's
chat. The avatar's video and voice stream from Azure straight to the browser over WebRTC.

Browser -> server: binary PCM16 mono 24 kHz audio, or JSON {"type": "avatar.offer", "sdp"} | {"type": "avatar.ready"}
| {"type": "text", "text"} | {"type": "hangup"}.
Server -> browser: JSON {"type": "status", "state"} | {"type": "ice", "servers"} | {"type": "avatar.answer", "sdp"}
| {"type": "caption", "role", "text", "final"} | {"type": "tool", "name", "state"} | {"type": "error", "message"}
| {"type": "ended", "reason"}.
"""

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from azure.identity import DefaultAzureCredential
from starlette.websockets import WebSocket, WebSocketState

from .agent import EVENT_PREFIX, INSTRUCTIONS, MAX_TOOL_ROUNDS, TOOLS, LeadAgent
from .bus import EventBus
from .config import Settings
from .store import Store

log = logging.getLogger(__name__)

TOKEN_SCOPE = "https://ai.azure.com/.default"
IDLE_TIMEOUT_SECONDS = 10 * 60
MAX_CALL_SECONDS = 30 * 60
TOOL_OUTPUT_LIMIT = 12_000
TRANSCRIPT_LIMIT = 8_000
EVENT_TEXT_LIMIT = 4_000
CONFIRMED_TOOLS = ("create_task", "update_task", "retry_task", "write_document", "start_delivery")

VOICE_INSTRUCTIONS = f"""# Video call
You are now on a live video call with the customer, shown as a talking avatar. Everything you say is spoken aloud.
These call rules override any formatting rules above.
- Speak in short, natural sentences. Never use Markdown, lists, tables, code, or URLs in speech. Say "issue 12",
  not "#12". Keep answers brief and offer detail rather than reciting it.
- Before calling {", ".join(CONFIRMED_TOOLS)}, say in one or two sentences exactly what you are about to do and
  ask the customer to confirm. Call the tool only after they clearly say yes. One confirmation can cover a set of
  tasks you described together. This applies to delivery events too: during a call, always ask first.
- Other tools only read information and need no confirmation; say briefly that you are checking.
- Your speech is short, but task briefs and documents you write through tools must be as complete as in text chat.
- Messages that start with "{EVENT_PREFIX}" come from the delivery loop during the call. Tell the customer about
  them in a sentence or two.
- The customer may also type lines during the call; treat them like speech."""

GREETING = ("The call is connected and the customer can see and hear you. Greet them in one short sentence, give a "
            "one-sentence status of the project, and ask what they would like to discuss.")
INTERIM_INSTRUCTIONS = ("Say a very short, natural filler that you are working on it, such as 'One moment, I'm "
                        "updating the issues.' Never repeat the same filler twice in a row.")


def realtime_tools() -> list[dict]:
    """The lead engineer's tools in the Voice Live (Realtime API) function format."""
    tools = []
    for t in TOOLS:
        d = t.as_dict()
        tools.append({"type": "function", "name": d["name"], "description": d.get("description", ""),
                      "parameters": d["parameters"]})
    return tools


def avatar_config(spec: str) -> dict:
    """VOICE_LIVE_AVATAR is "<character>-<style>", for example "lisa-casual-sitting"."""
    character, _, style = spec.strip().lower().partition("-")
    config: dict[str, Any] = {
        "type": "video-avatar",
        "character": character,
        "customized": False,
        # A portrait crop of the 1080p frame on a steel background that matches the app. Avatar styles that render
        # natively in portrait (1080x1920) reject this resolution.
        "video": {"codec": "h264", "bitrate": 1_000_000, "resolution": {"width": 1920, "height": 1080},
                  "crop": {"top_left": [560, 0], "bottom_right": [1360, 1080]},
                  "background": {"color": "#262A30FF"}},
    }
    if style:
        config["style"] = style
    return config


def session_config(settings: Settings, instructions: str) -> dict:
    return {
        "modalities": ["text", "audio"],
        "instructions": instructions,
        "voice": {"name": settings.voice_name, "type": "azure-standard"},
        "input_audio_format": "pcm16",
        "input_audio_sampling_rate": 24000,
        "input_audio_transcription": {"model": "azure-speech"},
        "input_audio_noise_reduction": {"type": "azure_deep_noise_suppression"},
        "input_audio_echo_cancellation": {"type": "server_echo_cancellation"},
        "turn_detection": {"type": "azure_semantic_vad", "silence_duration_ms": 600, "remove_filler_words": True},
        "tools": realtime_tools(),
        "tool_choice": "auto",
        "interim_response": {"type": "llm_interim_response", "triggers": ["tool"], "instructions": INTERIM_INSTRUCTIONS},
        "avatar": avatar_config(settings.voice_avatar),
    }


def conversation_so_far(messages: list[dict]) -> str:
    """Recent chat as plain text for the call's instructions, newest last, trimmed from the oldest end."""
    names = {"user": "Customer", "assistant": "Lead engineer", "event": "Delivery event"}
    lines = [f"{names[m['role']]}: {m['content']}" for m in messages if m["role"] in names]
    text = "\n\n".join(lines)
    if len(text) > TRANSCRIPT_LIMIT:
        text = "...(earlier conversation trimmed)\n" + text[-TRANSCRIPT_LIMIT:]
    return text or "(no conversation yet)"


def friendly_error(e: Exception) -> str:
    status = getattr(getattr(e, "response", None), "status_code", None)
    if status in (401, 403):
        return "Voice Live denied access. Your Azure account needs the Foundry User role on the Foundry resource."
    if "CredentialUnavailable" in type(e).__name__:
        return "No Azure sign-in found. Run `az login` and restart Software Factory."
    return f"Could not start the call: {str(e)[:300]}"


async def _connect(url: str, token: str) -> Any:
    return await websockets.connect(url, additional_headers={"Authorization": f"Bearer {token}"},
                                    max_size=None, open_timeout=20)


class CallManager:
    """Owns the active calls (at most one per project) and routes delivery events to them."""

    def __init__(self, settings: Settings, store: Store, bus: EventBus, agent: LeadAgent,
                 connect: Callable[[str, str], Awaitable[Any]] | None = None,
                 token: Callable[[], Awaitable[str]] | None = None) -> None:
        self.settings = settings
        self.store = store
        self.bus = bus
        self.agent = agent
        self.calls: dict[str, CallSession] = {}
        self.connect = connect or _connect
        self.token = token or self._azure_token
        self.idle_timeout = IDLE_TIMEOUT_SECONDS
        self.max_seconds = MAX_CALL_SECONDS
        self.tick = 2.0
        self._credential: DefaultAzureCredential | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def _azure_token(self) -> str:
        if self._credential is None:
            self._credential = DefaultAzureCredential()
        token = await asyncio.to_thread(self._credential.get_token, TOKEN_SCOPE)
        return token.token

    def status(self) -> dict:
        if not self.settings.voice_url:
            return {"ok": False, "detail": "Set AZURE_AI_PROJECT_ENDPOINT (or VOICE_LIVE_ENDPOINT) in .env."}
        return {"ok": True, "detail": f"Voice Live {self.settings.voice_model} with avatar {self.settings.voice_avatar}"}

    def is_active(self, pid: str) -> bool:
        return pid in self.calls

    def route_event(self, pid: str) -> None:
        """Orchestrator callback (any thread): a delivery event goes to the live call if there is one, else to chat."""
        call, loop = self.calls.get(pid), self._loop
        if call is not None and loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(call.on_delivery_event)
        else:
            self.agent.chat_async(pid)

    async def handle(self, pid: str, ws: WebSocket, is_shutting_down: Callable[[], bool] = lambda: False) -> None:
        await ws.accept()
        problem = None
        if not self.store.get_project(pid):
            problem = "Project not found."
        elif not self.settings.voice_url:
            problem = self.status()["detail"]
        elif pid in self.calls:
            problem = "A call with this project is already open, possibly in another browser tab."
        if problem:
            await ws.send_json({"type": "error", "message": problem})
            await ws.close(code=4400)
            return
        self._loop = asyncio.get_running_loop()
        call = CallSession(self, pid, ws)
        self.calls[pid] = call
        try:
            await call.run(is_shutting_down)
        finally:
            self.calls.pop(pid, None)
            await call.finish()


class CallSession:
    def __init__(self, manager: CallManager, pid: str, browser: WebSocket) -> None:
        self.m = manager
        self.pid = pid
        self.browser = browser
        self.upstream: Any = None
        self.connected = False
        self.closed = False
        self.end_reason = "Call ended"
        self.started = time.monotonic()
        self.last_activity = self.started
        self.responding = False
        self.need_response = False
        self.tools_running = False
        self.user_speaking = False
        self.last_input = "user"  # "user" when the customer spoke last, "event" for delivery events and the greeting
        self.tool_rounds = 0
        self.greeted = False
        self.ice_sent = False
        self.state = ""
        self.pending_events: list[dict] = []
        messages = manager.store.list_messages(pid, limit=1)
        self.event_cursor = messages[-1]["id"] if messages else 0  # newest delivery event already handed to the call
        self._browser_lock = asyncio.Lock()
        self._up_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # ---------- plumbing ----------
    async def set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            await self.to_browser({"type": "status", "state": state})

    async def to_browser(self, msg: dict) -> None:
        if self.browser.client_state != WebSocketState.CONNECTED:
            return
        async with self._browser_lock:
            try:
                await self.browser.send_json(msg)
            except Exception:  # noqa: BLE001 - the browser went away; the pumps notice and end the call
                pass

    async def up(self, msg: dict) -> None:
        if self.upstream is None:
            return
        async with self._up_lock:
            await self.upstream.send(json.dumps(msg))

    def spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def instructions(self) -> str:
        context = self.m.agent.build_context(self.pid)
        history = conversation_so_far(self.m.store.list_messages(self.pid, limit=40))
        return f"{INSTRUCTIONS}\n\n{VOICE_INSTRUCTIONS}\n\n{context}\n## Conversation so far\n{history}\n"

    def save(self, role: str, text: str) -> None:
        if self.m.store.add_message(self.pid, role, text, channel="call"):
            self.m.bus.publish(self.pid, {"type": "message"})

    # ---------- lifecycle ----------
    async def run(self, is_shutting_down: Callable[[], bool]) -> None:
        await self.set_state("connecting")
        try:
            token = await self.m.token()
            self.upstream = await self.m.connect(self.m.settings.voice_url, token)
            await self.up({"type": "session.update", "session": session_config(self.m.settings, self.instructions())})
        except Exception as e:  # noqa: BLE001
            log.exception("could not start Voice Live session")
            self.end_reason = friendly_error(e)
            await self.to_browser({"type": "error", "message": self.end_reason})
            return
        self.connected = True
        self.m.store.add_event(self.pid, "call", "Video call with the lead engineer started")
        self.m.bus.publish(self.pid, {"type": "call", "active": True})
        pumps = [asyncio.ensure_future(p) for p in
                 (self.pump_browser(), self.pump_upstream(), self.watchdog(is_shutting_down))]
        try:
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in [*pumps, *self._tasks]:
                t.cancel()
            await asyncio.gather(*pumps, *self._tasks, return_exceptions=True)

    async def finish(self) -> None:
        self.closed = True
        if self.upstream is not None:
            try:
                await self.upstream.close()
            except Exception:  # noqa: BLE001
                pass
        await self.to_browser({"type": "ended", "reason": self.end_reason})
        if self.browser.client_state == WebSocketState.CONNECTED:
            try:
                await self.browser.close()
            except Exception:  # noqa: BLE001
                pass
        if not self.connected:
            return
        # Chat turns skip what the call handled; events that arrived too late for the call go to chat instead.
        messages = self.m.store.list_messages(self.pid, limit=1)
        latest = messages[-1]["id"] if messages else 0
        self.m.agent.mark_seen(self.pid, min([e["id"] - 1 for e in self.pending_events], default=latest))
        if self.pending_events:
            self.m.agent.chat_async(self.pid)
        minutes = max(1, round((time.monotonic() - self.started) / 60))
        self.m.store.add_event(self.pid, "call", f"Video call ended after {minutes} min: {self.end_reason}")
        self.m.bus.publish(self.pid, {"type": "call", "active": False})

    async def watchdog(self, is_shutting_down: Callable[[], bool]) -> None:
        while True:
            await asyncio.sleep(self.m.tick)
            now = time.monotonic()
            if is_shutting_down():
                self.end_reason = "Software Factory is shutting down"
                return
            if now - self.started > self.m.max_seconds:
                self.end_reason = f"reached the {self.m.max_seconds // 60}-minute call limit"
                return
            if now - self.last_activity > self.m.idle_timeout and not self.tools_running:
                self.end_reason = f"no conversation for {self.m.idle_timeout // 60} minutes"
                return

    # ---------- browser -> Voice Live ----------
    async def pump_browser(self) -> None:
        while True:
            msg = await self.browser.receive()
            if msg["type"] == "websocket.disconnect":
                self.end_reason = "Call ended"
                return
            if msg.get("bytes"):
                await self.up({"type": "input_audio_buffer.append", "audio": base64.b64encode(msg["bytes"]).decode()})
                continue
            try:
                data = json.loads(msg.get("text") or "{}")
            except json.JSONDecodeError:
                continue
            kind = data.get("type")
            if kind == "hangup":
                self.end_reason = "Call ended"
                return
            if kind == "avatar.offer":
                await self.up({"type": "session.avatar.connect", "client_sdp": data.get("sdp", "")})
            elif kind == "avatar.ready" and not self.greeted:
                self.greeted = True
                self.last_input = "event"
                await self.up({"type": "conversation.item.create", "item": {
                    "type": "message", "role": "system", "content": [{"type": "input_text", "text": GREETING}]}})
                await self.request_response()
            elif kind == "text" and str(data.get("text", "")).strip():
                text = str(data["text"]).strip()[:4000]
                self.touch(user=True)
                self.save("user", text)
                await self.up({"type": "conversation.item.create", "item": {
                    "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
                await self.request_response()

    def touch(self, user: bool = False) -> None:
        self.last_activity = time.monotonic()
        if user:
            self.last_input = "user"
            self.tool_rounds = 0

    # ---------- Voice Live -> browser ----------
    async def pump_upstream(self) -> None:
        async for raw in self.upstream:
            if isinstance(raw, bytes):
                continue
            try:
                await self.on_upstream(json.loads(raw))
            except Exception:  # noqa: BLE001 - one bad event must not drop the call
                log.exception("failed to handle Voice Live event")
        self.end_reason = "Voice Live closed the connection"

    async def on_upstream(self, ev: dict) -> None:
        kind = ev.get("type", "")
        if kind == "session.updated":
            servers = ((ev.get("session") or {}).get("avatar") or {}).get("ice_servers")
            if servers and not self.ice_sent:
                self.ice_sent = True
                await self.to_browser({"type": "ice", "servers": servers})
        elif kind == "session.avatar.connecting":
            await self.to_browser({"type": "avatar.answer", "sdp": ev.get("server_sdp", "")})
        elif kind == "input_audio_buffer.speech_started":
            self.user_speaking = True
            self.touch(user=True)
            await self.set_state("listening")
        elif kind == "input_audio_buffer.speech_stopped":
            self.user_speaking = False
            await self.set_state("thinking")
        elif kind == "conversation.item.input_audio_transcription.completed":
            text = (ev.get("transcript") or "").strip()
            if text:
                self.save("user", text)
                await self.to_browser({"type": "caption", "role": "user", "text": text, "final": True})
        elif kind == "response.created":
            self.responding = True
            self.touch()
        elif kind == "response.audio_transcript.delta":
            await self.to_browser({"type": "caption", "role": "assistant", "text": ev.get("delta", ""), "final": False})
            await self.set_state("speaking")
        elif kind == "response.audio_transcript.done":
            text = (ev.get("transcript") or "").strip()
            if text:
                self.save("assistant", text)
                await self.to_browser({"type": "caption", "role": "assistant", "text": text, "final": True})
        elif kind == "session.avatar.switch_to_idle":
            await self.set_state("ready")
        elif kind == "response.done":
            await self.on_response_done(ev.get("response") or {})
        elif kind == "error":
            err = ev.get("error") or {}
            log.warning("Voice Live error: %s", err)
            # Overlapping response requests are expected when the customer talks over a tool round; stay quiet.
            if "active response" not in str(err.get("message", "")).lower():
                # A rejected request must not leave the call waiting for a response that will never come.
                self.responding = False
                await self.to_browser({"type": "error", "message": str(err.get("message") or err)[:300]})

    async def on_response_done(self, response: dict) -> None:
        self.responding = False
        self.touch()
        calls = [o for o in response.get("output") or [] if o.get("type") == "function_call"]
        if calls and response.get("status") == "completed":
            self.tools_running = True
            self.spawn(self.run_tools(calls))
        elif self.need_response:
            await self.request_response()
        elif self.pending_events and not self.user_speaking:
            await self.flush_events()
        else:
            await self.set_state("ready")

    async def request_response(self, tool_choice: str | None = None) -> None:
        if self.responding or self.tools_running:
            self.need_response = True
            return
        self.responding = True
        self.need_response = False
        msg: dict[str, Any] = {"type": "response.create"}
        if tool_choice:
            msg["response"] = {"tool_choice": tool_choice}
        await self.up(msg)

    async def run_tools(self, calls: list[dict]) -> None:
        customer_turn = self.last_input == "user"
        try:
            for c in calls:
                name = c.get("name", "")
                await self.to_browser({"type": "tool", "name": name, "state": "running"})
                try:
                    result = await asyncio.to_thread(self.m.agent.run_tool, self.pid, name, c.get("arguments") or "{}",
                                                     customer_turn=customer_turn)
                except Exception as e:  # noqa: BLE001
                    log.exception("call tool %s failed", name)
                    result = {"ok": False, "error": str(e)[:500]}
                output = json.dumps(result)
                if len(output) > TOOL_OUTPUT_LIMIT:
                    output = output[:TOOL_OUTPUT_LIMIT] + "...(truncated; ask for a narrower read)"
                await self.up({"type": "conversation.item.create", "item": {
                    "type": "function_call_output", "call_id": c.get("call_id"), "output": output}})
                await self.to_browser({"type": "tool", "name": name, "state": "done" if result.get("ok") else "error"})
            self.tool_rounds += 1
        finally:
            self.tools_running = False
        await self.request_response(tool_choice="none" if self.tool_rounds >= MAX_TOOL_ROUNDS else None)

    # ---------- delivery events ----------
    def on_delivery_event(self) -> None:
        """Runs on the event loop. New delivery events wait until the agent and customer are both quiet."""
        if self.closed:
            self.m.agent.chat_async(self.pid)
            return
        fresh = [m for m in self.m.store.list_messages(self.pid, limit=30)
                 if m["role"] == "event" and m["id"] > self.event_cursor]
        if not fresh:
            return
        self.event_cursor = fresh[-1]["id"]
        self.pending_events += fresh
        if not (self.responding or self.tools_running or self.user_speaking):
            self.spawn(self.flush_events())

    async def flush_events(self) -> None:
        events, self.pending_events = self.pending_events, []
        if not events:
            return
        for e in events:
            text = e["content"] if len(e["content"]) <= EVENT_TEXT_LIMIT else e["content"][:EVENT_TEXT_LIMIT] + "..."
            await self.up({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": f"{EVENT_PREFIX}\n{text}"}]}})
        self.last_input = "event"
        self.tool_rounds = 0
        await self.request_response()
