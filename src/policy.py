"""Policy engine: re-validate Stage 2 actions against current goals + tier overrides + realm map.

Every action the agent considers MUST pass through `evaluate_action`. The
function is pure (no I/O) and returns a `PolicyDecision` describing:
- whether the action is allowed at all
- the FINAL tier (after applying tier_overrides and realm map)
- the realm the action belongs to (if inferable from action description)
- reasons for any demotion or halt
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Optional

from src.agent_config import RealmConfig, repo_realm
from src.goals_types import GoalsConfig, Goal


log = logging.getLogger("justdumpit_agent.policy")


TIER_ORDER = {
    "tier_0_auto": 0,
    "tier_1_auto_with_notification": 1,
    "tier_2_propose_with_artifact": 2,
    "tier_3_explicit_green_light": 3,
    "tier_4_hard_stop": 4,
}


@dataclass
class PolicyDecision:
    allowed: bool
    raw_tier: str
    final_tier: str
    realm: str
    reason: str = ""
    goal_id: Optional[str] = None
    atom_ids: list[str] = field(default_factory=list)
    # If the action targets a specific repo (we extract this from action_description
    # or atoms. Phase 1 heuristic — Phase 2 use the goal constraints to specify)
    target_repo: Optional[str] = None


def action_id(
    video_id: str,
    goal_id: str,
    action_description: str,
) -> str:
    """Stable identifier for an action. Same input → same id. Idempotency key."""
    h = hashlib.sha256(f"{video_id}|{goal_id}|{action_description}".encode("utf-8")).hexdigest()
    return f"act_{h[:16]}"


def _infer_target_repo(action_description: str, atoms_used: list[str], dependencies: list[str]) -> Optional[str]:
    """Best-effort: find a repo name mentioned in the action's description / deps.

    Returns None if we can't infer it. Phase 2 will use a more structured field
    in the proposed_action.

    Order matters: longest (most specific) candidates are tried first so
    "justdumpit-agent" matches before "justdumpit" doesn't fire on its prefix.
    """
    text = " ".join([action_description, " ".join(atoms_used), " ".join(dependencies)]).lower()
    candidates = [
        "justdumpit-experiments",
        "justdumpit-agent",
        "hazelrigg-admin",
        "never-touch-this",
        "hazelrigg",
        "justdumpit",
        "mcppay",
        "fit50",
        "industrailead",
        "trg",
        "jobhunt",
        "reportdash",
        "aiconsult",
        "verdoplay",
    ]
    for c in candidates:
        if c in text:
            return c
    return None


def evaluate_action(
    action: dict,
    video_id: str,
    goals_cfg: GoalsConfig,
    realm_cfg: RealmConfig,
    video_extraction: Optional[dict] = None,
) -> PolicyDecision:
    """Apply the full policy chain. Pure function — no I/O.

    action: a Stage 2 proposed_action dict
    video_id: for action_id generation
    goals_cfg: parsed GoalsConfig (for goal_id validation)
    realm_cfg: parsed RealmConfig (for realm lookup)
    video_extraction: the stored Stage 1 extraction, used to verify atoms_used
    """
    goal_id = action.get("goal_id", "")
    raw_tier = action.get("proposed_tier", "tier_2_propose_with_artifact")
    atoms_used = list(action.get("atoms_used", []) or [])
    deps = list(action.get("dependencies", []) or [])
    description = action.get("action_description", "")

    target_repo = _infer_target_repo(description, atoms_used, deps)

    if goal_id not in {g.id for g in goals_cfg.goals}:
        return PolicyDecision(
            allowed=False,
            raw_tier=raw_tier,
            final_tier="tier_4_hard_stop",
            realm=repo_realm(realm_cfg, target_repo) if target_repo else "unknown",
            reason=f"goal_id {goal_id!r} not in current goals.yaml",
            goal_id=goal_id,
            atom_ids=atoms_used,
            target_repo=target_repo,
        )

    if video_extraction and video_extraction.get("transferable_atoms"):
        valid_atom_ids = {a.get("id", "") for a in video_extraction["transferable_atoms"]}
        invalid_atoms = [a for a in atoms_used if a not in valid_atom_ids]
        if invalid_atoms:
            return PolicyDecision(
                allowed=False,
                raw_tier=raw_tier,
                final_tier="tier_4_hard_stop",
                realm=repo_realm(realm_cfg, target_repo) if target_repo else "unknown",
                reason=f"atoms_used contains invalid atom ids: {invalid_atoms}",
                goal_id=goal_id,
                atom_ids=atoms_used,
                target_repo=target_repo,
            )

    final_tier = raw_tier

    if target_repo:
        realm = repo_realm(realm_cfg, target_repo)
        if realm == "frozen":
            final_tier = "tier_4_hard_stop"
            return PolicyDecision(
                allowed=False,
                raw_tier=raw_tier,
                final_tier=final_tier,
                realm=realm,
                reason=f"target repo {target_repo!r} is in 'frozen' realm — never execute",
                goal_id=goal_id,
                atom_ids=atoms_used,
                target_repo=target_repo,
            )

        if realm == "live_product_owned_by_me":
            impact = action.get("impact_classification")
            if impact == "substantial":
                final_tier = "tier_3_explicit_green_light"
            elif impact == "minor":
                final_tier = "tier_2_propose_with_artifact"
            else:
                final_tier = "tier_2_propose_with_artifact"

        if realm == "client_or_third_party_work":
            final_tier = "tier_3_explicit_green_light"
    else:
        realm = "unknown"

    for dep in deps:
        dep_l = dep.lower()
        if any(token in dep_l for token in ["secret", "credential", "auth", "wallet"]):
            final_tier = "tier_4_hard_stop"
            return PolicyDecision(
                allowed=False,
                raw_tier=raw_tier,
                final_tier=final_tier,
                realm=realm,
                reason=f"action touches credentials / secrets ({dep!r})",
                goal_id=goal_id,
                atom_ids=atoms_used,
                target_repo=target_repo,
            )
        if any(token in dep_l for token in ["spend", "payment", "invoice", "subscription", "bill"]):
            if TIER_ORDER.get(final_tier, 0) < TIER_ORDER["tier_3_explicit_green_light"]:
                final_tier = "tier_3_explicit_green_light"

    if final_tier not in TIER_ORDER:
        final_tier = "tier_2_propose_with_artifact"

    allowed = TIER_ORDER[final_tier] < TIER_ORDER["tier_4_hard_stop"]
    note = ""
    if final_tier != raw_tier:
        note = f"tier bumped {raw_tier} → {final_tier}"

    return PolicyDecision(
        allowed=allowed,
        raw_tier=raw_tier,
        final_tier=final_tier,
        realm=realm,
        reason=note,
        goal_id=goal_id,
        atom_ids=atoms_used,
        target_repo=target_repo,
    )


# goals_types is imported lazily; we'll create that module next.
from src import goals_types  # noqa: E402