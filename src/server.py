"""FastAPI server for justdumpit-agent.

Endpoints:
- POST /internal/event           # webhook from justdumpit-ytscraper
- GET  /health
- GET  /status
- GET  /queue                    # actions awaiting decision
- GET  /history                  # full action log
- GET  /action/<id>              # single action detail
- POST /action/<id>/approve      # mark approved
- POST /action/<id>/reject       # mark rejected
- POST /action/<id>/execute      # explicitly execute after approval
- POST /cron/run                 # manual trigger of the cron fallback
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from src import auditor, dispatcher, goals_client, poller
from src.agent_config import load_gh_repos, load_realm
from src.channels import telegram as tg


log = logging.getLogger("justdumpit_agent.server")


app = FastAPI(title="justdumpit-agent", version="0.1.0")

REALM_CFG = load_realm()
GH_CFG = load_gh_repos()


_poller_thread: Optional[threading.Thread] = None
_poller_stop = threading.Event()


@app.on_event("startup")
def _startup() -> None:
    auditor.init_db()
    if poller.should_start():
        poller.start_background()


@app.on_event("shutdown")
def _shutdown() -> None:
    poller.stop_background()


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/status")
def status() -> dict:
    return {
        "ok": True,
        "realms": list(REALM_CFG.realms.keys()),
        "gh_repos": [r.name for r in GH_CFG.repos],
        "audit_stats": auditor.stats(),
        "poller_running": poller.is_running(),
        "poller_interval_seconds": poller.interval_seconds(),
    }


# ---------------------------------------------------------------------------
# Webhook from justdumpit
# ---------------------------------------------------------------------------


class InternalEvent(BaseModel):
    video_id: str = Field(..., description="11-char YouTube video ID")
    video_url: str = Field("", description="Source URL (optional)")
    stage2: Optional[dict] = Field(None, description="Stage 2 payload; if omitted, fetched from justdumpit")


@app.post("/internal/event")
def internal_event(evt: InternalEvent) -> dict:
    log.info("internal_event video_id=%s", evt.video_id)
    report = dispatcher.process_video_payload(
        video_id=evt.video_id,
        video_url=evt.video_url,
        stage2=evt.stage2,
        realm_cfg=REALM_CFG,
        gh_cfg=GH_CFG,
    )
    return report


# ---------------------------------------------------------------------------
# Queue / history / approve / reject / execute
# ---------------------------------------------------------------------------


@app.get("/queue")
def get_queue(status: str = "awaiting_greenlight", limit: int = 50) -> dict:
    rows = auditor.list_actions(status=status, limit=limit)
    return {"count": len(rows), "actions": rows}


@app.get("/history")
def get_history(video_id: Optional[str] = None, limit: int = 100) -> dict:
    rows = auditor.list_actions(video_id=video_id, limit=limit)
    stats = auditor.stats()
    return {"count": len(rows), "stats": stats, "actions": rows}


@app.get("/action/{action_id}")
def get_action(action_id: str) -> dict:
    row = auditor.get_action(action_id)
    if row is None:
        raise HTTPException(status_code=404, detail="action not found")
    return row


@app.get("/action/{action_id}/transcript-context")
def get_action_transcript_context(action_id: str):
    """Fetch the transcript ranges for all atoms_used in this action.

    Resolves atom timestamp strings ("MM:SS-MM:SS") to seconds, calls
    justdumpit's /video/{id}/transcript/range per atom, and returns
    the concatenated segments so an operator (or downstream agent)
    can drill into the source wording.
    """
    from src import goals_client
    from src.policy import parse_timestamp_range

    row = auditor.get_action(action_id)
    if row is None:
        raise HTTPException(status_code=404, detail="action not found")

    atom_ids = row.get("atom_ids") or []
    if not isinstance(atom_ids, list):
        atom_ids = []
    video_id = row.get("video_id")

    extraction_resp = goals_client.get_extraction(video_id)
    atoms = (extraction_resp or {}).get("transferable_atoms", []) or []
    atom_by_id = {a.get("id"): a for a in atoms if a.get("id")}

    contexts = []
    for aid in atom_ids:
        atom = atom_by_id.get(aid)
        if not atom:
            contexts.append({"atom_id": aid, "found": False})
            continue
        ts = atom.get("timestamp") or ""
        parsed = parse_timestamp_range(str(ts))
        if not parsed:
            contexts.append({"atom_id": aid, "found": True, "transcript": None,
                             "reason": f"unparseable timestamp: {ts!r}"})
            continue
        start, end = parsed
        segs = goals_client.get_transcript_range(video_id, start, end) or {}
        contexts.append({
            "atom_id": aid,
            "found": True,
            "timestamp": ts,
            "start": start,
            "end": end,
            "segments": segs.get("segments", []),
            "transcript_video_url": f"https://www.youtube.com/watch?v={video_id}&t={int(start)}s",
        })

    return {
        "action_id": action_id,
        "video_id": video_id,
        "atoms": contexts,
    }


class DecisionRequest(BaseModel):
    note: Optional[str] = None


@app.post("/action/{action_id}/approve")
def approve_action(action_id: str, body: Optional[DecisionRequest] = None) -> dict:
    row = auditor.get_action(action_id)
    if row is None:
        raise HTTPException(status_code=404, detail="action not found")
    if row["status"] not in ("awaiting_greenlight",):
        raise HTTPException(
            status_code=409,
            detail=f"action is in status {row['status']!r}; only 'awaiting_greenlight' can be approved",
        )
    note = (body or DecisionRequest()).note
    updated = auditor.update_action_status(action_id, "approved", artifacts={"approval_note": note} if note else None)
    return updated


@app.post("/action/{action_id}/reject")
def reject_action(action_id: str, body: Optional[DecisionRequest] = None) -> dict:
    row = auditor.get_action(action_id)
    if row is None:
        raise HTTPException(status_code=404, detail="action not found")
    note = (body or DecisionRequest()).note
    updated = auditor.update_action_status(
        action_id, "rejected",
        rejection_reason=note or "operator rejected",
    )
    return updated


@app.post("/action/{action_id}/execute")
def execute_action(action_id: str) -> dict:
    """Operator-triggered execution of an approved action. Phase 1 scope:
    tier_0/1 → re-run executor; tier_2/3 → write a draft issue as proposal (Phase 2).
    """
    row = auditor.get_action(action_id)
    if row is None:
        raise HTTPException(status_code=404, detail="action not found")
    if row["status"] not in ("approved",):
        raise HTTPException(
            status_code=409,
            detail=f"action is in status {row['status']!r}; must be 'approved' to execute",
        )

    final_tier = row["final_tier"]
    target_repo = row.get("target_repo")
    video_id = row["video_id"]

    from src import executor

    if final_tier == "tier_0_auto":
        action_dict = {
            "action_description": row["action_description"],
            "dependencies": row.get("dependencies", []),
            "atoms_used": row.get("atom_ids", []),
        }
        result = executor.execute_tier_0(action_dict, action_id)
        if result.get("ok"):
            return auditor.update_action_status(action_id, "executed", artifacts=result.get("artifact"))
        return auditor.update_action_status(
            action_id, "executed_failed",
            rejection_reason=result.get("reason"),
        )

    if final_tier == "tier_1_auto_with_notification":
        action_dict = {
            "action_description": row["action_description"],
            "dependencies": row.get("dependencies", []),
            "atoms_used": row.get("atom_ids", []),
            "goal_id": row.get("goal_id"),
            "video_id": video_id,
            "stage2_relevance": row.get("stage2_relevance", 0),
        }
        repo_cfg = next((r for r in GH_CFG.repos if r.name == target_repo), None)
        default_branch = repo_cfg.default_branch if repo_cfg else "main"
        result = executor.execute_tier_1(
            action_dict, action_id,
            target_repo=target_repo or "",
            default_branch=default_branch,
        )
        if result.get("ok"):
            return auditor.update_action_status(action_id, "pr_opened", artifacts=result.get("artifact"))
        return auditor.update_action_status(
            action_id, "executed_failed",
            rejection_reason=result.get("reason"),
        )

    if final_tier in ("tier_2_propose_with_artifact", "tier_3_explicit_green_light"):
        auditor.update_action_status(
            action_id, "proposal_drafted",
            artifacts={"note": "Phase 2: open GitHub issue proposal here"},
        )
        return auditor.get_action(action_id)

    raise HTTPException(status_code=400, detail=f"tier {final_tier!r} not executable in Phase 1")


# ---------------------------------------------------------------------------
# Manual cron trigger
# ---------------------------------------------------------------------------


@app.post("/cron/run")
def cron_run(limit: int = 20) -> dict:
    report = poller.run_once(limit=limit)
    return report


@app.post("/internal/email-poll")
def email_poll_endpoint() -> dict:
    """Trigger one pass of the email reply poller. Manual trigger for testing."""
    return poller.email_poll_once()


@app.post("/telegram/webhook")
def telegram_webhook_endpoint(update: dict) -> dict:
    """Receive Telegram updates (messages + callback queries)."""
    result = tg.handle_update(update)
    return {"ok": True, "handled": result}


@app.post("/telegram/setup-webhook")
def telegram_setup_webhook_endpoint(webhook_url: Optional[str] = None) -> dict:
    """Register the bot's webhook with Telegram.

    If `webhook_url` is omitted, builds it from request headers
    (`https://<host>/telegram/webhook`).
    """
    from fastapi import Request
    return {"result": "use /telegram/setup-webhook?webhook_url=... or POST with body"}


@app.post("/telegram/register-webhook")
def telegram_register_webhook(request: Request) -> dict:
    """Register the bot's webhook with Telegram using this server's URL."""
    host = request.headers.get("host", "")
    if not host:
        return {"ok": False, "error": "no host header"}
    url = f"https://{host}/telegram/webhook"
    return {"ok": True, "webhook_url": url, "set_result": tg.set_webhook(url)}


@app.post("/telegram/send-proposals/{video_id}")
def telegram_send_proposals(video_id: str) -> dict:
    """Re-send all awaiting_greenlight actions for a video to the operator via Telegram."""
    chat_id = tg.get_operator_chat_id()
    if not chat_id:
        return {"ok": False, "error": "no operator chat_id; send /start to the bot first"}

    actions = auditor.list_actions(video_id=video_id, status="awaiting_greenlight")
    if not actions:
        return {"ok": False, "error": f"no awaiting_greenlight actions for {video_id}"}

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    responses = tg.send_proposals_batch(chat_id, actions, video_url=video_url)
    return {"ok": True, "chat_id": chat_id, "sent": len(responses), "responses": responses}


@app.post("/telegram/test")
def telegram_test_endpoint(message: str = "Test from justdumpit-agent") -> dict:
    """Send a test message to the registered operator chat."""
    chat_id = tg.get_operator_chat_id()
    if not chat_id:
        return {"ok": False, "error": "no operator chat_id; send /start to the bot first"}
    resp = tg.send_message(chat_id, message)
    return {"ok": True, "chat_id": chat_id, "response": resp}



@app.post("/internal/retrospective")
def retrospective_endpoint() -> dict:
    """Fire the agent webhook for every entry in justdumpit's WL list.

    Useful when the agent was deployed AFTER videos were already
    processed by justdumpit-ytscraper (so they got Stage 2 + email
    but never reached the agent). Idempotent: re-running does nothing
    for actions already in the audit log; re-uses existing GitHub
    issues if they're still open.
    """
    entries = goals_client.list_watch_later_entries(only_pending=False, limit=200)
    processed = []
    skipped = []

    for entry in entries:
        vid = entry.get("video_id")
        if not vid:
            continue
        existing = auditor.list_actions(video_id=vid, limit=200)
        if existing and any(a["status"] in ("executed", "pr_opened", "approved", "proposal_drafted") for a in existing):
            skipped.append({"video_id": vid, "reason": "already processed"})
            continue

        report = dispatcher.process_video_payload(
            video_id=vid,
            video_url=entry.get("video_url", ""),
            stage2=None,
            realm_cfg=REALM_CFG,
            gh_cfg=GH_CFG,
        )
        processed.append({"video_id": vid, "report": report})

    return {
        "scanned": len(entries),
        "processed": len(processed),
        "skipped": len(skipped),
        "processed_details": processed,
        "skipped_details": skipped,
    }
