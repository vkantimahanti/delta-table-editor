"""Load environment for agent-builder (portable — own .env or parent repo .env)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

AGENT_ROOT = Path(__file__).resolve().parents[1]


def load_env() -> None:
    """Load agent-builder/.env then optional DATA_EDITOR_ROOT/.env."""
    load_dotenv(AGENT_ROOT / ".env")
    editor_root = os.environ.get("DATA_EDITOR_ROOT", "").strip()
    if editor_root:
        load_dotenv(Path(editor_root).resolve() / ".env")
    else:
        # Default: parent of agent-builder is data-editor repo
        candidate = AGENT_ROOT.parent / ".env"
        if candidate.is_file():
            load_dotenv(candidate)


def data_editor_root() -> Path:
    raw = os.environ.get("DATA_EDITOR_ROOT", "").strip()
    if raw:
        return Path(raw).resolve()
    return AGENT_ROOT.parent.resolve()


def litellm_settings() -> dict[str, str]:
    load_env()
    base = (
        os.environ.get("LITELLM_BASE_URL")
        or os.environ.get("AI_GW_BASE_URL", "")
    ).rstrip("/")
    key = os.environ.get("LITELLM_API_KEY") or os.environ.get("AI_GW_API_KEY", "")
    model = os.environ.get("LITELLM_MODEL") or os.environ.get("COMPLETIONS_MODEL", "gpt-5")
    if not base or not key:
        raise EnvironmentError(
            "Set LITELLM_BASE_URL and LITELLM_API_KEY in agent-builder/.env"
        )
    return {"base_url": base, "api_key": key, "model": model}


def databricks_catalog() -> str:
    load_env()
    return os.environ.get("TARGET_CATALOG", "your_catalog")
