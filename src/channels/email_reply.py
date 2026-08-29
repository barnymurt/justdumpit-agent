"""Email reply channel: polls Gmail for replies to action proposal emails.

When justdumpit-ytscraper sends a proposal email with an
`X-Justdumpit-Action-Id: <goal_id>:<action_id>` header, the operator can
reply with:
  /approve
  /reject
  /changes: <notes>
  approve   (shorthand)

The agent's email poller (see poller.py) hits justdumpit's
`/gmail/inbox` endpoint every minute, matches replies to the right
action via the header, and applies the decision.

Replies via Gmail API have `In-Reply-To` header set to the original
message's Message-ID. We set `Message-ID: <action_id>@justdumpit.online`
on the original email so we can match.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from src import auditor
from src.config import get_justdumpit_api_token_for_internal, get_justdumpit_url


log = logging.getLogger("justdumpit_agent.email_reply")


_COMMAND_RE = re.compile(
    r"^\s*(?:/\s*)?(approve|reject|changes|skip|done|yes|no)\b\s*[:\-]?\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)


def fetch_inbox(since_minutes: int = 10, limit: int = 20) -> list[dict]:
    """Fetch recent inbox messages via justdumpit's /gmail/inbox endpoint."""
    token = get_justdumpit_api_token_for_internal()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = httpx.get(
            f"{get_justdumpit_url()}/gmail/inbox",
            params={"since_minutes": since_minutes, "limit": limit},
            headers=headers,
            timeout=20.0,
        )
        r.raise_for_status()
        return r.json().get("messages", []) or []
    except Exception as e:
        log.warning("fetch_inbox failed: %s", e)
        return []


def parse_command(body: str) -> tuple[Optional[str], Optional[str]]:
    """Parse an email body for a decision command.

    Returns (decision, note) or (None, None) if no command found.

    Accepted forms:
        approve
        /approve
        approve: <note>
        reject because <reason>
        changes: <note>
    """
    body = (body or "").strip()
    if not body:
        return None, None

    quoted_lines = []
    non_quoted_lines = []
    for line in body.splitlines():
        if line.lstrip().startswith(">"):
            quoted_lines.append(line)
        else:
            non_quoted_lines.append(line)

    candidate = "\n".join(non_quoted_lines).strip()
    if not candidate:
        return None, None

    m = _COMMAND_RE.match(candidate)
    if not m:
        return None, None

    verb = m.group(1).lower()
    note = m.group(2).strip() or None
    if verb in ("yes", "approve"):
        return "approve", note
    if verb in ("no", "reject"):
        return "reject", note
    if verb == "changes":
        return "changes", note
    if verb == "skip":
        return "skip", note
    if verb == "done":
        return "done", note
    return None, None


def extract_action_id_from_header(header_value: str) -> list[tuple[str, str]]:
    """Parse `X-Justdumpit-Action-Id: <goal1>:<aid1> <goal2>:<aid2> ...` into [(goal_id, action_id), ...].

    Returns empty list if header is empty or malformed.
    """
    if not header_value:
        return []
    out: list[tuple[str, str]] = []
    for token in header_value.split():
        if ":" not in token:
            continue
        gid, aid = token.split(":", 1)
        if gid and aid:
            out.append((gid.strip(), aid.strip()))
    return out


def apply_decision(action_id: str, decision: str, note: Optional[str] = None) -> dict:
    """Apply a decision to the audit log."""
    if decision == "approve":
        auditor.update_action_status(action_id, "approved")
    elif decision == "reject":
        auditor.update_action_status(
            action_id, "rejected",
            rejection_reason=note or "rejected via email",
        )
    elif decision == "changes":
        auditor.update_action_status(
            action_id,
            "awaiting_greenlight",
            artifacts={"changes_requested": note or "(no notes)"},
        )
    return {"ok": True, "decision": decision, "action_id": action_id}


def send_reply(to: str, subject: str, body: str,
               in_reply_to: Optional[str] = None,
               thread_id: Optional[str] = None) -> dict:
    """Send a confirmation reply via justdumpit's /gmail/send-reply."""
    from src.config import get_justdumpit_api_token_for_internal
    token = get_justdumpit_api_token_for_internal()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = httpx.post(
            f"{get_justdumpit_url()}/gmail/send-reply",
            json={
                "to": to,
                "subject": subject,
                "body": body,
                "in_reply_to": in_reply_to,
                "thread_id": thread_id,
            },
            headers=headers,
            timeout=15.0,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("send_reply failed: %s", e)
        return {"ok": False, "error": str(e)}


def poll_once(since_minutes: int = 10) -> dict:
    """One pass: fetch recent inbox, find replies to action emails, apply decisions.

    Returns a report dict with counts.
    """
    messages = fetch_inbox(since_minutes=since_minutes, limit=30)
    applied: list[dict] = []
    skipped: list[dict] = []

    for msg in messages:
        action_pairs = extract_action_id_from_header(msg.get("x_justdumpit_action_id", ""))
        if not action_pairs:
            continue

        verb, note = parse_command(msg.get("body", ""))
        if verb is None:
            continue

        for _, action_id in action_pairs:
            if verb == "skip":
                continue
            try:
                apply_decision(action_id, verb, note=note)
                applied.append({
                    "action_id": action_id,
                    "decision": verb,
                    "from": msg.get("from"),
                    "subject": msg.get("subject"),
                    "note": note,
                })
            except Exception as e:
                log.warning("apply_decision(%s, %s) failed: %s", action_id, verb, e)
                skipped.append({"action_id": action_id, "error": str(e)})

        confirm_body = (
            f"Decision recorded: <b>{verb}</b> for action(s) "
            f"{', '.join(aid for _, aid in action_pairs)}"
            + (f"\n\nNote: {note}" if note else "")
        )
        send_reply(
            to=msg.get("from", ""),
            subject=msg.get("subject", ""),
            body=confirm_body,
            in_reply_to=msg.get("in_reply_to"),
            thread_id=msg.get("thread_id"),
        )

    return {
        "scanned": len(messages),
        "applied": len(applied),
        "skipped": len(skipped),
        "applied_details": applied,
        "skipped_details": skipped,
    }