"""Polling workers.

Two background threads:
1. Stage 2 poller (every POLLER_INTERVAL seconds): polls justdumpit for
   recent Stage 2 outputs and processes them.
2. Email reply poller (every EMAIL_POLL_INTERVAL seconds): polls Gmail
   for replies to action proposal emails and applies operator decisions.

Both are daemon threads inside the FastAPI process. Disable with
POLLER_ENABLED=false or EMAIL_POLL_ENABLED=false.
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

_EMAIL_POLL_ENABLED = os.getenv("EMAIL_POLL_ENABLED", "true").lower() in ("1", "true", "yes")
_EMAIL_POLL_INTERVAL = int(os.getenv("EMAIL_POLL_INTERVAL", "60"))
_EMAIL_LOOKBACK_MINUTES = int(os.getenv("EMAIL_LOOKBACK_MINUTES", "15"))


_thread: Optional[threading.Thread] = None
_email_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


def interval_seconds() -> int:
    return _INTERVAL


def should_start() -> bool:
    return _ENABLED


def email_poll_interval_seconds() -> int:
    return _EMAIL_POLL_INTERVAL


def start_background() -> None:
    global _thread, _email_thread
    if not _ENABLED:
        log.info("poller disabled via POLLER_ENABLED")
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="agent-poller", daemon=True)
    _thread.start()
    log.info("poller started (interval=%ds, startup_delay=%ds)", _INTERVAL, _STARTUP_DELAY)
    if _EMAIL_POLL_ENABLED:
        _email_thread = threading.Thread(
            target=email_poll_loop,
            name="agent-email-poller",
            daemon=True,
        )
        _email_thread.start()
        log.info(
            "email poller started (interval=%ds, lookback=%dmin)",
            _EMAIL_POLL_INTERVAL, _EMAIL_LOOKBACK_MINUTES,
        )


def stop_background() -> None:
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5)
    if _email_thread is not None:
        _email_thread.join(timeout=5)


def email_poll_loop() -> None:
    from src.channels import email_reply
    while not _stop_event.is_set():
        try:
            report = email_reply.poll_once(since_minutes=_EMAIL_LOOKBACK_MINUTES)
            if report.get("applied", 0) > 0:
                log.info("email poller applied %d decisions", report["applied"])
        except Exception as e:
            log.exception("email poll iteration failed: %s", e)
        if _stop_event.wait(timeout=_EMAIL_POLL_INTERVAL):
            return


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


def email_poll_once() -> dict:
    from src.channels import email_reply
    return email_reply.poll_once(since_minutes=_EMAIL_LOOKBACK_MINUTES)


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
