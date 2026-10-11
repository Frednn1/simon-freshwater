"""
Water bill PDF generator.

Rendering approach mirrors receipts.py:
  • Walk the character stream to find every {{placeholder}} — handles
    single-line, multi-line, mixed-case tokens uniformly.
  • Redact each placeholder's bbox with no fill (no white boxes).
  • Insert the value at the placeholder's origin, at its font size and
    colour. No padding, no stretching, no wrapping.

Uses PyMuPDF (fitz). If PyMuPDF is not installed, generate_water_bill_pdf
raises a clear RuntimeError.
"""
import os
from io import BytesIO


_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = [
    os.path.join(_HERE, "..", "templates", "water_bill_template.pdf"),
    os.path.join(_HERE, "templates", "water_bill_template.pdf"),
    os.path.join(_HERE, "water_bill_template.pdf"),
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
    """Case-insensitive lookup. Empty/missing → em-dash."""
    key = name.lower()
    for k, v in snapshot.items():
        if k.lower() == key:
            return str(v) if v not in (None, "") else "—"
    return "—"


def _apply_replacements(page, pairs):
    if not pairs:
        return
    import fitz

    # Reflow only lines where ALL placeholders are brand_* (the
    # branding footer). Without this, substituted values that are shorter
    # than their placeholders leave wide visual gaps before the next
    # separator dot. Ledger rows are untouched — they contain t{n}_*
    # placeholders, not brand_*.
    groups = {}
    for ph, _text in pairs:
        y_key = round(ph["origin"][1], 1)
        groups.setdefault(y_key, []).append((ph, _text))

    reflow_keys = set()
    for k, g in groups.items():
        if len(g) < 3:
            continue
        names = [p[0]["name"].lower() for p in g]
        if all(n.startswith("brand_") for n in names):
            reflow_keys.add(k)

    for ph, _text in pairs:
        y_key = round(ph["origin"][1], 1)
        if y_key in reflow_keys:
            continue
        page.add_redact_annot(fitz.Rect(ph["bbox"]), fill=None, text=None)

    for y_key in reflow_keys:
        g = sorted(groups[y_key], key=lambda p: p[0]["origin"][0])
        x0 = g[0][0]["bbox"][0]
        x1 = g[-1][0]["bbox"][2]
        y0 = min(p[0]["bbox"][1] for p in g)
        y1 = max(p[0]["bbox"][3] for p in g)
        page.add_redact_annot(fitz.Rect(x0, y0, x1, y1),
                              fill=None, text=None)

    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

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

    for y_key in reflow_keys:
        g = sorted(groups[y_key], key=lambda p: p[0]["origin"][0])
        parts = [str(txt) for _, txt in g if txt]
        if not parts:
            continue
        line = " \u00b7 ".join(parts)
        first_ph = g[0][0]
        page.insert_text(
            (first_ph["origin"][0], first_ph["origin"][1]), line,
            fontsize=first_ph["size"],
            fontname="helv",
            color=first_ph["color"],
        )


def generate_water_bill_pdf(snapshot: dict) -> BytesIO:
    """Fill the water bill template and return a BytesIO."""
    try:
        import fitz
    except ImportError as e:
        raise RuntimeError(
            "PyMuPDF (fitz) is required for water bill generation. "
            "Install with: pip install pymupdf"
        ) from e

    if not os.path.exists(TEMPLATE_PATH):
        raise RuntimeError(f"Water bill template not found: {TEMPLATE_PATH}")

    doc = fitz.open(TEMPLATE_PATH)
    page = doc[0]

    placeholders = _find_placeholders(page)
    pairs = [(ph, _scalar_value(snapshot, ph["name"])) for ph in placeholders]
    _apply_replacements(page, pairs)

    out = BytesIO()
    doc.save(out, garbage=4, deflate=True, clean=True)
    doc.close()
    out.seek(0)
    return out
