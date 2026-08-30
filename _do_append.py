"""Append retrospective endpoint to server.py."""
import sys
from pathlib import Path

p = Path(sys.argv[1])
content = p.read_bytes()
marker = b'    return {"ok": True, "chat_id": chat_id, "response": resp}\n'
if marker not in content:
    print(f"ERROR: marker not found in {p}")
    sys.exit(1)

with open(sys.argv[2]) as f:
    snippet = f.read()

new_content = content.replace(marker, marker + snippet.encode("utf-8"), 1)
p.write_bytes(new_content)
print(f"Appended to {p}")
