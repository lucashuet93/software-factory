import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from dotenv import load_dotenv

VOICE_LIVE_API_VERSION = "2026-07-15"


def _load_env() -> None:
    # Process environment wins; then working-directory .env; then the data-directory .env.
    load_dotenv(Path.cwd() / ".env", override=False)
    repo_env = Path(__file__).resolve().parents[3] / ".env"
    load_dotenv(repo_env, override=False)
    load_dotenv(default_home() / ".env", override=False)


def default_home() -> Path:
    override = os.environ.get("SOFTWARE_FACTORY_HOME", "").strip()
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "SoftwareFactory"


@dataclass(frozen=True)
class Settings:
    project_endpoint: str
    model_deployment: str
    tenant_id: str
    agent_name: str
    home: Path
    log_level: str
    poll_seconds: int
    host: str
    port: int
    voice_endpoint: str = ""
    voice_model: str = "gpt-realtime"
    voice_name: str = "en-US-Ava:DragonHDLatestNeural"
    voice_avatar: str = "lisa-casual-sitting"

    @property
    def foundry_configured(self) -> bool:
        return bool(self.project_endpoint and self.model_deployment)

    @property
    def voice_url(self) -> str:
        """Voice Live WebSocket URL on the Foundry resource behind the project endpoint, unless overridden."""
        host = urlsplit(self.voice_endpoint or self.project_endpoint).netloc
        if not host:
            return ""
        query = urlencode({"api-version": VOICE_LIVE_API_VERSION, "model": self.voice_model})
        return f"wss://{host}/voice-live/realtime?{query}"


def load_settings() -> Settings:
    _load_env()
    home = default_home()
    return Settings(
        project_endpoint=os.environ.get("AZURE_AI_PROJECT_ENDPOINT", "").strip(),
        model_deployment=os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "").strip(),
        tenant_id=os.environ.get("AZURE_TENANT_ID", "").strip(),
        agent_name=os.environ.get("SOFTWARE_FACTORY_AGENT_NAME", "").strip() or "software-factory-lead",
        home=home,
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO",
        poll_seconds=int(os.environ.get("SOFTWARE_FACTORY_POLL_SECONDS", "60")),
        host="127.0.0.1",
        port=int(os.environ.get("SOFTWARE_FACTORY_PORT", "8765")),
        voice_endpoint=os.environ.get("VOICE_LIVE_ENDPOINT", "").strip(),
        voice_model=os.environ.get("VOICE_LIVE_MODEL", "").strip() or "gpt-realtime",
        voice_name=os.environ.get("VOICE_LIVE_VOICE", "").strip() or "en-US-Ava:DragonHDLatestNeural",
        voice_avatar=os.environ.get("VOICE_LIVE_AVATAR", "").strip() or "lisa-casual-sitting",
    )
