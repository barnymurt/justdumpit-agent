




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
    processed: list[dict] = []
    skipped: list[dict] = []

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
