"""
Receipt PDF generator — fills {{placeholder}} tokens in the designated
PDF template with payment data.

Approach:
  • Locate each {{placeholder}} using PyMuPDF's search_for() — robust
    for both single-line and multi-line tokens.
  • Erase the placeholder using a no-fill redaction — no white boxes.
  • Insert the replacement value at the placeholder's exact origin, at
    the placeholder's font size. No padding, no stretching, no wrapping.
    Short values stay short; long values simply flow.

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


# Scalar placeholders (both lowercase + UPPERCASE aliases are supported)
_SCALAR_KEYS = (
    "receipt_no", "receipt_date", "receipt_time",
    "cust_name", "acc_name", "meter_acc_no", "contact", "email", "address",
    "amount_kes", "method", "reference", "recorded_by",
    "previous_balance", "remaining_balance", "prepaid_credit", "status_label",
    "last_reading_date", "last_reading_m3",
)


def _collect_spans(page):
    """Return a list of span info dicts (bbox + size) from the text layer."""
    spans = []
    raw = page.get_text("dict")
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                spans.append({
                    "bbox": tuple(span["bbox"]),
                    "size": float(span.get("size", 9)),
                })
    return spans


def _font_size_for_rect(rect, spans, fallback=9.0):
    """Font size of the span that best overlaps `rect`."""
    best_size = fallback
    best_overlap = 0.0
    rx0, ry0, rx1, ry1 = rect
    for s in spans:
        bx0, by0, bx1, by1 = s["bbox"]
        ix0 = max(rx0, bx0)
        iy0 = max(ry0, by0)
        ix1 = min(rx1, bx1)
        iy1 = min(ry1, by1)
        if ix1 <= ix0 or iy1 <= iy0:
            continue
        overlap = (ix1 - ix0) * (iy1 - iy0)
        if overlap > best_overlap:
            best_overlap = overlap
            best_size = s["size"]
    return best_size


def _build_value_map(snapshot):
    """Return {placeholder_name: value_string} for every supported key."""
    values = {}

    # Scalars — include lowercase and UPPERCASE variants
    for key in _SCALAR_KEYS:
        v = snapshot.get(key)
        v_str = str(v) if v not in (None, "") else ""
        values[key] = v_str
        values[key.upper()] = v_str

    # Fixed 5 allocation rows
    allocations = snapshot.get("allocations") or []
    for idx in range(1, 6):
        alloc = allocations[idx - 1] if idx - 1 < len(allocations) else None
        if alloc:
            values[f"a{idx}_date"]    = str(alloc.get("reading_date") or "")
            values[f"a{idx}_m3"]      = str(alloc.get("reading_m3") or "")
            values[f"a{idx}_amt"]     = str(alloc.get("amount_kes") or "")
            values[f"a{idx}_applied"] = str(alloc.get("applied") or "")
            values[f"a{idx}_bal"]     = str(alloc.get("new_balance") or "")
        else:
            values[f"a{idx}_date"]    = ""
            values[f"a{idx}_m3"]      = ""
            values[f"a{idx}_amt"]     = ""
            values[f"a{idx}_applied"] = ""
            values[f"a{idx}_bal"]     = ""

    return values


def _find_replacements(page, values):
    """Return a list of {rect, text, size} for every placeholder on the page."""
    spans = _collect_spans(page)
    found = []

    for key, text in values.items():
        token = "{{" + key + "}}"
        rects = page.search_for(token)
        if not rects:
            continue
        for rect in rects:
            size = _font_size_for_rect(rect, spans, fallback=9.0)
            found.append({"rect": rect, "text": text, "size": size})

    return found


def _apply(page, items):
    """Erase placeholder rects (no white fill), then insert replacement text."""
    import fitz

    if not items:
        return

    # 1. Mark every placeholder rect for redaction (no fill, no replacement text)
    for item in items:
        page.add_redact_annot(item["rect"], fill=None, text=None)

    # 2. Apply all redactions in one pass
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    # 3. Insert the replacement values
    for item in items:
        if not item["text"]:
            continue
        rect = item["rect"]
        size = item["size"]
        baseline_y = rect.y1 - size * 0.22
        page.insert_text(
            (rect.x0, baseline_y),
            item["text"],
            fontsize=size,
            fontname="helv",
            color=(0, 0, 0),
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

    values = _build_value_map(snapshot)
    items = _find_replacements(page, values)
    _apply(page, items)

    out = BytesIO()
    doc.save(out, garbage=4, deflate=True, clean=True)
    doc.close()
    out.seek(0)
    return out
