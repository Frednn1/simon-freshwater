"""
Receipt PDF generator.

Approach:
  • Find every {{placeholder}} by walking the PDF's character stream —
    handles single-line, multi-line, and mixed-case tokens uniformly.
  • Erase the placeholder using a no-fill redaction (no white boxes).
  • Insert the replacement value at the placeholder's exact origin, at
    the placeholder's font size and colour. No padding, no stretching,
    no wrapping.

Bill Allocation is handled by 5 fixed rows in the template, named
a1_* through a5_*. Rows beyond the number of allocations are blanked.

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


# ─────────────────────────────────────────────
#  UTILITIES
# ─────────────────────────────────────────────

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
    Whenever we see '{{' followed later by '}}', capture the whole
    placeholder (including any that wraps across lines) with:
      - name        : the inner text, whitespace-stripped
      - origin      : (x, y) baseline of the first '{'
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


# ─────────────────────────────────────────────
#  VALUE LOOKUPS
# ─────────────────────────────────────────────

def _scalar_value(snapshot, name):
    """Top-level lookup, case-insensitive. Empty/missing → em-dash."""
    key = name.lower()
    for k, v in snapshot.items():
        if k.lower() == key:
            return str(v) if v not in (None, "") else "—"
    return "—"


def _alloc_value(alloc, key):
    """Allocation-row lookup. Empty/missing → blank string."""
    if alloc is None:
        return ""
    v = alloc.get(key)
    if v in (None, ""):
        return ""
    return str(v)


def _build_alloc_replacements(snapshot):
    """
    For each of the 5 fixed allocation rows, build:
        a<N>_date, a<N>_m3, a<N>_amt, a<N>_applied, a<N>_bal
    Values come from snapshot['allocations'][N-1], or blank if absent.
    """
    allocations = snapshot.get("allocations") or []
    out = {}
    for idx in range(1, 6):
        alloc = allocations[idx - 1] if idx - 1 < len(allocations) else None
        out[f"a{idx}_date"]    = _alloc_value(alloc, "reading_date")
        out[f"a{idx}_m3"]      = _alloc_value(alloc, "reading_m3")
        out[f"a{idx}_amt"]     = _alloc_value(alloc, "amount_kes")
        out[f"a{idx}_applied"] = _alloc_value(alloc, "applied")
        out[f"a{idx}_bal"]     = _alloc_value(alloc, "new_balance")
    return out


# ─────────────────────────────────────────────
#  APPLY
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
#  PUBLIC API
# ─────────────────────────────────────────────

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

    alloc_values = _build_alloc_replacements(snapshot)

    placeholders = _find_placeholders(page)
    pairs = []
    for ph in placeholders:
        name = ph["name"].lower()
        if name in alloc_values:
            pairs.append((ph, alloc_values[name]))
        else:
            pairs.append((ph, _scalar_value(snapshot, ph["name"])))

    _apply_replacements(page, pairs)

    out = BytesIO()
    doc.save(out, garbage=4, deflate=True, clean=True)
    doc.close()
    out.seek(0)
    return out
