"""Check what's actually in the YouTube WL via API."""
import sys
sys.path.insert(0, ".")
import httpx
from src.config import get_telegram_bot_token
import os
# Get YouTube token from fly ssh
os.environ.setdefault("YOUTUBE_CLIENT_ID", "placeholder")
os.environ.setdefault("YOUTUBE_CLIENT_SECRET", "placeholder")

import re
# Read token directly
import json
with open("/data/youtube_token.json") as f:
    t = json.load(f)

token = t["token"]
print(f"Token scope: {t.get('scopes')}")
print(f"Token expires: {t.get('expiry')}")

# Test 1: What's our channel?
r = httpx.get(
    "https://www.googleapis.com/youtube/v3/channels",
    params={"part": "snippet,contentDetails", "mine": "true"},
    headers={"Authorization": f"Bearer {token}"},
)
data = r.json()
items = data.get("items", [])
print(f"\nMy channels ({len(items)}):")
for c in items:
    s = c.get("snippet", {})
    cd = c.get("contentDetails", {})
    print(f"  - {s.get('title')} ({c.get('id')})")
    print(f"    uploads playlist: {cd.get('relatedPlaylists', {}).get('uploads')}")
    print(f"    WL: cd has relatedPlaylists: {bool(cd.get('relatedPlaylists'))}")

# Test 2: Fetch WL directly
print("\nFetching WL playlist items...")
r2 = httpx.get(
    "https://www.googleapis.com/youtube/v3/playlistItems",
    params={"part": "snippet,contentDetails", "playlistId": "WL", "maxResults": 5},
    headers={"Authorization": f"Bearer {token}"},
)
data2 = r2.json()
print(f"Status: {r2.status_code}")
print(f"Items: {len(data2.get('items', []))}")
for it in data2.get("items", [])[:5]:
    s = it.get("snippet", {})
    print(f"  - {s.get('title', '?')[:60]} ({s.get('resourceId', {}).get('videoId', '?')})")
print(f"\nFull response: {r2.text[:500]}")
