"""HTTP client for justdumpit-ytscraper.

The agent reads:
- /watch-later/entries — list of recent Stage 2 outputs (for cron fallback)
- /watch-later/score?video_id=X — re-run Stage 2 against a single video
- /goals — the parsed GoalsConfig (single source of truth)
- /video/{id}/transcript/range — for agents that drill into transcript ranges
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from src.config import get_justdumpit_api_token, get_justdumpit_url


log = logging.getLogger("justdumpit_agent.goals_client")


_TIMEOUT = 15.0


def _headers() -> dict:
    h = {"Accept": "application/json"}
    token = get_justdumpit_api_token()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def get_goals() -> Optional[dict]:
    try:
        r = httpx.get(
            f"{get_justdumpit_url()}/goals",
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("get_goals failed: %s", e)
        return None


def list_watch_later_entries(only_pending: bool = True, limit: int = 50) -> list[dict]:
    try:
        params: dict = {"limit": limit}
        if only_pending:
            params["only_pending"] = "true"
        r = httpx.get(
            f"{get_justdumpit_url()}/watch-later/entries",
            params=params,
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("entries", []) or []
    except Exception as e:
        log.warning("list_watch_later_entries failed: %s", e)
        return []


def get_stage2(video_id: str, prompt_version: str = "v2") -> Optional[dict]:
    """Fetch a single Stage 2 output. Reads it from the analyses table via
    justdumpit's DB-backed endpoint. Returns the INNER stage2 payload
    (with `per_goal`, `rejections`, `goals_version`, etc.) — not the
    endpoint's outer wrapper."""
    try:
        r = httpx.get(
            f"{get_justdumpit_url()}/video/{video_id}/stage2",
            params={"prompt_version": prompt_version},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        wrapper = r.json()
        return wrapper.get("stage2") or wrapper
    except Exception as e:
        log.warning("get_stage2(%s) failed: %s", video_id, e)
        return None


def get_extraction(video_id: str, prompt_version: str = "v2") -> Optional[dict]:
    """Fetch the stored Stage 1 extraction (atoms, stack, etc.) for a video.

    Returns the inner extraction payload (with `transferable_atoms`, `stack`).
    """
    try:
        r = httpx.get(
            f"{get_justdumpit_url()}/video/{video_id}/stage2",
            params={"prompt_version": prompt_version},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        wrapper = r.json()
        return wrapper.get("extraction")
    except Exception as e:
        log.warning("get_extraction(%s) failed: %s", video_id, e)
        return None


def get_transcript_range(video_id: str, start: float, end: float) -> Optional[dict]:
    try:
        r = httpx.get(
            f"{get_justdumpit_url()}/video/{video_id}/transcript/range",
            params={"start": start, "end": end},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("get_transcript_range(%s) failed: %s", video_id, e)
        return None