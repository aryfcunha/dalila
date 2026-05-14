import sqlite3
import time
import logging
from dalila.ingestors.prediction_markets import poll_markets, get_market_signals

logging.basicConfig(level=logging.INFO)

conn = sqlite3.connect('dalila.db')

print("Polling markets first time...")
poll_markets(conn)
conn.commit()

print("Waiting 10 seconds (for testing, usually 30 min)...")
# Note: the code uses -25 minutes cutoff, so for testing I'll manually adjust the recorded_at in DB
# or just check if it records.
# Actually, I'll manually update a recorded_at in prediction_market_history to be 30 mins ago.

cursor = conn.cursor()
cursor.execute("UPDATE prediction_market_history SET recorded_at = datetime('now', '-30 minutes')")
conn.commit()

print("Polling markets second time...")
poll_markets(conn)
conn.commit()

print("Fetching signals...")
signals = get_market_signals(conn)
for s in signals:
    print(f"Q: {s['question'][:50]}... | Prob: {s['probability']:.1%} | 30m: {s.get('delta_30m')}")

conn.close()
