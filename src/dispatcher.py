"""Dispatch logic — single entry point for processing a Stage 2 video payload.

`process_video_payload` is called by:
- The webhook handler (server.py:internal_event)
- The cron poller (poller.py:run_once)

It is idempotent — replays of the same video don't double-record actions
because action_id is a stable hash of (video_id, goal_id, action_description).
"""

from __future__ import annotations

import logging
from typing import Optional

from src import auditor, executor, goals_client
from src.agent_config import GitHubReposConfig, RealmConfig, get_repo
from src.goals_types import GoalsConfig, empty_goals
from src.policy import PolicyDecision, action_id as make_action_id, evaluate_action


log = logging.getLogger("justdumpit_agent.dispatcher")


VALID_TIERS = {
    "tier_0_auto",
    "tier_1_auto_with_notification",
    "tier_2_propose_with_artifact",
    "tier_3_explicit_green_light",
    "tier_4_hard_stop",
}


def process_video_payload(
    video_id: str,
    video_url: str = "",
    stage2: Optional[dict] = None,
    *,
    realm_cfg: RealmConfig,
    gh_cfg: GitHubReposConfig,
) -> dict:
    """Process one video's Stage 2 output. Returns a summary report.

    Fetches goals (cached once per call) and re-fetches the Stage 2 payload
    from justdumpit if not supplied (so replays always see the current state).
    """
    if stage2 is None:
        stage2 = goals_client.get_stage2(video_id)
    if not stage2:
        return {"video_id": video_id, "status": "no_stage2", "actions": []}

    goals_dict = goals_client.get_goals()
    goals_cfg = GoalsConfig.from_dict(goals_dict) if goals_dict else empty_goals()
    if not goals_cfg.goals:
        return {"video_id": video_id, "status": "no_goals", "actions": []}

    extraction_resp = goals_client.get_stage2(video_id)
    video_extraction = (extraction_resp or {}).get("extraction") if extraction_resp else None
    if not video_extraction:
        try:
            r = goals_client._headers  # type: ignore
        except Exception:
            pass

    per_goal = stage2.get("per_goal", []) or []
    max_relevance = max((g.get("relevance", 0) for g in per_goal), default=0)
    report_actions = []

    video_extraction = goals_client.get_extraction(video_id)

    for goal_entry in per_goal:
        goal_id = goal_entry.get("goal_id", "")
        relevance = goal_entry.get("relevance", 0)
        for action in goal_entry.get("proposed_actions", []) or []:
            action["goal_id"] = action.get("goal_id") or goal_id
            action["stage2_relevance"] = relevance
            decision = evaluate_action(
                action=action,
                video_id=video_id,
                goals_cfg=goals_cfg,
                realm_cfg=realm_cfg,
                video_extraction=video_extraction,
            )
            record = _dispatch(
                action=action,
                decision=decision,
                video_id=video_id,
                video_url=video_url,
                realm_cfg=realm_cfg,
                gh_cfg=gh_cfg,
            )
            report_actions.append(record)

    return {
        "video_id": video_id,
        "max_relevance": max_relevance,
        "actions_count": len(report_actions),
        "actions": report_actions,
    }


