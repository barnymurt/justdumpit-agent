"""Config loaders: realm.yaml + gh_repos.yaml + remote goals fetch.

Loads at startup. Validates structure. Exposes typed dataclasses for the rest
of the codebase to use without re-reading YAML.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


log = logging.getLogger("justdumpit_agent.config")


# ---------------------------------------------------------------------------
# Realm config
# ---------------------------------------------------------------------------


@dataclass
class RealmConfig:
    realms: dict[str, list[str]] = field(default_factory=dict)
    owner: str = ""
    version: int = 1
    last_reviewed: str = ""


REALM_FILENAME = "realm.yaml"


def _config_path(filename: str) -> Path:
    env = os.getenv("AGENT_CONFIG_PATH", "").strip()
    if env:
        return Path(env) / filename
    candidates = [
        Path(__file__).parent.parent / "config" / filename,
        Path("/app/config") / filename,
        Path("/data") / filename,
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        f"{filename} not found. Tried: {[str(p) for p in candidates]}. "
        f"Set AGENT_CONFIG_PATH or place the file in config/."
    )


def load_realm() -> RealmConfig:
    p = _config_path(REALM_FILENAME)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{REALM_FILENAME}: top-level must be a mapping")
    realms = raw.get("realms", {})
    if not isinstance(realms, dict):
        raise ValueError(f"{REALM_FILENAME}: realms must be a mapping")
    valid_keys = {"live_product_owned_by_me", "client_or_third_party_work", "scratch", "frozen"}
    for key in realms:
        if key not in valid_keys:
            log.warning("realm.yaml: unknown realm '%s' (will be ignored by tier rules)", key)
    for key, values in realms.items():
        if not isinstance(values, list):
            raise ValueError(f"{REALM_FILENAME}: realms.{key} must be a list")
    return RealmConfig(
        realms=realms,
        owner=str(raw.get("owner", "")),
        version=int(raw.get("version", 1)),
        last_reviewed=str(raw.get("last_reviewed", "")),
    )


def repo_realm(realm_cfg: RealmConfig, repo_name: str) -> str:
    """Map a repo name to its realm. Returns 'unknown' if no match."""
    repo_lower = repo_name.lower()
    for realm_key, patterns in realm_cfg.realms.items():
        for pattern in patterns:
            if pattern.endswith("*"):
                if repo_lower.startswith(pattern[:-1].lower()):
                    return realm_key
            elif pattern.lower() == repo_lower:
                return realm_key
    return "unknown"


# ---------------------------------------------------------------------------
# GitHub repos config
# ---------------------------------------------------------------------------


@dataclass
class GitHubRepoConfig:
    name: str
    default_branch: str = "main"
    description: str = ""
    proposal_label: str = "agent:proposal"
    disabled: bool = False


@dataclass
class GitHubReposConfig:
    repos: list[GitHubRepoConfig] = field(default_factory=list)
    owner: str = ""
    version: int = 1


def load_gh_repos() -> GitHubReposConfig:
    p = _config_path("gh_repos.yaml")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("gh_repos.yaml: top-level must be a mapping")
    repos_raw = raw.get("repos", [])
    if not isinstance(repos_raw, list):
        raise ValueError("gh_repos.yaml: repos must be a list")
    repos: list[GitHubRepoConfig] = []
    for r in repos_raw:
        if not isinstance(r, dict):
            continue
        repos.append(GitHubRepoConfig(
            name=str(r.get("name", "")).strip(),
            default_branch=str(r.get("default_branch", "main")).strip() or "main",
            description=str(r.get("description", "")).strip(),
            proposal_label=str(r.get("proposal_label", "agent:proposal")).strip(),
            disabled=bool(r.get("disabled", False)),
        ))
    return GitHubReposConfig(
        repos=repos,
        owner=str(raw.get("owner", "")),
        version=int(raw.get("version", 1)),
    )


def get_repo(cfg: GitHubReposConfig, repo_name: str) -> Optional[GitHubRepoConfig]:
    for r in cfg.repos:
        if r.name.lower() == repo_name.lower():
            return r
    return None


# ---------------------------------------------------------------------------
# Goals (fetched from justdumpit-ytscraper — single source of truth)
# ---------------------------------------------------------------------------


def fetch_goals_from_justdumpit() -> dict:
    """Fetch the goals.yaml-derived JSON from justdumpit's /goals endpoint.

    Returns a dict that mirrors GoalsConfig.to_dict(). Falls back to {} if
    justdumpit is unreachable or endpoint not available — the caller must
    handle that gracefully (skip action validation against goals).
    """
    import httpx

    from src.config import get_justdumpit_url, get_justdumpit_api_token

    base = get_justdumpit_url()
    headers = {}
    if get_justdumpit_api_token():
        headers["Authorization"] = f"Bearer {get_justdumpit_api_token()}"

    try:
        r = httpx.get(f"{base}/goals", headers=headers, timeout=10.0)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("Failed to fetch goals from %s: %s", base, e)
        return {}