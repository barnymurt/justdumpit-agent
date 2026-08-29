"""Telegram bot integration for justdumpit-agent.

When a tier_2/3 proposal is drafted, the agent sends a Telegram message
to the operator with inline keyboard buttons (Approve / Reject / Changes).
Button taps and slash commands (e.g. /approve act_123) hit our webhook
and update the action status.

The operator's chat_id is auto-discovered: the first person to send /start
to the bot becomes the operator. We persist chat_id in /data/operator_chat_id.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import httpx

from src import auditor
from src.config import DATA_DIR, get_telegram_bot_token


log = logging.getLogger("justdumpit_agent.telegram")


OPERATOR_CHAT_FILE = DATA_DIR / "operator_chat_id"


_API_BASE = "https://api.telegram.org/bot{token}/{method}"


def _api(token: str, method: str, **params) -> dict:
    url = _API_BASE.format(token=token, method=method)
    try:
        r = httpx.post(url, json=params, timeout=15.0)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            log.warning("Telegram API %s returned not-ok: %s", method, data)
        return data
    except Exception as e:
        log.warning("Telegram API %s failed: %s", method, e)
        return {"ok": False, "error": str(e)}


def get_operator_chat_id() -> Optional[int]:
    if not OPERATOR_CHAT_FILE.exists():
        return None
    try:
        return int(OPERATOR_CHAT_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def set_operator_chat_id(chat_id: int) -> None:
    OPERATOR_CHAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OPERATOR_CHAT_FILE.write_text(str(chat_id))


def bot_info(token: Optional[str] = None) -> dict:
    """Return bot info from Telegram (used for /setWebhook setup)."""
    return _api(token or get_telegram_bot_token(), "getMe")


def set_webhook(webhook_url: str, token: Optional[str] = None) -> dict:
    return _api(token or get_telegram_bot_token(), "setWebhook", url=webhook_url)


def delete_webhook(token: Optional[str] = None) -> dict:
    return _api(token or get_telegram_bot_token(), "deleteWebhook")


def send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None,
                 parse_mode: Optional[str] = "HTML",
                 token: Optional[str] = None) -> dict:
    """Send a text message to a chat. Returns the Telegram API response."""
    params: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        params["parse_mode"] = parse_mode
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    return _api(token or get_telegram_bot_token(), "sendMessage", **params)


def answer_callback_query(callback_query_id: str, text: Optional[str] = None,
                        show_alert: bool = False,
                        token: Optional[str] = None) -> dict:
    params: dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        params["text"] = text
    return _api(token or get_telegram_bot_token(), "answerCallbackQuery", **params)


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


def format_proposal(action_row: dict, video_url: str = "") -> str:
    """Format an action as a Telegram message body (HTML)."""
    lines: list[str] = []
    lines.append(f"<b>Action:</b> {_html_escape(action_row.get('action_description', '')[:600])}")
    lines.append("")
    lines.append(f"<b>Goal:</b> {action_row.get('goal_id', '?')}")
    lines.append(f"<b>Final tier:</b> {action_row.get('final_tier', '?')}")
    lines.append(f"<b>Realm:</b> {action_row.get('realm', '?')}")
    lines.append(f"<b>Stage 2 relevance:</b> {action_row.get('stage2_relevance', '?')}/3")
    lines.append(f"<b>Effort:</b> {action_row.get('effort_hours', '?')}h")
    lines.append(f"<b>Reversibility:</b> {action_row.get('reversibility', '?')}")
    atoms = action_row.get("atom_ids") or []
    if atoms:
        lines.append(f"<b>Atoms:</b> {', '.join(atoms)}")
    if video_url:
        lines.append(f"<b>Video:</b> {_html_escape(video_url)}")
    lines.append("")
    lines.append("<i>Approve · Reject · Changes (reply with notes)</i>")
    return "\n".join(lines)


def action_keyboard(action_id: str) -> dict:
    """Inline keyboard with Approve / Reject / Changes buttons for one action."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"approve:{action_id}"},
                {"text": "❌ Reject", "callback_data": f"reject:{action_id}"},
                {"text": "✏️ Changes", "callback_data": f"changes:{action_id}"},
            ]
        ]
    }