def _dispatch(
    *,
    action: dict,
    decision: PolicyDecision,
    video_id: str,
    video_url: str,
    realm_cfg: RealmConfig,
    gh_cfg: GitHubReposConfig,
) -> dict:
    aid = make_action_id(video_id, decision.goal_id or "?", action.get("action_description", ""))

    if not decision.allowed:
        status = "halted" if decision.final_tier == "tier_4_hard_stop" else "rejected"
        auditor.record_action(
            action_id=aid,
            video_id=video_id,
            goal_id=decision.goal_id or "?",
            atom_ids=decision.atom_ids,
            stage2_relevance=int(action.get("stage2_relevance", 0)),
            raw_tier=decision.raw_tier,
            final_tier=decision.final_tier,
            status=status,
            realm=decision.realm,
            action_description=action.get("action_description", ""),
            effort_hours=int(action.get("effort_estimate_hours", 0) or 0),
            reversibility=action.get("reversibility", "undo_able"),
            external_surface=bool(action.get("external_surface", False)),
            dependencies=list(action.get("dependencies", []) or []),
            artifacts=None,
            rejection_reason=decision.reason,
            policy_note=decision.reason,
            target_repo=decision.target_repo,
        )
        return {
            "action_id": aid,
            "status": status,
            "final_tier": decision.final_tier,
            "reason": decision.reason,
        }

    if decision.final_tier == "tier_0_auto":
        result = executor.execute_tier_0(action, aid)
        if result.get("ok"):
            auditor.record_action(
                action_id=aid,
                video_id=video_id,
                goal_id=decision.goal_id or "?",
                atom_ids=decision.atom_ids,
                stage2_relevance=int(action.get("stage2_relevance", 0)),
                raw_tier=decision.raw_tier,
                final_tier=decision.final_tier,
                status="executed",
                realm=decision.realm,
                action_description=action.get("action_description", ""),
                effort_hours=int(action.get("effort_estimate_hours", 0) or 0),
                reversibility=action.get("reversibility", "undo_able"),
                external_surface=bool(action.get("external_surface", False)),
                dependencies=list(action.get("dependencies", []) or []),
                artifacts=result.get("artifact"),
                rejection_reason=None,
                policy_note=decision.reason or None,
                target_repo=decision.target_repo,
            )
            return {"action_id": aid, "status": "executed", "artifact": result.get("artifact")}
        else:
            auditor.update_action_status(
                aid, "executed_failed",
                rejection_reason=result.get("reason"),
            ) if _action_exists(aid) else auditor.record_action(
                action_id=aid,
                video_id=video_id,
                goal_id=decision.goal_id or "?",
                atom_ids=decision.atom_ids,
                stage2_relevance=int(action.get("stage2_relevance", 0)),
                raw_tier=decision.raw_tier,
                final_tier=decision.final_tier,
                status="executed_failed",
                realm=decision.realm,
                action_description=action.get("action_description", ""),
                effort_hours=int(action.get("effort_estimate_hours", 0) or 0),
                reversibility=action.get("reversibility", "undo_able"),
                external_surface=bool(action.get("external_surface", False)),
                dependencies=list(action.get("dependencies", []) or []),
                artifacts=None,
                rejection_reason=result.get("reason"),
                policy_note=decision.reason or None,
                target_repo=decision.target_repo,
            )
            return {"action_id": aid, "status": "executed_failed", "reason": result.get("reason")}

    if decision.final_tier == "tier_1_auto_with_notification":
        target_repo = decision.target_repo
        repo_cfg = get_repo(gh_cfg, target_repo) if target_repo else None
        default_branch = repo_cfg.default_branch if repo_cfg else "main"

        result = executor.execute_tier_1(
            action,
            aid,
            target_repo=target_repo or "",
            default_branch=default_branch,
            video_url=video_url,
        )
        if result.get("ok"):
            auditor.record_action(
                action_id=aid,
                video_id=video_id,
                goal_id=decision.goal_id or "?",
                atom_ids=decision.atom_ids,
                stage2_relevance=int(action.get("stage2_relevance", 0)),
                raw_tier=decision.raw_tier,
                final_tier=decision.final_tier,
                status="pr_opened",
                realm=decision.realm,
                action_description=action.get("action_description", ""),
                effort_hours=int(action.get("effort_estimate_hours", 0) or 0),
                reversibility=action.get("reversibility", "undo_able"),
                external_surface=bool(action.get("external_surface", False)),
                dependencies=list(action.get("dependencies", []) or []),
                artifacts=result.get("artifact"),
                rejection_reason=None,
                policy_note=decision.reason or None,
                target_repo=decision.target_repo,
            )
            return {"action_id": aid, "status": "pr_opened", "artifact": result.get("artifact")}

        auditor.update_action_status(
            aid, "executed_failed",
            rejection_reason=result.get("reason"),
        ) if _action_exists(aid) else auditor.record_action(
            action_id=aid,
            video_id=video_id,
            goal_id=decision.goal_id or "?",
            atom_ids=decision.atom_ids,
            stage2_relevance=int(action.get("stage2_relevance", 0)),
            raw_tier=decision.raw_tier,
            final_tier=decision.final_tier,
            status="executed_failed",
            realm=decision.realm,
            action_description=action.get("action_description", ""),
            effort_hours=int(action.get("effort_estimate_hours", 0) or 0),
            reversibility=action.get("reversibility", "undo_able"),
            external_surface=bool(action.get("external_surface", False)),
            dependencies=list(action.get("dependencies", []) or []),
            artifacts=None,
            rejection_reason=result.get("reason"),
            policy_note=decision.reason or None,
            target_repo=decision.target_repo,
        )
        return {"action_id": aid, "status": "executed_failed", "reason": result.get("reason")}

    if decision.final_tier in ("tier_2_propose_with_artifact", "tier_3_explicit_green_light"):
        auditor.record_action(
            action_id=aid,
            video_id=video_id,
            goal_id=decision.goal_id or "?",
            atom_ids=decision.atom_ids,
            stage2_relevance=int(action.get("stage2_relevance", 0)),
            raw_tier=decision.raw_tier,
            final_tier=decision.final_tier,
            status="awaiting_greenlight",
            realm=decision.realm,
            action_description=action.get("action_description", ""),
            effort_hours=int(action.get("effort_estimate_hours", 0) or 0),
            reversibility=action.get("reversibility", "undo_able"),
            external_surface=bool(action.get("external_surface", False)),
            dependencies=list(action.get("dependencies", []) or []),
            artifacts=None,
            rejection_reason=None,
            policy_note=decision.reason or "awaiting operator greenlight via /action/<id>/approve",
            target_repo=decision.target_repo,
        )
        return {"action_id": aid, "status": "awaiting_greenlight", "final_tier": decision.final_tier}

    auditor.record_action(
        action_id=aid,
        video_id=video_id,
        goal_id=decision.goal_id or "?",
        atom_ids=decision.atom_ids,
        stage2_relevance=int(action.get("stage2_relevance", 0)),
        raw_tier=decision.raw_tier,
        final_tier=decision.final_tier,
        status="halted",
        realm=decision.realm,
        action_description=action.get("action_description", ""),
        effort_hours=int(action.get("effort_estimate_hours", 0) or 0),
        reversibility=action.get("reversibility", "undo_able"),
        external_surface=bool(action.get("external_surface", False)),
        dependencies=list(action.get("dependencies", []) or []),
        artifacts=None,
        rejection_reason=decision.reason or f"unexpected final_tier {decision.final_tier}",
        policy_note=decision.reason or None,
        target_repo=decision.target_repo,
    )
    return {"action_id": aid, "status": "halted", "reason": decision.reason or decision.final_tier}


def _action_exists(action_id: str) -> bool:
    return auditor.get_action(action_id) is not None