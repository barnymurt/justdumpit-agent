"""Minimal GoalsConfig type used by the agent for validation.

The agent fetches goals from justdumpit's /goals endpoint, which returns
a JSON mirror of the parsed GoalsConfig. This module defines just enough
of that shape for the agent to validate goal_id + tier validity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


VALID_TIERS = {
    "tier_0_auto",
    "tier_1_auto_with_notification",
    "tier_2_propose_with_artifact",
    "tier_3_explicit_green_light",
    "tier_4_hard_stop",
}


@dataclass
class GoalConstraints:
    required_evidence: Optional[str] = None
    max_effort_per_action_hours: Optional[int] = None
    exploration_bonus: Optional[str] = None


@dataclass
class Goal:
    id: str
    name: str = ""
    priority: int = 99
    description: str = ""
    scoring_rubric: dict = field(default_factory=dict)
    constraints: GoalConstraints = field(default_factory=GoalConstraints)
    default_authority: str = "tier_2_propose_with_artifact"


@dataclass
class GoalsConfig:
    version: int = 0
    owner: str = ""
    goals: list[Goal] = field(default_factory=list)
    authority_tier_keys: list[str] = field(default_factory=list)
    atom_types: list[str] = field(default_factory=list)
    atom_evidence: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "GoalsConfig":
        goals = []
        for g in d.get("goals", []) or []:
            if not isinstance(g, dict):
                continue
            c = g.get("constraints") or {}
            goals.append(Goal(
                id=str(g.get("id", "")),
                name=str(g.get("name", "")),
                priority=int(g.get("priority", 99)),
                description=str(g.get("description", "")),
                scoring_rubric={int(k): v for k, v in (g.get("scoring_rubric") or {}).items()},
                constraints=GoalConstraints(
                    required_evidence=c.get("required_evidence"),
                    max_effort_per_action_hours=c.get("max_effort_per_action_hours"),
                    exploration_bonus=c.get("exploration_bonus"),
                ),
                default_authority=str(g.get("default_authority", "tier_2_propose_with_artifact")),
            ))
        return cls(
            version=int(d.get("version", 0)),
            owner=str(d.get("owner", "")),
            goals=goals,
            authority_tier_keys=list(d.get("authority_tier_keys", []) or []),
            atom_types=list(d.get("atom_types", []) or []),
            atom_evidence=list(d.get("atom_evidence", []) or []),
        )

    def goal_ids(self) -> set[str]:
        return {g.id for g in self.goals}

    def get_goal(self, goal_id: str) -> Optional[Goal]:
        for g in self.goals:
            if g.id == goal_id:
                return g
        return None


def empty_goals() -> GoalsConfig:
    """Returned when justdumpit's /goals endpoint fails. The agent must treat
    this as 'cannot validate goal_ids — surface as rejection'."""
    return GoalsConfig(version=0, owner="unknown", goals=[])