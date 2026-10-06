"""
Monthly billing report PDF generator.

Same rendering approach as receipts.py / statement.py / water_bill.py:
  • Walk the character stream to find every {{placeholder}}.
  • Redact each placeholder's bbox with no fill.
  • Insert the value at the placeholder's origin, at its font size.

The snapshot dict is expected to already contain ALL keys
(scalars + r1_*..r20_*). Empty-string values render as blank cells.
"""
import os
from io import BytesIO


_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = [
    os.path.join(_HERE, "..", "templates", "SimonWater_ReportTemplate.pdf"),
    os.path.join(_HERE, "templates", "SimonWater_ReportTemplate.pdf"),
    os.path.join(_HERE, "SimonWater_ReportTemplate.pdf"),
]
TEMPLATE_PATH = next((p for p in _CANDIDATES if os.path.exists(p)), _CANDIDATES[0])


def _int_to_rgb(c):
    return (
        ((c >> 16) & 0xFF) / 255.0,
        ((c >> 8) & 0xFF) / 255.0,
        (c & 0xFF) / 255.0,
    )


def _find_placeholders(page):
    raw = page.get_text("rawdict")
    chars = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                size = span.get("size", 10)
                font = span.get("font", "helv")
                color = _int_to_rgb(span.get("color", 0))
                for ch in span.get("chars", []):
                    chars.append({
                        "c":      ch.get("c", ""),
                        "origin": ch.get("origin", (0, 0)),
                        "bbox":   ch.get("bbox", (0, 0, 0, 0)),
                        "size":   size,
                        "font":   font,
                        "color":  color,
                    })

    results = []
    i, n = 0, len(chars)
    while i < n:
        if chars[i]["c"] == "{" and i + 1 < n and chars[i + 1]["c"] == "{":
            j = i + 2
            while j < n - 1:
                if chars[j]["c"] == "}" and chars[j + 1]["c"] == "}":
                    break
                j += 1
            if j < n - 1:
                inner = "".join(chars[k]["c"] for k in range(i + 2, j))
                name = inner.strip()
                all_chars = chars[i:j + 2]
                x0 = min(c["bbox"][0] for c in all_chars)
                y0 = min(c["bbox"][1] for c in all_chars)
                x1 = max(c["bbox"][2] for c in all_chars)
                y1 = max(c["bbox"][3] for c in all_chars)
                results.append({
                    "name":   name,
                    "origin": chars[i]["origin"],
                    "bbox":   (x0, y0, x1, y1),
                    "size":   chars[i]["size"],
                    "font":   chars[i]["font"],
                    "color":  chars[i]["color"],
                })
                i = j + 2
                continue
        i += 1
    return results


def _scalar_value(snapshot, name):
    key = name.lower()
    for k, v in snapshot.items():
        if k.lower() == key:
            return "" if v is None else str(v)
    return "—"


def _apply_replacements(page, pairs):
    if not pairs:
        return
    import fitz

    for ph, _text in pairs:
        page.add_redact_annot(fitz.Rect(ph["bbox"]), fill=None, text=None)

    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    for ph, text in pairs:
        if not text:
            continue
        x, y = ph["origin"]
        page.insert_text(
            (x, y), text,
            fontsize=ph["size"],
            fontname="helv",
            color=ph["color"],
        )


def _extract_layout(page):
    """Read column x-positions, row height, and font size from the template.
    Uses the r1_* and r2_* placeholder origins as anchors — no hardcoding."""
    placeholders = _find_placeholders(page)

    def find(name):
        for ph in placeholders:
            if ph["name"].lower() == name.lower():
                return ph
        return None

    cols = {}
    for c in ("name", "meter", "cm3", "amt", "paid", "bal"):
        ph = find(f"r1_{c}")
        if ph is None:
            return None
        cols[c] = ph["origin"][0]

    r1 = find("r1_name")
    r2 = find("r2_name")
    if r1 is None or r2 is None:
        return None
    row_h = r2["origin"][1] - r1["origin"][1]
    if row_h <= 0 or row_h > 40:
        row_h = 11.0  # safe fallback

    return {
        "cols":       cols,
        "row_h":      row_h,
        "font_size":  r1.get("size", 8.0),
        "text_color": r1.get("color", (0, 0, 0)),
        "page_w":     page.rect.width,
        "page_h":     page.rect.height,
    }


