"""Offline tests for policy + dispatcher. Uses fixtures â€” no network needed."""

from src.agent_config import RealmConfig
from src.goals_types import GoalsConfig, Goal, GoalConstraints
from src.policy import action_id, evaluate_action


def _goals() -> GoalsConfig:
    return GoalsConfig(
        version=2,
        owner="barnymurt",
        goals=[
            Goal(
                id="dev_workflow",
                priority=1,
                default_authority="tier_1_auto_with_notification",
                scoring_rubric={0: "n/a", 1: "weak", 2: "spike", 3: "ship"},
                constraints=GoalConstraints(required_evidence="at_least_one_atom_of_type[implementation_pattern]"),
            ),
            Goal(
                id="side_income",
                priority=2,
                default_authority="tier_3_explicit_green_light",
                scoring_rubric={0: "n/a", 1: "weak", 2: "spike", 3: "ship"},
            ),
            Goal(
                id="agent_run_business",
                priority=3,
                default_authority="tier_3_explicit_green_light",
                scoring_rubric={0: "n/a", 1: "weak", 2: "spike", 3: "ship"},
            ),
        ],
    )


def _realm() -> RealmConfig:
    return RealmConfig(
        owner="barnymurt",
        version=1,
        realms={
            "live_product_owned_by_me": ["justdumpit", "mcppay"],
            "client_or_third_party_work": ["hazelrigg"],
            "scratch": ["sandbox-*", "justdumpit-agent"],
            "frozen": [],
        },
    )


def _extraction() -> dict:
    return {
        "transferable_atoms": [
            {"id": "atom_01", "label": "Sub-agent tiering", "type": "implementation_pattern"},
            {"id": "atom_02", "label": "Watchdog agent", "type": "org_pattern"},
            {"id": "atom_03", "label": "Build the factory", "type": "business_model"},
        ]
    }


def test_goal_id_not_in_goals():
    goals = _goals()
    realm = _realm()
    action = {
        "goal_id": "nonexistent",
        "atoms_used": ["atom_01"],
        "proposed_tier": "tier_2_propose_with_artifact",
        "action_description": "do something",
        "external_surface": False,
        "dependencies": [],
    }
    d = evaluate_action(action, "vid1", goals, realm)
    assert d.allowed is False
    assert d.final_tier == "tier_4_hard_stop"
    assert "nonexistent" in d.reason
    print("PASS: unknown goal_id â†’ halted")


def test_atom_id_not_in_extraction():
    goals = _goals()
    realm = _realm()
    action = {
        "goal_id": "agent_run_business",
        "atoms_used": ["atom_99"],
        "proposed_tier": "tier_2_propose_with_artifact",
        "action_description": "do something",
        "external_surface": False,
        "dependencies": [],
    }
    d = evaluate_action(action, "vid1", goals, realm, video_extraction=_extraction())
    assert d.allowed is False
    assert "atom_99" in d.reason
    print("PASS: invalid atom id â†’ halted")


def test_live_product_substantial_bumps_to_tier3():
    goals = _goals()
    realm = _realm()
    action = {
        "goal_id": "agent_run_business",
        "atoms_used": ["atom_01"],
        "proposed_tier": "tier_1_auto_with_notification",
        "action_description": "Add Opus tiering to mcppay orchestrator",
        "external_surface": False,
        "dependencies": [],
        "impact_classification": "substantial",
    }
    d = evaluate_action(action, "vid1", goals, realm, video_extraction=_extraction())
    assert d.allowed is True
    assert d.final_tier == "tier_3_explicit_green_light"
    assert d.target_repo == "mcppay"
    print("PASS: live product + substantial impact â†’ tier_3")


def test_live_product_minor_bumps_to_tier2():
    goals = _goals()
    realm = _realm()
    action = {
        "goal_id": "dev_workflow",
        "atoms_used": ["atom_01"],
        "proposed_tier": "tier_1_auto_with_notification",
        "action_description": "Tweak mcppay README copy",
        "external_surface": False,
        "dependencies": [],
        "impact_classification": "minor",
    }
    d = evaluate_action(action, "vid1", goals, realm, video_extraction=_extraction())
    assert d.allowed is True
    assert d.final_tier == "tier_2_propose_with_artifact"
    print("PASS: live product + minor impact â†’ tier_2")


