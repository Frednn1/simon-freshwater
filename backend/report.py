"""
Monthly billing report PDF generator — fully programmatic.

No PDF template. Every page is drawn with PyMuPDF primitives so:
  • Row count grows with the data (no fixed 20-row limit).
  • Font is Times-Roman 11 pt body / 12 pt headers — legible.
  • All labels and values are left-aligned.
  • Content flows to a new page when the current one is full.
  • Continuation pages continue the table from the top.

The snapshot dict must contain:
  _all_rows       — list of {'id','name','meter','cm3','amt','paid','bal'}
  report_no, report_month, report_year, generated_date
  total_consumers, active_consumers
  total_consumption, total_billed, total_paid, total_outstanding
"""
from io import BytesIO


def _rgb(h):
    return (((h >> 16) & 0xFF) / 255.0,
            ((h >> 8)  & 0xFF) / 255.0,
            ( h        & 0xFF) / 255.0)


_NAVY  = _rgb(0x0a2540)
_BLUE  = _rgb(0x0d47a1)
_LIGHT = _rgb(0xe3f2fd)
_MID   = _rgb(0xcfd8dc)
_GREY  = _rgb(0x455a64)
_DARK  = _rgb(0x1c2b36)
_WHITE = (1.0, 1.0, 1.0)

_FONT   = "times-roman"
_FONT_B = "times-bold"

_SZ_H1     = 16
_SZ_H2     = 12
_SZ_META   = 10
_SZ_LABEL  = 8.5
_SZ_BODY   = 11
_SZ_BRAND  = 8
_SZ_TINY   = 7

_ML = 40
_MR = 40
_MT = 40
_MB = 40
_ROW_H     = 18
_HEADER_H  = 46
_META_H    = 30
_SUMMARY_H = 48
_COLHDR_H  = 22
_FOOTER_H  = 30
_CONT_HDR_H  = 32
_CONT_COLHDR_H = 22

_COL_X = {
    "num":   40,
    "name":  66,
    "meter": 266,
    "cm3":   376,
    "amt":   466,
    "paid":  556,
    "bal":   646,
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
    "name":  34,
    "meter": 16,
    "cm3":   14,
    "amt":   14,
    "paid":  14,
    "bal":   32,
}


