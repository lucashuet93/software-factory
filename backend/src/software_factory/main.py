import logging
import re
import threading
import webbrowser

import uvicorn

from .agent import LeadAgent
from .api import create_app
from .bus import EventBus
from .config import load_settings
from .orchestrator import Orchestrator
from .store import Store
from .voice import CallManager


class RedactingFilter(logging.Filter):
    _pattern = re.compile(r"(ghp_|github_pat_|gho_|ghu_)[A-Za-z0-9_]+")

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._pattern.sub(r"\1[REDACTED]", str(record.msg))
        if record.args:
            record.args = tuple(self._pattern.sub(r"\1[REDACTED]", str(a)) for a in record.args)
        return True


def build():
    settings = load_settings()
    store = Store(settings.home)
    bus = EventBus()
    orch = Orchestrator(store, bus, settings.poll_seconds)
    agent = LeadAgent(settings, store, orch, bus)
    calls = CallManager(settings, store, bus, agent)
    orch._foundry_check = agent.check_connection
    # Delivery events wake the lead engineer: on the live video call if one is open, otherwise in chat.
    orch.on_event = calls.route_event
    return settings, store, bus, orch, agent, calls


def main() -> None:
    settings, store, bus, orch, agent, calls = build()
    handler = logging.FileHandler(settings.home / "logs" / "software-factory.log", encoding="utf-8")
    console = logging.StreamHandler()
    for h in (handler, console):
        h.addFilter(RedactingFilter())
    logging.basicConfig(level=settings.log_level, handlers=[handler, console],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    url = f"http://{settings.host}:{settings.port}"
    print(f"Software Factory running at {url} (data: {settings.home}). Press Ctrl+C to stop.")
    server: uvicorn.Server | None = None
    app = create_app(settings, store, bus, orch, agent,
                     is_shutting_down=lambda: bool(server and server.should_exit), calls=calls)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning",
                                           timeout_graceful_shutdown=3))
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    server.run()


if __name__ == "__main__":
    main()
