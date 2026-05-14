from dalila.ingestors.prediction_markets import discover_markets
from dalila.db import connect

def check():
    with connect() as conn:
        mkts = discover_markets(conn)
        metaculus = [m for m in mkts if m.get("source") == "metaculus"]
        print(f"Total discovered: {len(mkts)}")
        print(f"Metaculus found:  {len(metaculus)}")
        for m in metaculus[:10]:
            print(f" - {m['question']} ({m['probability']:.1%})")

if __name__ == "__main__":
    check()
