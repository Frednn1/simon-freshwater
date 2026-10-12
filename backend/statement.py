"""
Customer statement PDF generator.

Same rendering approach as receipts.py / water_bill.py:
  • Walk the character stream to find every {{placeholder}}.
  • Redact each placeholder's bbox with no fill.
  • Insert the value at the placeholder's origin, at its font size.

The snapshot dict is expected to already contain ALL 137 keys
(scalars + t1_*..t20_*). Empty-string values render as blank cells;
missing keys render as an em-dash.
"""
import os
import re as _re
from io import BytesIO


_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = [
    os.path.join(_HERE, "..", "templates", "SimonWater_StatementTemplate.pdf"),
    os.path.join(_HERE, "templates", "SimonWater_StatementTemplate.pdf"),
    os.path.join(_HERE, "SimonWater_StatementTemplate.pdf"),
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
    """
    Case-insensitive lookup. Returns "" when the key is missing or the
    value is None/empty — so placeholder cells render completely blank,
    never a visible token.
    """
    key = name.lower()
    for k, v in snapshot.items():
        if k.lower() == key:
            return "" if v is None else str(v)
    return ""


def _apply_replacements(page, pairs):
    if not pairs:
        return
    import fitz

    # Group by y coordinate
    groups = {}
    for ph, _text in pairs:
        y_key = round(ph["origin"][1], 1)
        groups.setdefault(y_key, []).append((ph, _text))

    # Reflow only lines where ALL placeholders are brand_*
    reflow_keys = set()
    for k, g in groups.items():
        if len(g) < 3:
            continue
        names = [p[0]["name"].lower() for p in g]
        if all(n.startswith("brand_") for n in names):
            reflow_keys.add(k)

    # Redact non-reflow placeholders individually
    for ph, _text in pairs:
        y_key = round(ph["origin"][1], 1)
        if y_key in reflow_keys:
            continue
        page.add_redact_annot(fitz.Rect(ph["bbox"]), fill=None, text=None)

    # Redact the full bbox for each reflow line
    for y_key in reflow_keys:
        g = sorted(groups[y_key], key=lambda p: p[0]["origin"][0])
        x0 = g[0][0]["bbox"][0]
        x1 = g[-1][0]["bbox"][2]
        y0 = min(p[0]["bbox"][1] for p in g)
        y1 = max(p[0]["bbox"][3] for p in g)
        page.add_redact_annot(fitz.Rect(x0, y0, x1, y1),
                              fill=None, text=None)

    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    # Insert non-reflow placeholders (colour override for green credit rows
    # is applied BEFORE this function is called, on ph["color"]).
    for ph, text in pairs:
        y_key = round(ph["origin"][1], 1)
        if y_key in reflow_keys:
            continue
        if not text:
            continue
        page.insert_text(
            (ph["origin"][0], ph["origin"][1]), text,
            fontsize=ph["size"],
            fontname="helv",
            color=ph["color"],
        )

    # Insert each reflow line as one continuous string
    for y_key in reflow_keys:
        g = sorted(groups[y_key], key=lambda p: p[0]["origin"][0])
        parts = [str(txt) for _, txt in g if txt]
        if not parts:
            continue
        line = " \u00b7 ".join(parts)
        first_ph = g[0][0]
        try:
            text_w = fitz.get_text_length(
                line, fontname="helv", fontsize=first_ph["size"])
        except Exception:
            text_w = 0.0
        if text_w > 0:
            x = (page.rect.width - text_w) / 2.0
        else:
            x = first_ph["origin"][0]
        page.insert_text(
            (x, first_ph["origin"][1]), line,
            fontsize=first_ph["size"],
            fontname="helv",
            color=first_ph["color"],
        )


def generate_statement_pdf(snapshot: dict) -> BytesIO:
    try:
        import fitz
    except ImportError as e:
        raise RuntimeError(
            "PyMuPDF (fitz) is required for statement generation. "
            "Install with: pip install pymupdf"
        ) from e

    if not os.path.exists(TEMPLATE_PATH):
        raise RuntimeError(f"Statement template not found: {TEMPLATE_PATH}")

    doc = fitz.open(TEMPLATE_PATH)

    # Process EVERY page — placeholders may span page boundaries.
    # On a 2-page statement, page 2 holds the overflow rows + totals.
    _GREEN = (0.0, 0.5, 0.0)

    for page in doc.pages():
        placeholders = _find_placeholders(page)
        pairs = []
        for ph in placeholders:
            text = _scalar_value(snapshot, ph["name"])
            nm = ph["name"].lower()
            m = _re.match(r"t(\d+)_(type|ref|cr|dr|bal)$", nm)
            if m:
                idx = m.group(1)
                if snapshot.get(f"t{idx}_is_credit") == "1":
                    ph = dict(ph)
                    ph["color"] = _GREEN
            pairs.append((ph, text))
        _apply_replacements(page, pairs)

    out = BytesIO()
    doc.save(out, garbage=4, deflate=True, clean=True)
    doc.close()
    out.seek(0)
    return out
