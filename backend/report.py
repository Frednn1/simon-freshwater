"""
Monthly billing report PDF generator — programmatic, mirrors the HTML
design used at /template/report.

Design elements preserved from templates/SimonWater_ReportTemplate.html:
  • Gradient header (navy -> blue) with brand name and report number
  • Meta band with side borders and 4 cells (Period / Generated / Total / Report)
  • Body box with light-blue border
  • Section titles with a small blue bullet
  • Summary cards with individual borders on a light background
  • Table with dark-blue header and alternating row backgrounds
  • Totals band in dark blue
  • Gradient footer, centered text

Dynamic rows: the table flows to continuation pages when the current
one is full. No fixed row limit.

Snapshot keys used:
  _all_rows (list of dicts), report_no, report_month, report_year,
  generated_date, total_consumers, active_consumers,
  total_consumption, total_billed, total_paid, total_outstanding
"""
import os
from io import BytesIO


# ─────────────────────────────────────────────────────────────
#  Colours (RGB 0..1)
# ─────────────────────────────────────────────────────────────
def _c(h):
    return (((h >> 16) & 0xFF) / 255.0,
            ((h >> 8)  & 0xFF) / 255.0,
            ( h        & 0xFF) / 255.0)


_NAVY      = _c(0x0a2540)
_BLUE_DEEP = _c(0x0d47a1)
_BLUE_MID  = _c(0x1976d2)
_LIGHT     = _c(0xe3f2fd)
_CELL_BG   = _c(0xf5faff)
_CELL_BD   = _c(0xbbdefb)
_BODY_BD   = _c(0x90caf9)
_MID_GREY  = _c(0xcfd8dc)
_GREY      = _c(0x455a64)
_DARK      = _c(0x1c2b36)
_ROW_ALT   = _c(0xf8fbff)
_WHITE     = (1.0, 1.0, 1.0)


# ─────────────────────────────────────────────────────────────
#  Fonts (Times-Roman — built into PyMuPDF)
# ─────────────────────────────────────────────────────────────
_FONT   = "times-roman"
_FONT_B = "times-bold"
_FONT_I = "times-italic"

_SZ_H1     = 17
_SZ_H2     = 12
_SZ_META   = 10.5
_SZ_LABEL  = 8.5
_SZ_BODY   = 11
_SZ_ROW    = 10.5
_SZ_BRAND  = 9.5
_SZ_TINY   = 8


# ─────────────────────────────────────────────────────────────
#  Layout
# ─────────────────────────────────────────────────────────────
_ML = 34            # left margin (12mm)
_MR = 34
_MT = 28            # top margin  (10mm)
_MB = 28

_HDR_H       = 50   # gradient header height (page 1)
_META_H      = 34
_BODY_PAD_TOP = 12
_BODY_PAD_X   = 16

_TITLE_H     = 20   # section title block height
_SUM_GAP     = 8
_SUM_CELL_H  = 46
_THEAD_H     = 24
_ROW_H       = 20
_TOTALS_H    = 26
_FTR_H       = 40
_FTR_GAP     = 6

_CONT_HDR_H  = 32

# ─── Column x-positions (from left margin) ───
_COL_X = {
    "num":   _ML + 8,
    "name":  _ML + 34,
    "meter": _ML + 232,
    "cm3":   _ML + 340,
    "amt":   _ML + 428,
    "paid":  _ML + 518,
    "bal":   _ML + 608,
}
_COL_LABEL = {
    "num":   "#",
    "name":  "CUSTOMER",
    "meter": "METER NO.",
    "cm3":   "CONSUMPTION (M\u00b3)",
    "amt":   "BILLED (KES)",
    "paid":  "PAID (KES)",
    "bal":   "BALANCE / STATUS",
}
_TRUNC = {
    "name":  30,
    "meter": 14,
    "cm3":   12,
    "amt":   12,
    "paid":  12,
    "bal":   26,
}


