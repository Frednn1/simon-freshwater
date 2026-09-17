import os
import json
import gspread
from google.oauth2.service_account import Credentials


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SPREADSHEET_NAME = "Simon FreshWater - Consumer Records"


def _get_client():
    """
    Authenticate using service-account JSON.
    Priority: GOOGLE_CREDENTIALS_JSON env var (Render), else credentials.json file (local).
    """
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            "credentials.json", scopes=SCOPES
        )
    return gspread.authorize(creds)


def sync_consumer_to_sheet(consumer, readings):
    """
    Create/update a worksheet tab named after the consumer's Cust. Name.
    Columns: Full Name | Acc. Name | Contact | Address | Reading (M3) | Date | Amount | Paid | Balance.
    Safe to call repeatedly — clears and rewrites the tab.
    """
    client = _get_client()

    try:
        spreadsheet = client.open(SPREADSHEET_NAME)
    except gspread.SpreadsheetNotFound:
        spreadsheet = client.create(SPREADSHEET_NAME)
        try:
            spreadsheet.share(None, perm_type="anyone", role="reader")
        except Exception:
            pass  # sharing is best-effort

    tab_name = (consumer.cust_name or "Consumer")[:100]

    try:
        ws = spreadsheet.worksheet(tab_name)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=tab_name, rows=30, cols=10)

    header = [
        "Full Name", "Acc. Name", "Contact", "Address", "",
        "Reading (M3)", "Date", "Amount (KES)", "Paid (KES)", "Balance (KES)",
    ]
    ws.update("A1", [header], value_input_option="USER_ENTERED")

    # Consumer info row
    ws.update("A2", [[
        consumer.cust_name,
        consumer.acc_name,
        consumer.contact,
        consumer.address or "",
    ]], value_input_option="USER_ENTERED")

    # Last 5 readings
    rows = []
    for r in readings[:5]:
        rows.append([
            "", "", "", "", "",
            r.reading_m3,
            r.reading_date.isoformat(),
            r.amount_kes,
            r.amount_paid,
            r.balance,
        ])
    if rows:
        ws.update(f"A3:J{2 + len(rows)}", rows, value_input_option="USER_ENTERED")

    return True
