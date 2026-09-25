"""
Export the Cloudflare D1 database as a SQL dump via the REST API.

The D1 export API uses a two-step "polling" flow:
  1. POST with {"output_format": "polling"} → returns an at_bookmark
  2. POST repeatedly with {"current_bookmark": <bookmark>} until
     the response contains a signed download URL.
"""
import os
import sys
import json
import time
import requests

CF_ACCOUNT_ID     = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_D1_DATABASE_ID = os.environ.get("CF_D1_DATABASE_ID", "").strip()
CF_API_TOKEN      = os.environ.get("CF_API_TOKEN", "").strip()
OUTPUT_FILE       = os.environ.get("OUTPUT_FILE", "d1_export.sql")

if not all([CF_ACCOUNT_ID, CF_D1_DATABASE_ID, CF_API_TOKEN]):
    print("ERROR: Missing CF_ACCOUNT_ID, CF_D1_DATABASE_ID, or CF_API_TOKEN")
    sys.exit(1)

url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
       f"/d1/database/{CF_D1_DATABASE_ID}/export")
headers = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json",
}


def _post(payload):
    r = requests.post(url, headers=headers, json=payload, timeout=120)
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}: {r.text[:400]}")
        sys.exit(1)
    data = r.json()
    if not data.get("success"):
        print("API error:", json.dumps(data.get("errors"), indent=2))
        sys.exit(1)
    return data["result"]


# ─── 1. Start the export ───
print("Starting D1 export …")
result = _post({"output_format": "polling"})

bookmark = result.get("at_bookmark")
if not bookmark:
    print("No at_bookmark in response:")
    print(json.dumps(result, indent=2))
    sys.exit(1)

print(f"  bookmark: {bookmark[:20]}…")
print(f"  status  : {result.get('status', 'unknown')}")

# ─── 2. Poll until complete ───
signed_url = None
filename   = None

for attempt in range(1, 31):
    time.sleep(1)

    result = _post({
        "output_format": "polling",
        "current_bookmark": bookmark,
    })

    status = result.get("status", "")
    print(f"  poll {attempt}: status={status}")

    # The signed URL may be at the top level or nested inside `result`
    inner = result.get("result") or {}
    signed_url = result.get("signed_url") or inner.get("signed_url")
    filename   = result.get("filename")   or inner.get("filename")

    if signed_url:
        break

if not signed_url:
    print("Export did not produce a signed URL in time.")
    print("Last response:", json.dumps(result, indent=2))
    sys.exit(1)

# ─── 3. Download the SQL dump ───
print(f"Downloading SQL dump ({filename or 'unnamed'}) …")
r = requests.get(signed_url, stream=True, timeout=120)
r.raise_for_status()

with open(OUTPUT_FILE, "wb") as f:
    for chunk in r.iter_content(chunk_size=8192):
        f.write(chunk)

size = os.path.getsize(OUTPUT_FILE)
print(f"✅ Saved to {OUTPUT_FILE} ({size:,} bytes)")
