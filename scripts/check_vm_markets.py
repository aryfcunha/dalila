from dalila.ingestors.prediction_markets import get_market_signals, poll_markets
from dalila.db import connect
import sys

def check():
    with connect() as conn:
        print('--- REFRESHING MARKETS ---')
        poll_markets(conn)
        
        print('\n--- TOP 5 RANKED SIGNALS (New Scoring + Dedup) ---')
        signals = get_market_signals(conn)
        
        if not signals:
            print("No signals found above threshold.")
            return

        for s in signals:
            # s: question, probability, delta_24h, _score
            delta_str = f"{s.get('delta_24h', 0)*100:+.1f}%" if s.get('delta_24h') else "NEW"
            print(f"[{s['source']:8}] {s['probability']*100:4.1f}% ({delta_str}) | Score: {s['_score']:.2f}")
            print(f"      Q: {s['question']}")
            print("-" * 40)

if __name__ == "__main__":
    check()
