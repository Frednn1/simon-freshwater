"""
Receipt PDF generator — fills {{placeholder}} tokens in the designated
PDF template with payment data.

Design principles:
  • No white rectangles drawn over placeholders.
  • Placeholder literal length is irrelevant — the replacement value is
    written at the placeholder's exact origin, at the placeholder's font
    size, with no padding and no wrapping.
  • If multiple bill allocations exist, the allocation row in the PDF is
    duplicated to fit them all.

Uses PyMuPDF (fitz). If PyMuPDF is not installed, generate_receipt_pdf
raises a clear RuntimeError.
"""
import os
from io import BytesIO


_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = [
    os.path.join(_HERE, "..", "templates", "SimonWater_ReceiptTemplate.pdf"),
    os.path.join(_HERE, "templates", "SimonWater_ReceiptTemplate.pdf"),
    os.path.join(_HERE, "SimonWater_ReceiptTemplate.pdf"),
]
TEMPLATE_PATH = next((p for p in _CANDIDATES if os.path.exists(p)), _CANDIDATES[0])


def _int_to_rgb(c):
    """Convert a packed 0xRRGGBB int to a (r, g, b) tuple of 0..1 floats."""
    return (
        ((c >> 16) & 0xFF) / 255.0,
        ((c >> 8) & 0xFF) / 255.0,
        (c & 0xFF) / 255.0,
    )


def _find_placeholders(page):
    """
    Walk every character on the page in document order.
    Whenever we see "{{" followed later by "}}", capture the whole
    placeholder (including any that wraps across lines) with:
      - name        : the inner text (whitespace-stripped)
      - origin      : (x, y) baseline of the first character
      - bbox        : (x0, y0, x1, y1) union of all its characters
      - size        : font size of the first character
      - font        : font name (informational)
      - color       : (r, g, b) of the first character
    """
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


def _value_for(snapshot, name, prefix=None, alloc=None):
    """
    Return the string to substitute for a placeholder.
    If `prefix` is set and the placeholder name starts with it, the lookup
    is done inside `alloc` (key = name without prefix). Otherwise it's a
    top-level snapshot lookup (case-insensitive).
    """
    key = name.lower()
    if prefix and key.startswith(prefix):
        short = key[len(prefix):]
        if alloc is None:
            return "—"
        val = alloc.get(short)
        return str(val) if val not in (None, "") else "—"
    for k, v in snapshot.items():
        if k.lower() == key:
            return str(v) if v not in (None, "") else "—"
    return "—"


def _apply_replacements(page, pairs):
    """
    pairs = [(placeholder_dict, text_value), ...]
    Steps:
      1. Mark every placeholder's bbox for redaction, with no fill.
      2. Apply redactions (removes original text, draws nothing).
      3. Insert replacement text at each origin, preserving font size/color.
    """
    if not pairs:
        return

    import fitz  # local import to keep module import-friendly

    for ph, _text in pairs:
        rect = fitz.Rect(ph["bbox"])
        page.add_redact_annot(rect, fill=None, text=None)

    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    for ph, text in pairs:
        if not text:
            continue
        x, y = ph["origin"]
        page.insert_text(
            (x, y),
            text,
            fontsize=ph["size"],
            fontname="helv",
            color=ph["color"],
        )


def generate_receipt_pdf(snapshot: dict) -> BytesIO:
    """Fill the designated PDF template and return a BytesIO."""
    try:
        import fitz
    except ImportError as e:
        raise RuntimeError(
            "PyMuPDF (fitz) is required for receipt generation. "
            "Install with: pip install pymupdf"
        ) from e

    if not os.path.exists(TEMPLATE_PATH):
        raise RuntimeError(f"Receipt template not found: {TEMPLATE_PATH}")

    doc = fitz.open(TEMPLATE_PATH)
    page = doc[0]

    initial = _find_placeholders(page)
    allocs = [p for p in initial if p["name"].lower().startswith("alloc_")]
    scalars = [p for p in initial if not p["name"].lower().startswith("alloc_")]

    allocations = snapshot.get("allocations") or []
    n_alloc = max(1, len(allocations))

    scalar_pairs = [(ph, _value_for(snapshot, ph["name"])) for ph in scalars]

    if not allocs:
        _apply_replacements(page, scalar_pairs)
    else:
        x0 = min(p["bbox"][0] for p in allocs)
        y0 = min(p["bbox"][1] for p in allocs)
        x1 = max(p["bbox"][2] for p in allocs)
        y1 = max(p["bbox"][3] for p in allocs)
        row_rect = fitz.Rect(x0, y0, x1, y1)

        if n_alloc > 1:
            src_doc = fitz.open(TEMPLATE_PATH)
            row_h = row_rect.height
            for i in range(1, n_alloc):
                target = fitz.Rect(
                    row_rect.x0, row_rect.y0 + i * row_h,
                    row_rect.x1, row_rect.y1 + i * row_h,
                )
                page.show_pdf_page(target, src_doc, 0, clip=row_rect)
            src_doc.close()

            # Re-scan placeholders (now N rows of alloc_*)
            rescanned = _find_placeholders(page)
            allocs = [p for p in rescanned if p["name"].lower().startswith("alloc_")]

        # Group alloc placeholders into rows by baseline y
        rows_by_y = {}
        for p in allocs:
            key = round(p["origin"][1], 1)
            rows_by_y.setdefault(key, []).append(p)
        sorted_rows = [rows_by_y[k] for k in sorted(rows_by_y.keys())]

        alloc_pairs = []
        for i, row in enumerate(sorted_rows):
            alloc = allocations[i] if i < len(allocations) else None
            for ph in row:
                alloc_pairs.append(
                    (ph, _value_for(snapshot, ph["name"], prefix="alloc_", alloc=alloc))
                )

        _apply_replacements(page, scalar_pairs + alloc_pairs)

    out = BytesIO()
    doc.save(out, garbage=4, deflate=True, clean=True)
    out.seek(0)
    return out
