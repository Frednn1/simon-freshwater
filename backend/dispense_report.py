"""Dispensing Activity Report PDF - direct PyMuPDF drawing.

Standalone generator (not template-based like report.py). Suitable for
variable row counts (7/30/90-day reports) with automatic pagination.
"""
from io import BytesIO
from datetime import datetime


def _ts_eat():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Africa/Nairobi"))
    except Exception:
        return datetime.utcnow()


def _fmt_l(v):
    try:
        return f"{int(round(float(v or 0))):,} L"
    except Exception:
        return "0 L"


def _fmt_kes(v):
    try:
        return f"{float(v or 0):,.2f}"
    except Exception:
        return "0.00"


def _fmt_m3(v):
    if v is None:
        return "-"
    try:
        return f"{float(v):.2f}"
    except Exception:
        return "-"


def _status_color(status):
    s = (status or "").lower()
    if s == "due": return (0.90, 0.32, 0.0)
    if s == "cleared": return (0.18, 0.49, 0.20)
    if s == "review": return (0.78, 0.16, 0.16)
    return (0.38, 0.49, 0.55)


def _draw_header(page, tenant, d_from, d_to, days):
    import fitz
    W = page.rect.width
    x = 40
    y = 48

    name = (tenant.org_name or tenant.slug or "Tenant") if tenant else "Tenant"
    page.insert_text((x, y), name, fontsize=17, fontname="hebo", color=(0.04, 0.15, 0.25))
    y += 18

    sub = []
    if tenant and tenant.address: sub.append(tenant.address)
    if tenant and tenant.phone: sub.append(tenant.phone)
    if tenant and tenant.email: sub.append(tenant.email)
    if sub:
        page.insert_text((x, y), " \u00b7 ".join(sub), fontsize=9, fontname="helv", color=(0.42, 0.42, 0.42))
        y += 16

    y += 10
    page.draw_rect(fitz.Rect(x, y, W - 40, y + 0.6), color=None, fill=(0.82, 0.82, 0.82), width=0)
    y += 18

    page.insert_text((x, y), "Dispensing Activity Report", fontsize=14, fontname="hebo", color=(0.04, 0.15, 0.25))
    y += 16
    period = f"{d_from.strftime('%d %b %Y')} - {d_to.strftime('%d %b %Y')}  ({days} day{'s' if days != 1 else ''})"
    page.insert_text((x, y), f"Period: {period}", fontsize=10, fontname="helv", color=(0.42, 0.42, 0.42))
    y += 14
    ts = _ts_eat().strftime("%d %b %Y %H:%M EAT")
    page.insert_text((x, y), f"Generated: {ts}", fontsize=9, fontname="helv", color=(0.55, 0.55, 0.55))
    return y + 22


def _draw_kpi_cards(page, y_start, totals):
    import fitz
    W = page.rect.width
    x = 40
    avail_w = W - 80
    cols, rows, gap, card_h = 3, 2, 8, 58
    card_w = (avail_w - gap * (cols - 1)) / cols

    def _variance_lbl():
        v = float(totals.get("variance_l") or 0)
        s = f"{int(round(abs(v))):,} L"
        return f"+{s}" if v > 0 else (f"-{s}" if v < 0 else s)

    tiles = [
        ("CONSUMPTION", _fmt_l(totals.get("consumption_l"))),
        ("SOLD",        _fmt_l(totals.get("sold_l"))),
        ("VARIANCE",    _variance_lbl()),
        ("SALES (KES)", _fmt_kes(totals.get("sales_kes"))),
        ("PAID (KES)",  _fmt_kes(totals.get("paid_kes"))),
        ("OUTSTANDING (KES)", _fmt_kes(totals.get("outstanding_kes"))),
    ]

    y = y_start
    for r in range(rows):
        row_y = y + r * (card_h + gap)
        for c in range(cols):
            idx = r * cols + c
            if idx >= len(tiles):
                break
            label, value = tiles[idx]
            cx = x + c * (card_w + gap)
            rect = fitz.Rect(cx, row_y, cx + card_w, row_y + card_h)
            page.draw_rect(rect, color=(0.85, 0.88, 0.92), fill=(0.97, 0.98, 0.99), width=0.7)
            page.draw_rect(fitz.Rect(cx, row_y, cx + 3, row_y + card_h),
                           color=None, fill=(0.05, 0.28, 0.63), width=0)
            page.insert_text((cx + 10, row_y + 16), label,
                             fontsize=7.5, fontname="hebo", color=(0.42, 0.42, 0.42))
            page.insert_text((cx + 10, row_y + 40), value,
                             fontsize=12, fontname="hebo", color=(0.04, 0.15, 0.25))

    return y_start + rows * (card_h + gap) + 8