def test_client_work_bumps_to_tier3():
    goals = _goals()
    realm = _realm()
    action = {
        "goal_id": "agent_run_business",
        "atoms_used": ["atom_01"],
        "proposed_tier": "tier_1_auto_with_notification",
        "action_description": "Apply watchdog pattern to hazelrigg lead-flow",
        "external_surface": False,
        "dependencies": [],
    }
    d = evaluate_action(action, "vid1", goals, realm, video_extraction=_extraction())
    assert d.allowed is True
    assert d.final_tier == "tier_3_explicit_green_light"
    print("PASS: client work â†’ tier_3")


def test_dependencies_secret_halts():
    goals = _goals()
    realm = _realm()
    action = {
        "goal_id": "agent_run_business",
        "atoms_used": ["atom_01"],
        "proposed_tier": "tier_1_auto_with_notification",
        "action_description": "Rotate mcppay auth credentials",
        "external_surface": False,
        "dependencies": ["AWS credentials in vault"],
    }
    d = evaluate_action(action, "vid1", goals, realm, video_extraction=_extraction())
    assert d.allowed is False
    assert d.final_tier == "tier_4_hard_stop"
    print("PASS: secrets/credentials dep â†’ halted")


def test_dependencies_payment_bumps_to_tier3():
    goals = _goals()
    realm = _realm()
    action = {
        "goal_id": "dev_workflow",
        "atoms_used": ["atom_01"],
        "proposed_tier": "tier_1_auto_with_notification",
        "action_description": "Add Stripe subscription billing for mcppay",
        "external_surface": False,
        "dependencies": ["Stripe subscription billing"],
    }
    d = evaluate_action(action, "vid1", goals, realm, video_extraction=_extraction())
    assert d.allowed is True
    assert d.final_tier == "tier_3_explicit_green_light"
    print("PASS: payment dep â†’ tier_3")


def test_scratch_repo_stays_low():
    """Scratch repo: realm maps to 'scratch', no tier override applied.
    Final tier = goal's default_authority (since proposed_tier matched it)."""
    goals = _goals()
    realm = _realm()
    action = {
        "goal_id": "dev_workflow",
        "atoms_used": ["atom_01"],
        "proposed_tier": "tier_1_auto_with_notification",
        "action_description": "Try tiering pattern in justdumpit-agent scratch repo",
        "external_surface": False,
        "dependencies": [],
    }
    d = evaluate_action(action, "vid1", goals, realm, video_extraction=_extraction())
    assert d.allowed is True
    assert d.realm == "scratch"
    assert d.final_tier == "tier_1_auto_with_notification"
    print("PASS: scratch repo preserves goal default tier")


def test_frozen_repo_halts():
    goals = _goals()
    realm = RealmConfig(
        owner="barnymurt",
        version=1,
        realms={
            "live_product_owned_by_me": [],
            "client_or_third_party_work": [],
            "scratch": [],
            "frozen": ["never-touch-this"],
        },
    )
    action = {
        "goal_id": "agent_run_business",
        "atoms_used": ["atom_01"],
        "proposed_tier": "tier_1_auto_with_notification",
        "action_description": "Add a thing to never-touch-this repo",
        "external_surface": False,
        "dependencies": [],
    }
    d = evaluate_action(action, "vid1", goals, realm, video_extraction=_extraction())
    assert d.allowed is False
    assert d.final_tier == "tier_4_hard_stop"
    print("PASS: frozen realm â†’ halted")


def test_action_id_stable():
    a1 = action_id("vid1", "agent_run_business", "do the thing")
    a2 = action_id("vid1", "agent_run_business", "do the thing")
    a3 = action_id("vid1", "agent_run_business", "different thing")
    assert a1 == a2
    assert a1 != a3
    assert a1.startswith("act_")
    print(f"PASS: action_id stable ({a1})")


if __name__ == "__main__":
    test_goal_id_not_in_goals()
    test_atom_id_not_in_extraction()
    test_live_product_substantial_bumps_to_tier3()
    test_live_product_minor_bumps_to_tier2()
    test_client_work_bumps_to_tier3()
    test_dependencies_secret_halts()
    test_dependencies_payment_bumps_to_tier3()
    test_scratch_repo_stays_low()
    test_frozen_repo_halts()
    test_action_id_stable()
    print("\nAll offline policy tests passed.")

