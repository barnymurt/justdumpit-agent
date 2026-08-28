# justdumpit-agent

Downstream action agent that consumes Stage 2 outputs from [justdumpit-ytscraper](https://github.com/barnymurt/justdumpit) and takes goal-aligned actions across the operator's GitHub repos.

## What it does

1. **Receives** a webhook from justdumpit-ytscraper when a video's Stage 2 output has `max_relevance >= 2`
2. **Re-fetches** the Stage 2 payload + goals.yaml from justdumpit (single source of truth — always current)
3. **Re-validates** every proposed action against:
   - Current `goals.yaml` (goal_id must still exist)
   - Stored Stage 1 extraction (`atoms_used` must reference real atoms)
   - Realm map (`config/realm.yaml`) → live_product / client / scratch / frozen
   - Dependency scan → secrets/payment triggers tier bumps
4. **Dispatches** by final tier:
   - **tier_0_auto**: filesystem write to `/tmp`, `/data`, or `~/scratch/`
   - **tier_1_auto_with_notification**: opens draft GitHub PR via `gh` CLI
   - **tier_2 / tier_3**: audit-only at Phase 1; operator approves via API then explicitly executes
   - **tier_4_hard_stop**: never executes; audit logs the halt
5. **Records** everything in a SQLite audit log at `/data/agent_audit.db`
6. **Cron fallback** polls justdumpit every 15 min for missed webhook events (idempotent)

## How to operate

After deployment, the agent exposes a small API:

```bash
# See what's awaiting your greenlight
curl https://justdumpit-agent.fly.dev/queue

# See full history
curl https://justdumpit-agent.fly.dev/history

# Inspect one action
curl https://justdumpit-agent.fly.dev/action/<action_id>

# Approve (tier 2/3 actions need this)
curl -X POST https://justdumpit-agent.fly.dev/action/<action_id>/approve \
     -H 'Content-Type: application/json' -d '{"note": "looks good"}'

# Reject
curl -X POST https://justdumpit-agent.fly.dev/action/<action_id>/reject

# Explicit execute (for approved tier 0/1, or to trigger tier 2/3 proposal drafting)
curl -X POST https://justdumpit-agent.fly.dev/action/<action_id>/execute

# Force the cron poll now
curl -X POST https://justdumpit-agent.fly.dev/cron/run
```

## Configuration

### `config/realm.yaml`

Maps GitHub repos to "realms" that drive `tier_overrides`:

- `live_product_owned_by_me`: tier bump on impact (substantial → tier_3, minor → tier_2)
- `client_or_third_party_work`: always tier_3
- `scratch`: no override
- `frozen`: always tier_4 (never executes)

### `config/gh_repos.yaml`

Repos the agent is allowed to operate on. `disabled: true` excludes a repo.

### Environment variables (Fly secrets)

| Var | Required | Notes |
|---|---|---|
| `MINIMAX_API_KEY` | yes | Same key as justdumpit-ytscraper |
| `JUSTDUMPIT_URL` | yes | e.g. `https://justdumpit-ytscraper.fly.dev` |
| `JUSTDUMPIT_API_TOKEN` | optional | Shared secret for `/goals` + `/video/{id}/stage2` calls |
| `GH_TOKEN` | yes | GitHub PAT with `repo` scope (https://github.com/settings/tokens) |
| `GH_OWNER` | no | Defaults to `barnymurt` |
| `AGENT_DATA_DIR` | no | Defaults to `/data` on Fly (persistent volume) |
| `POLLER_INTERVAL` | no | Seconds between cron polls (default 900 = 15 min) |
| `POLLER_ENABLED` | no | Set `false` to disable the background poller |

### Justdumpit-side

Set `AGENT_WEBHOOK_URL` and `AGENT_WEBHOOK_TOKEN` on justdumpit-ytscraper so its watch-later loop fires webhooks here when `max_relevance >= 2`.

## Phase 1 limitations

- Tier 2 / tier 3 proposals are audit-only — operator approves via API, then agent drafts a proposal (currently a stub)
- No GitHub-issue-comment greenlight (`/approve` in PR comments) — Phase 2
- No cross-goal project synthesis — Phase 3
- No email-reply greenlight — Phase 4
- Action target_repo is inferred from action_description / atoms / dependencies — explicit field in Stage 2 output coming in Phase 2 of the extractor

## Development

```bash
pip install -r requirements.txt
cp .env.example .env  # fill in values
python -m tests.test_policy  # offline policy tests
uvicorn src.server:app --reload
```

## Architecture

```
justdumpit-ytscraper (Fly)
   │  POST /internal/event
   │     {video_id, stage2, ...}
   │
   ▼
justdumpit-agent webhook (FastAPI)
   │
   │  fetch /goals + /video/{id}/stage2
   │  re-validate via policy.py
   │  dispatch per final tier
   │
   ├─ tier_0   → executor (filesystem)   → audit executed
   ├─ tier_1   → executor (gh pr create) → audit pr_opened
   ├─ tier_2/3 → audit awaiting_greenlight (operator /approve → /execute)
   └─ tier_4   → audit halted
   │
   ▼
SQLite audit log at /data/agent_audit.db
```