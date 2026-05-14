from dalila.ingestors.prediction_markets import discover_markets, get_market_signals
from dalila.db import connect

def debug():
    with connect() as conn:
        print("--- RAW DISCOVERY (PRIORITY BEATS) ---")
        mkts = discover_markets(conn)
        beats = ["sudan", "opec", "hormuz", "drone", "uae", "saudi", "afghanistan", "sahel", "yemen"]
        found = 0
        for m in mkts:
            q = m.get("question", "").lower()
            if any(b in q for b in beats):
                print(f"FOUND: {m['question']} (Vol: {m.get('volume')})")
                found += 1
        
        if found == 0:
            print("No priority beat markets found in the discovery pool.")

        print("\n--- RE-SCORING WITH HEAVY BOOST ---")
        # I'll check what's in the snapshot table
        cur = conn.cursor()
        cur.execute("SELECT question, probability FROM prediction_market_snapshots")
        for row in cur.fetchall():
             if any(b in row[0].lower() for b in beats):
                 print(f"IN DB: {row[0]}")

if __name__ == "__main__":
    debug()
