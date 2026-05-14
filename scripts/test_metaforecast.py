import urllib.request
import json

def test_metaforecast():
    query = """
    {
      forecasts(query: "Sudan", platforms: ["metaculus"]) {
        title
        probability
        platform { id }
        url
      }
    }
    """
    body = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        "https://metaforecast.org/api/graphql",
        data=body,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            print(json.dumps(data, indent=2))
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_metaforecast()
