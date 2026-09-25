"""
Export all Simon Fresh Water data from Render PostgreSQL
into a D1-compatible SQL file (INSERT statements).
Uses pg8000 — pure-Python driver, no native dependencies.
"""
import os
import sys
from datetime import datetime, date
from urllib.parse import urlparse

try:
    import pg8000.native as pg
except ImportError:
    print("ERROR: pg8000 not installed. Run: pip install pg8000")
    sys.exit(1)


# ─── CONFIG ───
PG_URL = os.environ.get("PG_URL", "").strip()
OUT_FILE = os.path.expanduser("~/simon_freshwater_export.sql")

# Tables to export, in dependency order
TABLES = [
    "admin_users",
    "consumers",
    "meter_readings",
    "payment_log",
    "notification_log",
    "password_reset_tokens",
]


def parse_pg_url(url):
    """postgresql://user:pass@host/dbname → dict for pg8000."""
    p = urlparse(url)
    return {
        "user": p.username,
        "password": p.password,
        "host": p.hostname,
        "port": p.port or 5432,
        "database": p.path.lstrip("/").split("?")[0],
    }


def sql_literal(v):
    """Convert a Python value to a SQLite-safe SQL literal."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (datetime, date)):
        return "'" + v.isoformat() + "'"
    # String — escape single quotes
    s = str(v).replace("'", "''")
    return "'" + s + "'"


def main():
    if not PG_URL:
        print("ERROR: PG_URL environment variable is required.")
        print("Usage:")
        print('  PG_URL="postgresql://..." python export_to_d1.py')
        sys.exit(1)

    cfg = parse_pg_url(PG_URL)
    print(f"Connecting to {cfg['host']}:{cfg['port']}/{cfg['database']} …")

    con = pg.Connection(
        user=cfg["user"],
        password=cfg["password"],
        host=cfg["host"],
        port=cfg["port"],
        database=cfg["database"],
        ssl_context=True,
    )
    print("Connected ✅")
    print()

    out = []
    out.append("-- Simon Fresh Water — data export for Cloudflare D1")
    out.append(f"-- Generated: {datetime.utcnow().isoformat()}Z")
    out.append("")
    out.append("PRAGMA foreign_keys = OFF;")
    out.append("")

    total_rows = 0
    for table in TABLES:
        try:
            rows = con.run(f"SELECT * FROM {table}")
            cols = [c["name"] for c in con.columns]
        except Exception as e:
            print(f"  ⚠️  {table}: skip ({e})")
            continue

        # con.columns is populated after run()
        col_names = [c["name"] if isinstance(c, dict) else c.name for c in con.columns]

        out.append(f"-- ─── {table} ({len(rows)} rows) ───")
        if not rows:
            out.append(f"-- (empty)")
            out.append("")
            continue

        for row in rows:
            vals = ", ".join(sql_literal(v) for v in row)
            cols_sql = ", ".join(col_names)
            out.append(f"INSERT INTO {table} ({cols_sql}) VALUES ({vals});")
        out.append("")
        total_rows += len(rows)
        print(f"  ✓ {table}: {len(rows)} rows")

    out.append("PRAGMA foreign_keys = ON;")
    out.append("")

    with open(OUT_FILE, "w") as f:
        f.write("\n".join(out))

    print()
    print(f"✅ Wrote {total_rows} rows to {OUT_FILE}")
    print(f"   File size: {os.path.getsize(OUT_FILE)} bytes")


if __name__ == "__main__":
    main()
