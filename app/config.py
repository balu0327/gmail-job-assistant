import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _path(env: str, default: str) -> Path:
    p = Path(os.getenv(env, default))
    return p if p.is_absolute() else ROOT / p


TRACKER_DB = _path("TRACKER_DB", "tracker.db")
CREDENTIALS = _path("GOOGLE_CREDENTIALS", "credentials.json")
TOKEN = _path("GOOGLE_TOKEN", "token.json")
PROFILE = _path("PROFILE", "profile.json")


def _json_env(name: str, default: str) -> dict:
    """Extra request fields per provider, e.g. to switch a reasoning model's thinking off."""
    raw = os.getenv(name, default).strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def providers() -> list[dict]:
    """Model providers in the order they are tried."""
    found = []
    if os.getenv("NVIDIA_API_KEY"):
        found.append({
            "name": "nvidia",
            "base_url": os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
            "api_key": os.environ["NVIDIA_API_KEY"],
            "model": os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b"),
            "extra": _json_env("NVIDIA_EXTRA_BODY", '{"chat_template_kwargs": {"enable_thinking": false}}'),
        })
    if os.getenv("OLLAMA_MODEL"):
        found.append({
            "name": "ollama",
            "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            "api_key": "ollama",
            "model": os.environ["OLLAMA_MODEL"],
        })
    return found


def load_profile() -> dict:
    """Facts about the user that replies may use. Missing file means no facts."""
    if not PROFILE.exists():
        return {}
    return json.loads(PROFILE.read_text(encoding="utf-8"))
