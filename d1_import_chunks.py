"""
Import a SQL dump into Cloudflare D1 via the /query endpoint.

Single-batch design:
  • DROP phase:  PRAGMA defer_foreign_keys=on; + all DROPs in ONE batch.
  • Import phase: the entire dump in ONE batch (it starts with its own
                  PRAGMA defer_foreign_keys=TRUE; which covers all INSERTs).

D1 executes each batch as a single transaction, so the pragma stays in
effect for every statement in that batch — which is why this works while
splitting the statements across HTTP calls would not.

Env:
  INPUT_FILE      path to the .sql dump (default: d1_export.sql)
  DROP_EXISTING   "1" → DROP all user tables before importing
"""
import os
import sys
import json
import requests

CF_ACCOUNT_ID     = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_D1_DATABASE_ID = os.environ.get("CF_D1_DATABASE_ID", "").strip()
CF_API_TOKEN      = os.environ.get("CF_API_TOKEN", "").strip()
INPUT_FILE        = os.environ.get("INPUT_FILE", "d1_export.sql")
DROP_EXISTING     = os.environ.get("DROP_EXISTING", "0") == "1"

if not all([CF_ACCOUNT_ID, CF_D1_DATABASE_ID, CF_API_TOKEN]):
    print("ERROR: Missing CF_ACCOUNT_ID, CF_D1_DATABASE_ID, or CF_API_TOKEN")
    sys.exit(1)

API_URL = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
           f"/d1/database/{CF_D1_DATABASE_ID}/query")
HEADERS = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json",
}


def query(sql, label=""):
    """Send a batch to D1. Returns the result list on success, None on failure."""
    if label:
        print(f"  → {label} ({len(sql):,} bytes)")
    r = requests.post(API_URL, headers=HEADERS, json={"sql": sql}, timeout=180)
    if r.status_code != 200:
        print(f"  ❌ HTTP {r.status_code}: {r.text[:300]}")
        return None
    data = r.json()
    if not data.get("success"):
        print("  ❌ API error:", json.dumps(data.get("errors"), indent=2))
        return None
    return data.get("result", [])


# ─── Phase 1 (optional): Drop all user tables in one batch ───
if DROP_EXISTING:
    print("Dropping existing user tables …")
    res = query(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE '_cf_%';"
    )
    if res is None:
        print("❌ Could not list tables.")
        sys.exit(1)

    rows = res[0].get("results", []) if res else []
    tables = [row.get("name") for row in rows if row.get("name")]
    print(f"  found {len(tables)} table(s): {', '.join(tables)}")

    if tables:
        drops = "PRAGMA defer_foreign_keys = on;\n"
        for t in tables:
            drops += f'DROP TABLE IF EXISTS "{t}";\n'
        if query(drops, "drop batch") is None:
            print("❌ Drop batch failed.")
            sys.exit(1)
        print("  ✅ tables dropped.")
    print()


# ─── Phase 2: Import the entire dump in one batch ───
if not os.path.exists(INPUT_FILE):
    print(f"❌ Input file not found: {INPUT_FILE}")
    sys.exit(1)

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    sql_content = f.read()

print(f"Importing dump ({len(sql_content):,} bytes) as a single batch …")
if query(sql_content, "import batch") is None:
    print("❌ Import failed.")
    sys.exit(1)

print("✅ Import complete.")
