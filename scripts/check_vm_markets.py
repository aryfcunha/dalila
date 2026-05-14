from dalila.ingestors.prediction_markets import poll_markets, discover_markets
from dalila.db import connect
import sys

def check():
    with connect() as conn:
        print('--- DISCOVERING MARKETS ---')
        # This will search and record snapshots
        alerts = poll_markets(conn)
        
        cur = conn.cursor()
        cur.execute('SELECT question, probability, source, recorded_at FROM prediction_market_snapshots ORDER BY recorded_at DESC LIMIT 15')
        rows = cur.fetchall()
        
        if not rows:
            print("\nNo market snapshots found. Check settings or API connectivity.")
            return

        print("\n--- CURRENT PREDICTION MARKET SIGNALS ---")
        for row in rows:
            # row: question, probability, source, recorded_at
            print(f"[{row[2]:8}] {row[1]*100:4.1f}% | {row[0]}")
        
        if alerts:
            print("\n--- ACTIVE ALERTS (Large Moves) ---")
            for a in alerts:
                print(f"ALERT: {a['question']} moved {a.get('delta_24h', 0)*100:+.1f}% in 24h")

if __name__ == "__main__":
    check()