def _append_continuation_pages(doc, layout, extra_rows, snapshot):
    """Add continuation pages for rows beyond the template's 20.
    Column positions come from the template — no hardcoded x values."""
    import fitz

    # ── Palette matching the template ──
    NAVY   = (0x0a / 255, 0x25 / 255, 0x40 / 255)
    BLUE   = (0x0d / 255, 0x47 / 255, 0xa1 / 255)
    LIGHT  = (0xe3 / 255, 0xf2 / 255, 0xfd / 255)
    GREY   = (0x45 / 255, 0x5a / 255, 0x64 / 255)
    WHITE  = (1.0, 1.0, 1.0)
    DARK   = (0x1c / 255, 0x2b / 255, 0x36 / 255)

    cols        = layout["cols"]
    row_h       = layout["row_h"]
    text_size   = min(layout["font_size"], 9.0)
    text_color  = layout["text_color"]
    pw, ph      = layout["page_w"], layout["page_h"]

    # Row number column: 45pt left of the name column
    hash_x = max(cols["name"] - 45, 12)

    # ── Layout bands ──
    HEADER_H  = 36
    COLHDR_H  = 18
    FOOTER_H  = 24
    rows_start = HEADER_H + COLHDR_H
    usable_h   = ph - rows_start - FOOTER_H
    rows_per_page = max(int(usable_h // row_h), 5)

    # ── Chunk the extra rows ──
    chunks = [extra_rows[i:i + rows_per_page]
              for i in range(0, len(extra_rows), rows_per_page)]
    total_cont = len(chunks)

    report_no  = snapshot.get("report_no", "")
    rpt_month  = snapshot.get("report_month", "")
    rpt_year   = snapshot.get("report_year", "")

    col_titles = {
        "name":  "CUSTOMER",
        "meter": "METER NO.",
        "cm3":   "CONSUMPTION (M³)",
        "amt":   "BILLED",
        "paid":  "PAID",
        "bal":   "BALANCE / STATUS",
    }

    for page_idx, chunk in enumerate(chunks, start=1):
        pg = doc.new_page(width=pw, height=ph)

        # ── Header band ──
        pg.draw_rect(fitz.Rect(0, 0, pw, HEADER_H),
                     color=None, fill=NAVY)

        pg.insert_text((18, HEADER_H * 0.5 + 3),
                       "SIMON FRESH WATER",
                       fontsize=11, fontname="hebo", color=WHITE)
        pg.insert_text((18, HEADER_H * 0.5 + 16),
                       "Monthly Billing Report  —  continued",
                       fontsize=7.5, fontname="helv", color=WHITE)

        period_str = f"{rpt_month} {rpt_year}  ·  {report_no}  ·  Page {page_idx + 1} of {total_cont + 1}"
        approx_w = len(period_str) * 3.9
        pg.insert_text((pw - 18 - approx_w, HEADER_H * 0.55 + 3),
                       period_str,
                       fontsize=7.5, fontname="helv", color=WHITE)

        # ── Column header row ──
        hdr_top = HEADER_H
        pg.draw_rect(fitz.Rect(0, hdr_top, pw, hdr_top + COLHDR_H),
                     color=None, fill=LIGHT)
        base_y = hdr_top + COLHDR_H * 0.72
        pg.insert_text((hash_x, base_y), "#",
                       fontsize=7.5, fontname="hebo", color=BLUE)
        for c, label in col_titles.items():
            pg.insert_text((cols[c], base_y), label,
                           fontsize=7.5, fontname="hebo", color=BLUE)

        # ── Data rows ──
        for i, row in enumerate(chunk):
            row_number = 21 + (page_idx - 1) * rows_per_page + i
            baseline = rows_start + i * row_h + row_h * 0.78

            pg.insert_text((hash_x, baseline), str(row_number),
                           fontsize=text_size, fontname="helv", color=GREY)

            for c, key in (("name", "name"), ("meter", "meter"),
                           ("cm3", "cm3"), ("amt", "amt"),
                           ("paid", "paid"), ("bal", "bal")):
                val = str(row.get(key, "") or "")
                if len(val) > 34:
                    val = val[:33] + "."
                pg.insert_text((cols[c], baseline), val,
                               fontsize=text_size, fontname="helv",
                               color=text_color)

        # ── Footer ──
        fy = ph - FOOTER_H + 6
        pg.insert_text((18, fy),
                       "Thank you for choosing Simon Fresh Water.",
                       fontsize=8, fontname="helv", color=GREY)
        pg.insert_text((18, fy + 11),
                       "SIMON FRESH WATER · KIKUYU · +254 700 719311 · simonchege84@gmail.com",
                       fontsize=6.5, fontname="helv", color=GREY)


def generate_report_pdf(snapshot: dict) -> BytesIO:
    try:
        import fitz
    except ImportError as e:
        raise RuntimeError(
            "PyMuPDF (fitz) is required for report generation. "
            "Install with: pip install pymupdf"
        ) from e

    if not os.path.exists(TEMPLATE_PATH):
        raise RuntimeError(f"Report template not found: {TEMPLATE_PATH}")

    doc = fitz.open(TEMPLATE_PATH)
    page = doc[0]

    placeholders = _find_placeholders(page)
    pairs = [(ph, _scalar_value(snapshot, ph["name"])) for ph in placeholders]
    _apply_replacements(page, pairs)

    # ── Continuation pages for rows 21+ ──
    extra_rows = snapshot.get("_extra_rows", []) or []
    if extra_rows:
        layout = _extract_layout(page)
        if layout is None:
            # Fallback: template placeholders missing — log and skip extras
            print("[Report] continuation skipped — template anchors not found")
        else:
            _append_continuation_pages(doc, layout, extra_rows, snapshot)

    out = BytesIO()
    doc.save(out, garbage=4, deflate=True, clean=True)
    doc.close()
    out.seek(0)
    return out