def _trunc(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "."


def _draw_page1_header(pg, page_w, report_month, report_year):
    import fitz
    pg.draw_rect(fitz.Rect(0, 0, page_w, _MT + _HEADER_H),
                 color=None, fill=_NAVY)
    y = _MT
    pg.insert_text((_ML, y + 18), "SIMON FRESH WATER",
                   fontsize=_SZ_H1, fontname=_FONT_B, color=_WHITE)
    pg.insert_text((_ML, y + 34), "Kikuyu, Kenya  \u00b7  Monthly Billing Report",
                   fontsize=_SZ_META, fontname=_FONT, color=_WHITE)
    right = page_w - _MR
    title = "MONTHLY BILLING REPORT"
    title_w = len(title) * _SZ_H2 * 0.55
    pg.insert_text((right - title_w, y + 18), title,
                   fontsize=_SZ_H2, fontname=_FONT_B, color=_WHITE)
    period = f"{report_month} {report_year}"
    period_w = len(period) * _SZ_META * 0.5
    pg.insert_text((right - period_w, y + 34), period,
                   fontsize=_SZ_META, fontname=_FONT, color=_WHITE)


def _draw_meta(pg, x, y, label, value):
    pg.insert_text((x, y), label,
                   fontsize=_SZ_LABEL, fontname=_FONT_B, color=_BLUE)
    pg.insert_text((x, y + 13), _trunc(value, 42),
                   fontsize=_SZ_META, fontname=_FONT, color=_DARK)


def _draw_summary(pg, x, y_label, y_value, label, value):
    pg.insert_text((x, y_label), label,
                   fontsize=_SZ_LABEL, fontname=_FONT_B, color=_BLUE)
    pg.insert_text((x, y_value), value,
                   fontsize=_SZ_META + 1, fontname=_FONT_B, color=_DARK)


def _draw_colheader(pg, y, page_w):
    import fitz
    pg.draw_rect(
        fitz.Rect(_ML, y, page_w - _MR, y + _COLHDR_H),
        color=None, fill=_LIGHT,
    )
    baseline = y + _COLHDR_H * 0.7
    for col, label in _COL_LABEL.items():
        pg.insert_text((_COL_X[col], baseline), label,
                       fontsize=_SZ_LABEL, fontname=_FONT_B, color=_BLUE)


def _draw_row(pg, y, row, row_number):
    baseline = y + _ROW_H * 0.72
    pg.insert_text((_COL_X["num"], baseline), str(row_number),
                   fontsize=_SZ_BODY - 1, fontname=_FONT, color=_GREY)
    for col in ("name", "meter", "cm3", "amt", "paid", "bal"):
        pg.insert_text(
            (_COL_X[col], baseline),
            _trunc(row.get(col, ""), _TRUNC[col]),
            fontsize=_SZ_BODY, fontname=_FONT, color=_DARK,
        )


def _draw_footer(pg, page_w, page_h, totals, page_idx, total_pages):
    import fitz
    if page_idx == total_pages and totals:
        ty = page_h - _MB - _FOOTER_H - 16
        pg.draw_line(
            fitz.Point(_ML, ty - 6),
            fitz.Point(page_w - _MR, ty - 6),
            color=_MID, width=0.5,
        )
        pg.insert_text((_COL_X["meter"], ty + 10), "TOTALS",
                       fontsize=_SZ_LABEL, fontname=_FONT_B, color=_BLUE)
        pg.insert_text((_COL_X["amt"], ty + 10),
                       f"Billed {totals['billed']}",
                       fontsize=_SZ_BODY, fontname=_FONT_B, color=_DARK)
        pg.insert_text((_COL_X["paid"], ty + 10),
                       f"Paid {totals['paid']}",
                       fontsize=_SZ_BODY, fontname=_FONT_B, color=_DARK)
        pg.insert_text((_COL_X["bal"], ty + 10),
                       f"Balance {totals['balance']}",
                       fontsize=_SZ_BODY, fontname=_FONT_B, color=_DARK)

    fy = page_h - _MB + 8
    pg.insert_text((_ML, fy),
                   "Thank you for choosing Simon Fresh Water.",
                   fontsize=_SZ_BRAND, fontname=_FONT, color=_GREY)
    pg.insert_text((_ML, fy + 10),
                   "SIMON FRESH WATER  \u00b7  KIKUYU  \u00b7  +254 700 719311  \u00b7  simonchege84@gmail.com",
                   fontsize=_SZ_TINY, fontname=_FONT, color=_GREY)


def _draw_cont_header(pg, page_w, report_month, report_year, page_idx, total_pages):
    import fitz
    pg.draw_rect(fitz.Rect(0, 0, page_w, _MT + _CONT_HDR_H),
                 color=None, fill=_NAVY)
    y = _MT + 18
    pg.insert_text((_ML, y), "SIMON FRESH WATER",
                   fontsize=_SZ_H2, fontname=_FONT_B, color=_WHITE)
    pg.insert_text((_ML + 210, y), "\u00b7 Monthly Billing Report \u2014 continued",
                   fontsize=_SZ_META, fontname=_FONT, color=_WHITE)
    right_txt = f"{report_month} {report_year}  \u00b7  Page {page_idx} of {total_pages}"
    tw = len(right_txt) * _SZ_META * 0.5
    pg.insert_text((page_w - _MR - tw, y), right_txt,
                   fontsize=_SZ_META, fontname=_FONT_B, color=_WHITE)


def generate_report_pdf(snapshot: dict) -> BytesIO:
    try:
        import fitz
    except ImportError as e:
        raise RuntimeError(
            "PyMuPDF (fitz) is required for report generation. "
            "Install with: pip install pymupdf"
        ) from e

    PAGE_W, PAGE_H = fitz.paper_size("a4-l")

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

    page1_table_top = _MT + _HEADER_H + _META_H + _SUMMARY_H
    page1_space     = PAGE_H - page1_table_top - _COLHDR_H - _MB - _FOOTER_H - 20
    page1_cap       = max(int(page1_space // _ROW_H), 3)

    cont_table_top = _MT + _CONT_HDR_H
    cont_space     = PAGE_H - cont_table_top - _CONT_COLHDR_H - _MB - _FOOTER_H - 20
    cont_cap       = max(int(cont_space // _ROW_H), 10)

    page1_rows = rows[:page1_cap]
    rest_rows  = rows[page1_cap:]
    chunks     = [rest_rows[i:i + cont_cap] for i in range(0, len(rest_rows), cont_cap)]
    total_pages = 1 + len(chunks)

    totals = {"billed": tot_billed, "paid": tot_paid, "balance": tot_out}

    doc = fitz.open()

    pg = doc.new_page(width=PAGE_W, height=PAGE_H)
    _draw_page1_header(pg, PAGE_W, report_month, report_year)

    meta_y = _MT + _HEADER_H + 10
    _draw_meta(pg, _ML + 0,    meta_y, "REPORT NO.",       report_no)
    _draw_meta(pg, _ML + 180,  meta_y, "PERIOD",           f"{report_month} {report_year}")
    _draw_meta(pg, _ML + 370,  meta_y, "GENERATED",        generated)
    _draw_meta(pg, _ML + 560,  meta_y, "TOTAL CONSUMERS",  tot_c)

    sum_top = _MT + _HEADER_H + _META_H + 4
    pg.draw_rect(
        fitz.Rect(_ML, sum_top, PAGE_W - _MR, sum_top + _SUMMARY_H - 8),
        color=_MID, fill=_LIGHT, width=0.5,
    )
    y_lbl = sum_top + 14
    y_val = sum_top + 34
    _draw_summary(pg, _ML +  14, y_lbl, y_val, "ACTIVE CONSUMERS",        act_c)
    _draw_summary(pg, _ML + 190, y_lbl, y_val, "TOTAL CONSUMPTION (M\u00b3)",  tot_cons)
    _draw_summary(pg, _ML + 370, y_lbl, y_val, "TOTAL BILLED (KES)",      tot_billed)
    _draw_summary(pg, _ML + 550, y_lbl, y_val, "TOTAL PAID (KES)",        tot_paid)
    _draw_summary(pg, _ML + 700, y_lbl, y_val, "OUTSTANDING (KES)",       tot_out)

    _draw_colheader(pg, page1_table_top - _COLHDR_H, PAGE_W)

    row_y = page1_table_top
    for i, row in enumerate(page1_rows):
        _draw_row(pg, row_y + i * _ROW_H, row, i + 1)

    _draw_footer(pg, PAGE_W, PAGE_H, totals, 1, total_pages)

    start_num = page1_cap + 1
    for page_idx, chunk in enumerate(chunks, start=2):
        pg = doc.new_page(width=PAGE_W, height=PAGE_H)
        _draw_cont_header(pg, PAGE_W, report_month, report_year, page_idx, total_pages)
        _draw_colheader(pg, _MT + _CONT_HDR_H, PAGE_W)

        for i, row in enumerate(chunk):
            _draw_row(pg, cont_table_top + _CONT_COLHDR_H + i * _ROW_H,
                      row, start_num + i)

        _draw_footer(pg, PAGE_W, PAGE_H, totals, page_idx, total_pages)
        start_num += len(chunk)

    out = BytesIO()
    doc.save(out, garbage=4, deflate=True, clean=True)
    doc.close()
    out.seek(0)
    return out
