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
import archive
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


_LOGO_RECT = (66.61, 29.47, 97.66, 60.51)


def _overlay_tenant_logo(page, snapshot):
    """Replace the baked-in SFW logo with the tenant's uploaded logo.
    No-op for the default tenant, when no logo is uploaded, or when the
    page has no logo image."""
    r2_key = (snapshot.get("brand_logo_r2_key") or "").strip()
    if not r2_key:
        return
    logo_bytes, _ctype = archive.fetch_logo_bytes(r2_key)
    if not logo_bytes:
        return

    import fitz

    target_xref = None
    for img in page.get_images(full=True):
        xref = img[0]
        for r in page.get_image_rects(xref):
            if (abs(r.x0 - _LOGO_RECT[0]) < 1.0
                    and abs(r.y0 - _LOGO_RECT[1]) < 1.0
                    and abs(r.x1 - _LOGO_RECT[2]) < 1.0
                    and abs(r.y1 - _LOGO_RECT[3]) < 1.0):
                target_xref = xref
                break
        if target_xref is not None:
            break

    if target_xref is None:
        return

    try:
        page.delete_image(target_xref)
    except Exception:
        pass

    rect = fitz.Rect(*_LOGO_RECT)
    page.insert_image(rect, stream=logo_bytes)


def _open_template_doc(snapshot):
    """Open the effective template as a fitz document.

    Prefers a tenant-specific override stored in R2
    (snapshot["template_r2_key"]); falls back to the bundled on-disk
    PDF. Importing fitz here keeps water_bill import-safe when PyMuPDF
    is not installed (the caller raises a clear error).
    """
    import fitz

    key = (snapshot.get("template_r2_key") or "").strip()
    if key.startswith("templates/"):
        data, _ctype = archive.download_object(key)
        if data:
            return fitz.open(stream=data, filetype="pdf")

    if not os.path.exists(TEMPLATE_PATH):
        raise RuntimeError(f"Water bill template not found: {TEMPLATE_PATH}")
    return fitz.open(TEMPLATE_PATH)


def generate_water_bill_pdf(snapshot: dict) -> BytesIO:
    """Fill the water bill template and return a BytesIO."""
    try:
        import fitz  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "PyMuPDF (fitz) is required for water bill generation. "
            "Install with: pip install pymupdf"
        ) from e

    doc = _open_template_doc(snapshot)
    page = doc[0]

    placeholders = _find_placeholders(page)
    pairs = [(ph, _scalar_value(snapshot, ph["name"])) for ph in placeholders]
    _apply_replacements(page, pairs)

    _overlay_tenant_logo(page, snapshot)

    out = BytesIO()
    doc.save(out, garbage=4, deflate=True, clean=True)
    doc.close()
    out.seek(0)
    return out
