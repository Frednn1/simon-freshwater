"""
Import SQL files into Cloudflare D1 via the REST API.
Replaces Wrangler (which has no Android/ARM64 build).

Usage:
  CF_ACCOUNT_ID=xxx CF_D1_DATABASE_ID=yyy CF_API_TOKEN=zzz python d1_import.py
"""
import os
import sys
import json
import time
import requests

CF_ACCOUNT_ID     = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_D1_DATABASE_ID = os.environ.get("CF_D1_DATABASE_ID", "").strip()
CF_API_TOKEN      = os.environ.get("CF_API_TOKEN", "").strip()

if not all([CF_ACCOUNT_ID, CF_D1_DATABASE_ID, CF_API_TOKEN]):
    print("ERROR: Set CF_ACCOUNT_ID, CF_D1_DATABASE_ID, CF_API_TOKEN env vars.")
    sys.exit(1)

API_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
    f"/d1/database/{CF_D1_DATABASE_ID}/query"
)

HEADERS = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json",
}


def run_sql(sql: str, label: str) -> bool:
    """POST a SQL batch to D1 and report the result."""
    print(f"→ Sending {label} ({len(sql)} bytes) …")
    r = requests.post(API_URL, headers=HEADERS, json={"sql": sql}, timeout=120)

    if r.status_code != 200:
        print(f"  ❌ HTTP {r.status_code}: {r.text[:400]}")
        return False

    data = r.json()
    if not data.get("success"):
        print(f"  ❌ API error: {json.dumps(data.get('errors'), indent=2)}")
        return False

    result = data.get("result", [])
    total_changes = 0
    for entry in result:
        meta = entry.get("meta", {}) or {}
        total_changes += meta.get("changes", 0)
    total_statements = len(result)

    print(f"  ✅ {total_statements} statement(s), {total_changes} row(s) changed")
    return True


def chunk_sql(sql: str, max_bytes: int = 90_000) -> list:
    """D1 caps request body size; split INSERT statements into chunks."""
    lines = [ln for ln in sql.splitlines() if ln.strip()]
    chunks, current, size = [], [], 0
    for line in lines:
        line_size = len(line) + 1
        if size + line_size > max_bytes and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += line_size
    if current:
        chunks.append("\n".join(current))
    return chunks


def main():
    schema_path = os.path.expanduser("~/simon_freshwater_schema.sql")
    data_path   = os.path.expanduser("~/simon_freshwater_export.sql")

    # ── 1. Schema ──
    if os.path.exists(schema_path):
        with open(schema_path) as f:
            schema_sql = f.read()
        if not run_sql(schema_sql, "schema"):
            print("Schema import FAILED — stopping.")
            sys.exit(1)
    else:
        print(f"⚠️  Schema file not found: {schema_path}")
        print("    Skipping schema import (tables may already exist).")

    # ── 2. Data ──
    if not os.path.exists(data_path):
        print(f"❌ Data file not found: {data_path}")
        sys.exit(1)

    with open(data_path) as f:
        data_sql = f.read()

    chunks = chunk_sql(data_sql)
    print(f"Data SQL split into {len(chunks)} chunk(s).")

    ok = 0
    for i, chunk in enumerate(chunks, 1):
        if run_sql(chunk, f"data chunk {i}/{len(chunks)}"):
            ok += 1
        else:
            print(f"  ⚠️  Chunk {i} failed — continuing with remaining chunks.")
        time.sleep(0.5)  # gentle rate limit

    print()
    print(f"✅ {ok}/{len(chunks)} chunk(s) imported successfully.")

    # ── 3. Verify ──
    print()
    print("→ Verifying: SELECT COUNT(*) FROM consumers …")
    r = requests.post(API_URL, headers=HEADERS,
                      json={"sql": "SELECT COUNT(*) AS c FROM consumers;"},
                      timeout=30)
    if r.status_code == 200 and r.json().get("success"):
        result = r.json()["result"]
        rows = result[0].get("results", []) if result else []
        count = rows[0].get("c") if rows else "?"
        print(f"  ✅ consumers count: {count}")
    else:
        print(f"  ⚠️  Verify failed: {r.text[:300]}")


if __name__ == "__main__":
    main()
