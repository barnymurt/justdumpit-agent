"""Executor: tier 0/1 actions.

- tier_0_auto: filesystem writes to /tmp, /data, or ~/scratch/
- tier_1_auto_with_notification: opens a draft GitHub PR whose body
  describes the proposed work. Operator can close it (reject) or fill it
  in (approve + execute).

Tier 2/3/4 actions are NOT executed here — they require explicit operator
greenlight via the API. The auditor records them as 'awaiting_greenlight'
or 'rejected' (tier 4 = 'halted').
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

import httpx

from src.config import get_gh_owner, get_gh_token


log = logging.getLogger("justdumpit_agent.executor")


def _run_gh(*args: str, cwd: Optional[str] = None, timeout: int = 60) -> tuple[int, str, str]:
    """Run a `gh` CLI command. Returns (returncode, stdout, stderr)."""
    env = os.environ.copy()
    if get_gh_token():
        env["GH_TOKEN"] = get_gh_token()
    env["GH_PROMPT_DISABLED"] = "1"
    env["NO_COLOR"] = "1"
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"gh {args[0] if args else ''} timed out after {timeout}s"
    except FileNotFoundError:
        return -1, "", "gh CLI not found in PATH"
    except Exception as e:
        return -1, "", f"gh invocation failed: {e}"


def _gh_api(method: str, path: str, json_body: Optional[dict] = None) -> tuple[int, dict]:
    headers = {
        "Authorization": f"Bearer {get_gh_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    r = httpx.request(
        method, f"https://api.github.com{path}",
        headers=headers, json=json_body, timeout=30.0,
    )
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


# ---------------------------------------------------------------------------
# Tier 0 — filesystem writes (scratch / notes)
# ---------------------------------------------------------------------------


def execute_tier_0(action: dict, action_id: str) -> dict:
    """Execute a tier_0 action. Narrow Phase 1 scope:
    - If a dependency includes 'fs_path: <path>' and that path is under
      /tmp, /data, or ~/scratch/, write a JSON note there.
    - Otherwise no-op with a stub artifact.
    """
    deps = action.get("dependencies", []) or []
    target_path: Optional[str] = None
    for dep in deps:
        if isinstance(dep, str) and dep.startswith("fs_path:"):
            target_path = dep.split(":", 1)[1].strip()
            break

    if not target_path:
        return {"ok": False, "reason": "no fs_path dependency — nothing to do"}

    p = Path(target_path)
    safe_prefixes = ("/tmp/", "/data/", os.path.expanduser("~/scratch/"))
    if not any(str(p).startswith(pref) for pref in safe_prefixes):
        return {
            "ok": False,
            "reason": f"refusing to write outside safe prefixes ({safe_prefixes}): {target_path}",
        }

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({
            "action_id": action_id,
            "description": action.get("action_description", ""),
            "created_by": "justdumpit-agent",
            "atom_ids": action.get("atoms_used", []),
        }, indent=2),
        encoding="utf-8",
    )
    return {"ok": True, "artifact": {"type": "file_write", "path": str(p)}}


# ---------------------------------------------------------------------------
# Tier 1 — draft PR with proposal body
# ---------------------------------------------------------------------------


def _pr_title(action: dict) -> str:
    desc = action.get("action_description", "agent action").strip()
    return f"[agent] {desc[:60]}" + ("..." if len(desc) > 60 else "")


def _pr_body(action: dict, video_id: str, video_url: str) -> str:
    return (
        "## Proposal\n\n"
        f"{action.get('action_description', '(no description)')}\n\n"
        "## Context\n\n"
        f"- Source video: `{video_id}` ([link]({video_url or 'https://www.youtube.com/'}))\n"
        f"- Goal: `{action.get('goal_id', '?')}`\n"
        f"- Atoms used: {', '.join(action.get('atoms_used', []) or []) or '(none)'}\n"
        f"- Stage 2 relevance: {action.get('stage2_relevance', '?')}/3\n\n"
        "## Notes\n\n"
        "- This draft PR was opened by `justdumpit-agent` as a proposal.\n"
        "- Close the PR without merging to **reject** the action.\n"
        "- Push commits to **approve and continue** the work.\n"
        "- Operator notified by email with this PR URL.\n"
    )


def _branch_already_exists(repo: str, branch: str) -> bool:
    rc, out, err = _run_gh("api", f"repos/{get_gh_owner()}/{repo}/git/refs/heads/{branch}")
    return rc == 0 and out.strip().startswith("{")


def execute_tier_1(
    action: dict,
    action_id: str,
    target_repo: str,
    default_branch: str = "main",
    video_url: str = "",
) -> dict:
    """Open a draft PR against `target_repo` with the proposal as body.

    Phase 1: creates a new branch from default_branch, commits a tiny
    stub file (so the PR has a real diff), opens draft PR with the body.
    """
    if not target_repo:
        return {"ok": False, "reason": "no target_repo resolved"}

    branch = f"agent/{action_id[:12]}"

    rc, _, branch_err = _run_gh(
        "api", f"repos/{get_gh_owner()}/{target_repo}/git/refs/heads/{default_branch}",
    )
    if rc != 0:
        return {"ok": False, "reason": f"could not fetch base branch: {branch_err.strip()[:200]}"}

    if not _branch_already_exists(target_repo, branch):
        base_sha_resp, _, _ = _run_gh(
            "api", f"repos/{get_gh_owner()}/{target_repo}/git/refs/heads/{default_branch}",
        )
        try:
            base_sha = json.loads(base_sha_resp)["object"]["sha"]
        except (json.JSONDecodeError, KeyError):
            return {"ok": False, "reason": "could not parse base SHA"}

        rc, _, branch_create_err = _run_gh(
            "api", "--method", "POST",
            f"repos/{get_gh_owner()}/{target_repo}/git/refs",
            "-f", f"ref=refs/heads/{branch}",
            "-f", f"sha={base_sha}",
        )
        if rc != 0 and "Reference already exists" not in (branch_create_err or ""):
            return {"ok": False, "reason": f"branch create failed: {branch_create_err.strip()[:200]}"}

    stub_content = (
        f"# Agent scaffold — {action_id}\n\n"
        f"Opened by `justdumpit-agent`. See the PR description for context.\n"
    )
    content_b64 = base64.b64encode(stub_content.encode("utf-8")).decode("ascii")
    commit_msg = f"agent: scaffold for {action_id}"
    rc, _, commit_err = _run_gh(
        "api", "--method", "PUT",
        f"repos/{get_gh_owner()}/{target_repo}/contents/AGENT_SCAFFOLD.md",
        "-f", f"message={commit_msg}",
        "-f", f"branch={branch}",
        "-f", f"content={content_b64}",
    )
    if rc != 0:
        return {"ok": False, "reason": f"commit failed: {commit_err.strip()[:200]}"}

    title = _pr_title(action)
    body = _pr_body(action, action.get("video_id", "unknown"), video_url)
    rc, pr_stdout, pr_err = _run_gh(
        "pr", "create",
        "--repo", target_repo,
        "--base", default_branch,
        "--head", branch,
        "--title", title,
        "--body", body,
        "--draft",
    )
    if rc != 0:
        return {"ok": False, "reason": f"pr create failed: {pr_err.strip()[:200]}"}

    pr_url = ""
    for line in pr_stdout.splitlines():
        if line.startswith("https://"):
            pr_url = line.strip()
            break

    return {
        "ok": True,
        "artifact": {
            "type": "draft_pr",
            "repo": target_repo,
            "branch": branch,
            "pr_url": pr_url,
        },
    }