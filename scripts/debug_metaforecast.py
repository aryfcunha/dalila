import urllib.request
import json

METAFORECAST_API = "https://metaforecast.org/api/graphql"

def debug_metaforecast():
    seeds = ["Sudan", "OPEC"]
    for seed in seeds:
        print(f"--- Searching Metaforecast for: {seed} ---")
        gql = """
        query Search($input: SearchInput!) {
          searchQuestions(input: $input) {
            id
            title
            platform { id }
          }
        }
        """
        variables = {"input": {"query": seed, "forecastingPlatforms": ["metaculus"], "limit": 5}}
        body = json.dumps({"query": gql, "variables": variables}).encode("utf-8")
        req = urllib.request.Request(METAFORECAST_API, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                print(f"Raw response: {raw[:200]}...")
                data = json.loads(raw)
                if "errors" in data:
                    print(f"GQL Errors: {data['errors']}")
                else:
                    qs = data.get("data", {}).get("searchQuestions", [])
                    print(f"Found {len(qs)} results.")
        except Exception as e:
            print(f"HTTP Error for {seed}: {e}")

if __name__ == "__main__":
    debug_metaforecast()
