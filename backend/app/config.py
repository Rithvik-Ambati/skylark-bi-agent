"""
config.py
---------
Single place where every environment variable the app needs gets read
and validated. Every other module imports `settings` from here instead
of calling os.getenv() directly — this means if a required variable is
missing, the app fails fast at startup with a clear error, instead of
failing confusingly halfway through handling a user's chat message.
"""

import os
from dotenv import load_dotenv

# Load variables from a local .env file if one exists (used for local dev).
# In production (Render/Railway/etc.) these are usually injected directly
# as real environment variables, so load_dotenv() is a harmless no-op there.
load_dotenv()


def _require(name: str) -> str:
    """Fetch a required env var, or raise a clear error naming what's missing."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy backend/.env.example to backend/.env and fill it in."
        )
    return value


class Settings:
    # monday.com
    MONDAY_API_TOKEN: str = _require("MONDAY_API_TOKEN")
    MONDAY_API_URL: str = "https://api.monday.com/v2"
    MONDAY_DEALS_BOARD_ID: str = _require("MONDAY_DEALS_BOARD_ID")
    MONDAY_WORK_ORDERS_BOARD_ID: str = _require("MONDAY_WORK_ORDERS_BOARD_ID")

    # Anthropic (the LLM powering the agent's reasoning + tool use)
    ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")
    ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    # Required when the API key is identity-linked (acts as a specific person)
    # rather than scoped to a single workspace at creation time. Find yours by
    # running: curl -i https://api.anthropic.com/v1/models -H "x-api-key: $KEY"
    # -H "anthropic-version: 2023-06-01" and reading the anthropic-workspace-id
    # response header.
    ANTHROPIC_WORKSPACE_ID: str = _require("ANTHROPIC_WORKSPACE_ID")

    # Misc
    APP_SECRET: str = os.getenv("APP_SECRET", "dev-secret-change-me")


settings = Settings()