def _col_layout(page):
    import fitz
    W = page.rect.width
    x_start, x_end = 40, W - 40
    weights = [12, 10, 12, 11, 11, 12, 12, 12, 13, 11]  # sum=116
    total_w = sum(weights)
    headers = ["Date", "Opening", "Cons L", "Sold L", "Var L",
               "Sales", "Paid", "Recd", "Outst", "Status"]
    xs, x = [], x_start
    for h, w in zip(headers, weights):
        xs.append((h, x))
        x += (w / total_w) * (x_end - x_start)
    return xs


def _draw_table_header(page, y, cols):
    import fitz
    page.draw_rect(fitz.Rect(40, y, page.rect.width - 40, y + 20),
                   color=None, fill=(0.90, 0.94, 0.97), width=0)
    for h, x in cols:
        page.insert_text((x + 3, y + 13), h, fontsize=8, fontname="hebo",
                         color=(0.05, 0.28, 0.63))
    return y + 20


def _draw_row(page, y, cols, values, row_h=15):
    for (h, x), val in zip(cols, values):
        if isinstance(val, dict):
            text = val.get("text", "")
            color = val.get("color", (0, 0, 0))
        else:
            text = str(val)
            color = (0.15, 0.15, 0.15)
        page.insert_text((x + 3, y + 11), text, fontsize=8, fontname="helv", color=color)
    return y + row_h


def generate_dispense_report_pdf(tenant, daily, totals, d_from, d_to):
    try:
        import fitz
    except ImportError as e:
        raise RuntimeError("PyMuPDF (fitz) required for report generation") from e

    doc = fitz.open()
    W, H = fitz.paper_size("a4")

    def _new_page():
        return doc.new_page(width=W, height=H)

    page = _new_page()
    y = _draw_header(page, tenant, d_from, d_to, len(daily))
    y = _draw_kpi_cards(page, y, totals)
    y += 8

    cols = _col_layout(page)
    y = _draw_table_header(page, y, cols)

    row_h = 15
    footer_reserve = 32
    for row in daily:
        if y + row_h > H - footer_reserve:
            page = _new_page()
            y = 40
            y = _draw_table_header(page, y, cols)

        status = row.get("status") or ""
        values = [
            row.get("date", ""),
            _fmt_m3(row.get("opening_reading_m3")),
            _fmt_l(row.get("consumption_l")),
            _fmt_l(row.get("sold_l")),
            (f"+{_fmt_l(row.get('variance_l'))}" if (row.get("variance_l") or 0) > 0
             else (f"-{_fmt_l(abs(row.get('variance_l') or 0))}"
                   if (row.get("variance_l") or 0) < 0
                   else _fmt_l(0))),
            _fmt_kes(row.get("sales_kes")),
            _fmt_kes(row.get("paid_kes")),
            _fmt_kes(row.get("cash_received_kes")),
            _fmt_kes(row.get("outstanding_kes")),
            {"text": status, "color": _status_color(status)},
        ]
        y = _draw_row(page, y, cols, values, row_h)

    tenant_lbl = (tenant.org_name if tenant else "Tenant") or "Tenant"
    for idx, p in enumerate(doc):
        foot_y = H - 16
        p.insert_text((40, foot_y),
                      f"{tenant_lbl} \u00b7 Dispensing Activity Report",
                      fontsize=8, fontname="helv", color=(0.55, 0.55, 0.55))
        p.insert_text((W - 90, foot_y),
                      f"Page {idx + 1} of {len(doc)}",
                      fontsize=8, fontname="helv", color=(0.55, 0.55, 0.55))

    out = BytesIO()
    doc.save(out, garbage=4, deflate=True, clean=True)
    doc.close()
    out.seek(0)
    return out
