from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


CONFIG_DIR = Path(__file__).parent.parent
ENV_PATH = CONFIG_DIR / ".env"
DATA_DIR = Path(os.getenv("AGENT_DATA_DIR", str(CONFIG_DIR / "data")))


load_dotenv(ENV_PATH)


def get_api_key() -> str:
    key = os.getenv("MINIMAX_API_KEY", "").strip()
    if not key:
        raise ValueError("MINIMAX_API_KEY not set in .env")
    return key


def get_justdumpit_url() -> str:
    url = os.getenv("JUSTDUMPIT_URL", "").strip()
    if not url:
        raise ValueError("JUSTDUMPIT_URL not set in .env (e.g. https://justdumpit-ytscraper.fly.dev)")
    return url.rstrip("/")


def get_justdumpit_api_token() -> str:
    return os.getenv("JUSTDUMPIT_API_TOKEN", "").strip()


def get_gh_token() -> str:
    token = os.getenv("GH_TOKEN", "").strip()
    if not token:
        raise ValueError("GH_TOKEN not set in .env (create a PAT at https://github.com/settings/tokens)")
    return token


def get_gh_owner() -> str:
    return os.getenv("GH_OWNER", "barnymurt").strip()


def get_db_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "agent_audit.db"


def get_telegram_bot_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def get_justdumpit_api_token_for_internal() -> str:
    """Shared secret the agent uses to authenticate calls TO justdumpit."""
    return os.getenv("JUSTDUMPIT_API_TOKEN", "").strip()


def get_email_poll_interval() -> int:
    return int(os.getenv("EMAIL_POLL_INTERVAL", "60"))


def get_email_poll_enabled() -> bool:
    return os.getenv("EMAIL_POLL_ENABLED", "true").lower() in ("1", "true", "yes")