# ---------------------------------------------------------------------------
# Decision application
# ---------------------------------------------------------------------------


def apply_decision(action_id: str, decision: str, note: Optional[str] = None) -> dict:
    """Apply a Telegram decision to the audit log.

    decision ∈ {approve, reject, changes}
    """
    if decision == "approve":
        auditor.update_action_status(action_id, "approved")
    elif decision == "reject":
        auditor.update_action_status(
            action_id, "rejected", rejection_reason=note or "rejected via Telegram"
        )
    elif decision == "changes":
        auditor.update_action_status(
            action_id,
            "awaiting_greenlight",
            artifacts={"changes_requested": note or "(no notes)"},
        )
    return {"ok": True, "decision": decision}


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------


def send_proposal(chat_id: int, action_id: str, action_row: dict,
                 video_url: str = "") -> dict:
    """Send one proposal to chat_id with inline keyboard. Returns Telegram API response."""
    text = format_proposal(action_row, video_url=video_url)
    return send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=action_keyboard(action_id),
    )


def send_proposals_batch(chat_id: int, actions: list[dict],
                         video_url: str = "") -> list[dict]:
    """Send each action as its own message so each gets its own inline keyboard.

    Returns list of (action_id, telegram_response).
    """
    responses = []
    for action in actions:
        aid = action.get("action_id")
        if not aid:
            continue
        resp = send_proposal(chat_id, aid, action, video_url=video_url)
        responses.append({"action_id": aid, "response": resp})
    return responses


def handle_update(update: dict) -> Optional[dict]:
    """Handle one Telegram Update (message or callback_query). Returns what we did."""
    token = get_telegram_bot_token()
    if not token:
        return {"error": "TELEGRAM_BOT_TOKEN not set"}

    if "message" in update:
        msg = update["message"]
        chat_id = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id:
            return None

        if text.startswith("/start") or text == "/start@yourbot":
            set_operator_chat_id(chat_id)
            send_message(
                chat_id,
                "Welcome to justdumpit-agent. You'll receive action proposals here. "
                "Tap Approve / Reject / Changes on each one, or reply with "
                "/approve act_xxx, /reject act_xxx, /changes act_xxx: notes.",
            )
            return {"registered_chat_id": chat_id}

        m = re.match(r"^/(approve|reject|changes)\s+(\S+)(?:\s+(.*))?$", text, re.DOTALL)
        if m and chat_id == get_operator_chat_id():
            verb, target_aid, note = m.group(1), m.group(2), m.group(3)
            if target_aid.startswith("act_"):
                result = apply_decision(target_aid, verb, note=note)
                send_message(chat_id, f"Decision recorded: <b>{verb}</b> {target_aid}"
                                    + (f"\nNote: {note}" if note else ""))
                return result
            send_message(chat_id, f"Unknown action id: {target_aid}")
            return None

        return {"received_message": text}

    if "callback_query" in update:
        cb = update["callback_query"]
        data = (cb.get("data") or "").strip()
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        callback_id = cb.get("id")
        if not chat_id:
            return None

        if chat_id != get_operator_chat_id():
            answer_callback_query(
                callback_id,
                text="This bot is configured for a single operator. /start on your own instance to claim it.",
                show_alert=True,
            )
            return {"unauthorized_chat": chat_id}

        verb, _, target_aid = data.partition(":")
        if target_aid.startswith("act_"):
            apply_decision(target_aid, verb, note=None)
            answer_callback_query(
                callback_id,
                text=f"Decision recorded: {verb} {target_aid}",
            )
            send_message(
                chat_id,
                f"<b>{verb}</b> applied to {target_aid}.",
            )
            return {"applied": verb, "action_id": target_aid}

    return None


import re  # at end to avoid circular imports for type hints