def _t(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "."


# ═════════════════════════════════════════════════════════════
#  Drawing primitives
# ═════════════════════════════════════════════════════════════

def _gradient_bar(pg, rect, c1, c2, steps=50):
    """Horizontal 2-stop gradient inside rect."""
    import fitz
    x0, y0, x1, y1 = rect
    w = x1 - x0
    sw = w / steps
    for i in range(steps):
        t = i / (steps - 1)
        r = c1[0] + (c2[0] - c1[0]) * t
        g = c1[1] + (c2[1] - c1[1]) * t
        b = c1[2] + (c2[2] - c1[2]) * t
        pg.draw_rect(
            fitz.Rect(x0 + i * sw, y0, x0 + (i + 1) * sw + 0.6, y1),
            color=None, fill=(r, g, b), width=0,
        )


def _draw_header_page1(pg, page_w, report_month, report_year, report_no):
    import fitz
    rect = fitz.Rect(0, _MT, page_w, _MT + _HDR_H)
    _gradient_bar(pg, rect, _NAVY, _BLUE_MID)

    # Left: brand name + tagline
    tx = _ML + 8
    pg.insert_text((tx, _MT + 22), "SIMON FRESH WATER",
                   fontsize=_SZ_H1, fontname=_FONT_B, color=_WHITE)
    pg.insert_text((tx, _MT + 38),
                   "Kikuyu, Kenya  \u00b7  Monthly Billing Report",
                   fontsize=_SZ_META, fontname=_FONT_I, color=_WHITE)

    # Right: Report No. label + value (right-aligned)
    label = "REPORT NO."
    value = report_no
    lw = len(label) * _SZ_LABEL * 0.62
    vw = len(value) * _SZ_H2 * 0.55
    pg.insert_text((page_w - _MR - 8 - lw, _MT + 20), label,
                   fontsize=_SZ_LABEL, fontname=_FONT_B, color=_WHITE)
    pg.insert_text((page_w - _MR - 8 - vw, _MT + 40), value,
                   fontsize=_SZ_H2, fontname=_FONT_B, color=_WHITE)


def _draw_cont_header(pg, page_w, report_month, report_year, page_idx, total_pages):
    import fitz
    rect = fitz.Rect(0, _MT, page_w, _MT + _CONT_HDR_H)
    _gradient_bar(pg, rect, _NAVY, _BLUE_MID)

    tx = _ML + 8
    pg.insert_text((tx, _MT + 20), "SIMON FRESH WATER",
                   fontsize=_SZ_H2, fontname=_FONT_B, color=_WHITE)
    pg.insert_text((tx + 190, _MT + 20),
                   "\u00b7 Monthly Billing Report \u2014 continued",
                   fontsize=_SZ_META, fontname=_FONT_I, color=_WHITE)

    right_txt = f"{report_month} {report_year}   \u00b7   Page {page_idx} of {total_pages}"
    tw = len(right_txt) * _SZ_META * 0.5
    pg.insert_text((page_w - _MR - 8 - tw, _MT + 20), right_txt,
                   fontsize=_SZ_META, fontname=_FONT_B, color=_WHITE)


def _draw_meta_band(pg, page_w, y, report_month, report_year, generated,
                    total_consumers, report_no):
    import fitz
    # Background band
    pg.draw_rect(fitz.Rect(_ML, y, page_w - _MR, y + _META_H),
                 color=None, fill=_LIGHT)
    # Left and right borders (4px in HTML)
    pg.draw_line(fitz.Point(_ML, y), fitz.Point(_ML, y + _META_H),
                 color=_BLUE_MID, width=3)
    pg.draw_line(fitz.Point(page_w - _MR, y), fitz.Point(page_w - _MR, y + _META_H),
                 color=_BLUE_MID, width=3)

    cells = [
        ("PERIOD",          f"{report_month} {report_year}"),
        ("GENERATED",       generated),
        ("TOTAL CONSUMERS", str(total_consumers)),
        ("REPORT NO.",      report_no),
    ]
    n = len(cells)
    gap = 6
    avail = (page_w - _MR) - _ML - 2 * _BODY_PAD_X
    cw = (avail - (n - 1) * gap) / n

    for i, (label, value) in enumerate(cells):
        cx = _ML + _BODY_PAD_X + i * (cw + gap)
        pg.insert_text((cx, y + 13), label,
                       fontsize=_SZ_LABEL, fontname=_FONT_B, color=_GREY)
        pg.insert_text((cx, y + 28), _t(value, 24),
                       fontsize=_SZ_META, fontname=_FONT_B, color=_BLUE_DEEP)


def _draw_body_border_top(pg, page_w, y, height):
    """Left/right/bottom border of the body box (top is open — attaches to meta)."""
    import fitz
    pg.draw_line(fitz.Point(_ML, y), fitz.Point(_ML, y + height),
                 color=_BODY_BD, width=1.5)
    pg.draw_line(fitz.Point(page_w - _MR, y), fitz.Point(page_w - _MR, y + height),
                 color=_BODY_BD, width=1.5)
    pg.draw_line(fitz.Point(_ML, y + height), fitz.Point(page_w - _MR, y + height),
                 color=_BODY_BD, width=1.5)


def _draw_section_title(pg, x, y, page_w, title):
    import fitz
    # Small blue bullet
    pg.draw_rect(fitz.Rect(x, y - 1, x + 6, y + 5),
                 color=None, fill=_BLUE_MID)
    # Title
    pg.insert_text((x + 12, y + 4), title.upper(),
                   fontsize=_SZ_BODY, fontname=_FONT_B, color=_BLUE_DEEP)
    # Bottom divider
    pg.draw_line(fitz.Point(x, y + _TITLE_H - 4),
                 fitz.Point(page_w - _MR - _BODY_PAD_X, y + _TITLE_H - 4),
                 color=_LIGHT, width=1.2)


def _draw_summary_cells(pg, x, y, page_w, values):
    """values = list of (label, value) length 5."""
    import fitz
    n = len(values)
    gap = 8
    avail = (page_w - _MR - _BODY_PAD_X) - x
    cw = (avail - (n - 1) * gap) / n

    for i, (label, value) in enumerate(values):
        cx = x + i * (cw + gap)
        pg.draw_rect(fitz.Rect(cx, y, cx + cw, y + _SUM_CELL_H),
                     color=_CELL_BD, fill=_CELL_BG, width=0.8)
        pg.insert_text((cx + 8, y + 16), label,
                       fontsize=_SZ_LABEL, fontname=_FONT_B, color=_GREY)
        pg.insert_text((cx + 8, y + 36), _t(value, 18),
                       fontsize=_SZ_H2, fontname=_FONT_B, color=_BLUE_DEEP)


def _draw_table_header(pg, y, page_w):
    import fitz
    x0 = _ML + _BODY_PAD_X
    x1 = page_w - _MR - _BODY_PAD_X
    pg.draw_rect(fitz.Rect(x0, y, x1, y + _THEAD_H),
                 color=None, fill=_BLUE_DEEP)
    base = y + _THEAD_H * 0.68
    for col, label in _COL_LABEL.items():
        pg.insert_text((_COL_X[col], base), label,
                       fontsize=_SZ_LABEL, fontname=_FONT_B, color=_WHITE)


def _draw_row(pg, y, row, row_number, alt=False):
    import fitz
    x0 = _ML + _BODY_PAD_X
    x1 = _ML + _BODY_PAD_X + 780   # will be clipped by page width
    # Actually use the column span: from first to last column
    x0 = _COL_X["num"] - 8
    x1 = _COL_X["bal"] + 180
    if x1 > _COL_X["bal"] + 200:
        x1 = _COL_X["bal"] + 200
    # Simpler: base the strip on fixed left/right margins of the table
    x0 = _ML + _BODY_PAD_X
    # -- not used further; rely on col x positions --

    if alt:
        # Alternating row background — extends slightly beyond col range
        left  = _ML + _BODY_PAD_X
        right = _COL_X["bal"] + 150
        pg.draw_rect(fitz.Rect(left, y, right, y + _ROW_H),
                     color=None, fill=_ROW_ALT)

    base = y + _ROW_H * 0.72
    pg.insert_text((_COL_X["num"], base), str(row_number),
                   fontsize=_SZ_ROW - 1, fontname=_FONT, color=_GREY)
    for col in ("name", "meter", "cm3", "amt", "paid", "bal"):
        pg.insert_text((_COL_X[col], base),
                       _t(row.get(col, ""), _TRUNC[col]),
                       fontsize=_SZ_ROW, fontname=_FONT, color=_DARK)

    # Row underline
    left  = _ML + _BODY_PAD_X
    right = _COL_X["bal"] + 150
    pg.draw_line(fitz.Point(left, y + _ROW_H),
                 fitz.Point(right, y + _ROW_H),
                 color=_LIGHT, width=0.6)


def _draw_totals(pg, y, page_w, totals):
    import fitz
    x0 = _ML + _BODY_PAD_X
    x1 = _COL_X["bal"] + 150
    pg.draw_rect(fitz.Rect(x0, y, x1, y + _TOTALS_H),
                 color=None, fill=_BLUE_DEEP)
    base = y + _TOTALS_H * 0.68
    pg.insert_text((x0 + 12, base), "TOTALS:",
                   fontsize=_SZ_BODY, fontname=_FONT_B, color=_WHITE)
    pg.insert_text((_COL_X["amt"], base),
                   f"Billed KES {totals['billed']}",
                   fontsize=_SZ_BODY, fontname=_FONT_B, color=_WHITE)
    pg.insert_text((_COL_X["paid"], base),
                   f"Paid KES {totals['paid']}",
                   fontsize=_SZ_BODY, fontname=_FONT_B, color=_WHITE)
    pg.insert_text((_COL_X["bal"], base),
                   f"Balance KES {totals['balance']}",
                   fontsize=_SZ_BODY, fontname=_FONT_B, color=_WHITE)


def _draw_footer(pg, page_w, page_h):
    import fitz
    top = page_h - _MB - _FTR_H
    rect = fitz.Rect(0, top, page_w, top + _FTR_H)
    _gradient_bar(pg, rect, _NAVY, _BLUE_MID)

    # Centered text
    thanks = "Thank you for choosing Simon Fresh Water."
    tw = len(thanks) * _SZ_BRAND * 0.5
    pg.insert_text(((page_w - tw) / 2, top + 17), thanks,
                   fontsize=_SZ_BRAND, fontname=_FONT_B, color=_WHITE)

    # Divider
    pg.draw_line(fitz.Point(_ML, top + 22),
                 fitz.Point(page_w - _MR, top + 22),
                 color=(1, 1, 1), width=0.4)

    contact = "SIMON FRESH WATER  \u00b7  KIKUYU  \u00b7  +254 700 719311  \u00b7  simonchege84@gmail.com"
    cw = len(contact) * _SZ_TINY * 0.5
    pg.insert_text(((page_w - cw) / 2, top + 34), contact,
                   fontsize=_SZ_TINY, fontname=_FONT, color=_WHITE)


# ═════════════════════════════════════════════════════════════
#  Entry point
# ═════════════════════════════════════════════════════════════

def generate_report_pdf(snapshot: dict) -> BytesIO:
    try:
        import fitz
    except ImportError as e:
        raise RuntimeError(
            "PyMuPDF (fitz) is required for report generation. "
            "Install with: pip install pymupdf"
        ) from e

    PAGE_W, PAGE_H = fitz.paper_size("a4-l")

    # ── Rows ──
    rows = list(snapshot.get("_all_rows", []) or [])
    if not rows:
        for i in range(1, 21):
            nm = snapshot.get(f"r{i}_name") or ""
            if not nm:
                continue
            rows.append({
                "id":    snapshot.get(f"r{i}_id") or "",
                "name":  nm,
                "meter": snapshot.get(f"r{i}_meter") or "",
                "cm3":   snapshot.get(f"r{i}_cm3") or "",
                "amt":   snapshot.get(f"r{i}_amt") or "",
                "paid":  snapshot.get(f"r{i}_paid") or "",
                "bal":   snapshot.get(f"r{i}_bal") or "",
            })

    # ── Scalars ──
    report_no    = snapshot.get("report_no", "")
    report_month = snapshot.get("report_month", "")
    report_year  = snapshot.get("report_year", "")
    generated    = snapshot.get("generated_date", "")
    tot_c        = snapshot.get("total_consumers", "0")
    act_c        = snapshot.get("active_consumers", "0")
    tot_cons     = snapshot.get("total_consumption", "0.00")
    tot_billed   = snapshot.get("total_billed", "0.00")
    tot_paid     = snapshot.get("total_paid", "0.00")
    tot_out      = snapshot.get("total_outstanding", "0.00")

    # ── Capacity ──
    page1_table_top = _MT + _HDR_H + _META_H + _BODY_PAD_TOP \
                      + _TITLE_H + _SUM_CELL_H + _SUM_GAP \
                      + _TITLE_H + 8
    page1_bottom = PAGE_H - _MB - _FTR_H - _FTR_GAP - _TOTALS_H
    page1_cap = max(int((page1_bottom - page1_table_top) // _ROW_H), 3)

    cont_table_top = _MT + _CONT_HDR_H + _THEAD_H
    cont_bottom = PAGE_H - _MB - _FTR_H - _FTR_GAP - _TOTALS_H
    cont_cap = max(int((cont_bottom - cont_table_top) // _ROW_H), 10)

    page1_rows = rows[:page1_cap]
    rest = rows[page1_cap:]
    chunks = [rest[i:i + cont_cap] for i in range(0, len(rest), cont_cap)]
    total_pages = 1 + len(chunks)

    totals = {"billed": tot_billed, "paid": tot_paid, "balance": tot_out}

    doc = fitz.open()

    # ═══════════ PAGE 1 ═══════════
    pg = doc.new_page(width=PAGE_W, height=PAGE_H)

    _draw_header_page1(pg, PAGE_W, report_month, report_year, report_no)

    meta_y = _MT + _HDR_H
    _draw_meta_band(pg, PAGE_W, meta_y, report_month, report_year,
                    generated, tot_c, report_no)

    body_top = meta_y + _META_H
    body_bottom = PAGE_H - _MB - _FTR_H - _FTR_GAP
    body_height = body_bottom - body_top
    # Body border: left/right/bottom
    _draw_body_border_top(pg, PAGE_W, body_top, body_height)

    y = body_top + _BODY_PAD_TOP

    # Summary section
    _draw_section_title(pg, _ML + _BODY_PAD_X, y, PAGE_W, "Summary")
    y += _TITLE_H
    _draw_summary_cells(pg, _ML + _BODY_PAD_X, y, PAGE_W, [
        ("ACTIVE CONSUMERS",   act_c),
        ("TOTAL CONSUMPTION",  f"{tot_cons} M\u00b3"),
        ("TOTAL BILLED",       f"KES {tot_billed}"),
        ("TOTAL PAID",         f"KES {tot_paid}"),
        ("OUTSTANDING",        f"KES {tot_out}"),
    ])
    y += _SUM_CELL_H + _SUM_GAP

    # Consumer Accounts section
    _draw_section_title(pg, _ML + _BODY_PAD_X, y, PAGE_W, "Consumer Accounts")
    y += _TITLE_H + 6

    # Table
    _draw_table_header(pg, y, PAGE_W)
    y += _THEAD_H

    for i, row in enumerate(page1_rows):
        _draw_row(pg, y + i * _ROW_H, row, i + 1, alt=(i % 2 == 1))
    y += len(page1_rows) * _ROW_H

    # Totals — page 1 is last page only if there are no continuation chunks
    if total_pages == 1:
        _draw_totals(pg, y + 6, PAGE_W, totals)

    _draw_footer(pg, PAGE_W, PAGE_H)

    # ═══════════ CONTINUATION PAGES ═══════════
    start_num = page1_cap + 1
    for page_idx, chunk in enumerate(chunks, start=2):
        pg = doc.new_page(width=PAGE_W, height=PAGE_H)
        _draw_cont_header(pg, PAGE_W, report_month, report_year, page_idx, total_pages)

        y = _MT + _CONT_HDR_H + 4
        _draw_table_header(pg, y, PAGE_W)
        y += _THEAD_H

        for i, row in enumerate(chunk):
            _draw_row(pg, y + i * _ROW_H, row, start_num + i, alt=(i % 2 == 1))
        y += len(chunk) * _ROW_H

        if page_idx == total_pages:
            _draw_totals(pg, y + 6, PAGE_W, totals)

        _draw_footer(pg, PAGE_W, PAGE_H)
        start_num += len(chunk)

    out = BytesIO()
    doc.save(out, garbage=4, deflate=True, clean=True)
    doc.close()
    out.seek(0)
    return out
