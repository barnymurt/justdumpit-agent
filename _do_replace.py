"""Replace auditor, dispatcher, poller → add goals_client."""
import sys
from pathlib import Path

p = Path(sys.argv[1])
content = p.read_text(encoding="utf-8")
old = "from src import auditor, dispatcher, poller"
new = "from src import auditor, dispatcher, goals_client, poller"
if old not in content:
    print("OLD NOT FOUND")
    sys.exit(1)
content = content.replace(old, new, 1)
p.write_text(content, encoding="utf-8")
print("DONE")
