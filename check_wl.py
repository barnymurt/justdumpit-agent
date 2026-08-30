import sqlite3
c = sqlite3.connect("/data/kb.db")
print("watch_later_processed count:", c.execute("SELECT COUNT(*) FROM watch_later_processed").fetchone()[0])
print("sample rows:", list(c.execute("SELECT video_id, processed, emailed_at, processed_at, channel_name FROM watch_later_processed ORDER BY added_to_watch_later_at DESC LIMIT 5").fetchall()))
