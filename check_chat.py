import os, sys
sys.path.insert(0, "/app")
from src.channels.telegram import get_operator_chat_id
print("operator_chat_id =", get_operator_chat_id())
print("/data contents:", os.listdir("/data") if os.path.exists("/data") else "MISSING")
print("/data/operator_chat_id exists:", os.path.exists("/data/operator_chat_id"))
