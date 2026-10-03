"""Central configuration, read from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

MCP_SERVER_PATH = ROOT / "mcp_server" / "server.py"


@dataclass(frozen=True)
class Settings:
    gcp_project: str = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    gcp_location: str = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0"))
    max_agent_steps: int = int(os.getenv("MAX_AGENT_STEPS", "6"))
    max_history_messages: int = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))


def get_settings() -> Settings:
    return Settings()
