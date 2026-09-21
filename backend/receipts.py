"""
Receipt PDF generator.

Loads templates/receipt_template.html, substitutes the {{placeholder}}
tokens with real payment data, strips the print-hint helper block, and
returns a PDF byte stream via xhtml2pdf.
"""
import os
import re
from io import BytesIO


TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "templates", "receipt_template.html",
)


def _alloc_row(a: dict) -> str:
    """One <tr> for the Bill Allocation table."""
    note = a.get("note") or ""
    note_html = f'<span class="note">{note}</span>' if note else ""
    return (
        "<tr>"
        f'<td>{a.get("reading_date","")}</td>'
        f'<td>{a.get("reading_m3","")}</td>'
        f'<td class="right">KES {a.get("amount_kes","")}</td>'
        f'<td class="right">KES {a.get("applied","")}</td>'
        f'<td class="right">KES {a.get("new_balance","")} {note_html}</td>'
        "</tr>"
    )


def build_receipt_html(snapshot: dict) -> str:
    """Return the template with all placeholders filled."""
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        tpl = f.read()

    allocations = snapshot.get("allocations") or []
    rows_html = "\n".join(_alloc_row(a) for a in allocations) or (
        '<tr><td colspan="5" style="text-align:center; padding:12px;">'
        "No allocations on record for this payment.</td></tr>"
    )
    tpl = re.sub(
        r"(<tbody>).*?(</tbody>)",
        lambda m: m.group(1) + "\n" + rows_html + "\n" + m.group(2),
        tpl,
        count=1,
        flags=re.DOTALL,
    )

    tpl = re.sub(r'<div class="print-hint">.*?</div>\s*', "",
                 tpl, count=1, flags=re.DOTALL)
    tpl = re.sub(r'<div class="alloc-label">.*?</div>\s*', "",
                 tpl, count=1, flags=re.DOTALL)

    for key, value in snapshot.items():
        if key == "allocations":
            continue
        token = "{{" + key + "}}"
        tpl = tpl.replace(token, str(value if value not in (None, "") else "—"))

    tpl = tpl.replace('<span class="placeholder">', "<span>")
    tpl = re.sub(r"\{\{[^}]+\}\}", "", tpl)

    return tpl


def generate_receipt_pdf(snapshot: dict) -> BytesIO:
    """Generate a PDF from the snapshot. Returns a BytesIO ready to send."""
    from xhtml2pdf import pisa

    html = build_receipt_html(snapshot)
    buf = BytesIO()
    result = pisa.CreatePDF(
        src=html,
        dest=buf,
        encoding="utf-8",
        link_callback=lambda uri, rel: uri,
    )
    if result.err:
        raise RuntimeError(f"xhtml2pdf reported errors (err={result.err})")
    buf.seek(0)
    return buf
