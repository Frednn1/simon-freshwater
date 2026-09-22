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
