import json
t = json.load(open("/data/youtube_token.json"))
for k, v in t.items():
    if k == "refresh_token":
        print(f"refresh_token: {v[:30]}...")
    else:
        print(f"{k}: {v}")
