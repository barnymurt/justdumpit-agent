"""Cron fallback: poll justdumpit for new Stage 2 outputs and process them.

Runs as a daemon thread inside the FastAPI process. Disabled by setting
POLLER_ENABLED=false. Default interval 15 minutes.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from src import auditor, dispatcher, goals_client
from src.agent_config import load_gh_repos, load_realm


log = logging.getLogger("justdumpit_agent.poller")


_ENABLED = os.getenv("POLLER_ENABLED", "true").lower() in ("1", "true", "yes")
_INTERVAL = int(os.getenv("POLLER_INTERVAL", "900"))
_STARTUP_DELAY = int(os.getenv("POLLER_STARTUP_DELAY", "60"))


_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


def interval_seconds() -> int:
    return _INTERVAL


def should_start() -> bool:
    return _ENABLED


def start_background() -> None:
    global _thread
    if not _ENABLED:
        log.info("poller disabled via POLLER_ENABLED")
        return
    if is_running():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="agent-poller", daemon=True)
    _thread.start()
    log.info("poller started (interval=%ds, startup_delay=%ds)", _INTERVAL, _STARTUP_DELAY)


def stop_background() -> None:
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5)


def run_once(limit: int = 20) -> dict:
    """Single pass: fetch recent entries, process any not yet in audit log."""
    realm_cfg = load_realm()
    gh_cfg = load_gh_repos()
    entries = goals_client.list_watch_later_entries(only_pending=True, limit=limit)
    if not entries:
        return {"scanned": 0, "processed": 0, "videos": []}

    existing_videos = {a["video_id"] for a in auditor.list_actions(limit=1000)}
    new_entries = [e for e in entries if e.get("video_id") not in existing_videos]

    reports = []
    for entry in new_entries:
        vid = entry.get("video_id")
        if not vid:
            continue
        report = dispatcher.process_video_payload(
            video_id=vid,
            video_url=entry.get("video_url", ""),
            stage2=None,
            realm_cfg=realm_cfg,
            gh_cfg=gh_cfg,
        )
        reports.append({"video_id": vid, "report": report})

    return {"scanned": len(entries), "processed": len(reports), "videos": reports}


def _loop() -> None:
    if _STARTUP_DELAY > 0:
        log.info("poller waiting %ds before first run", _STARTUP_DELAY)
        if _stop_event.wait(timeout=_STARTUP_DELAY):
            return

    while not _stop_event.is_set():
        try:
            report = run_once()
            if report.get("processed", 0) > 0:
                log.info("poller processed %d videos", report["processed"])
        except Exception as e:
            log.exception("poller iteration failed: %s", e)
        if _stop_event.wait(timeout=_INTERVAL):
            return