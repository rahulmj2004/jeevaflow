"""
Isolated document-processing worker.

Run only by worker_client.py as a separate process:

    stdin  -> one JSON job {"op": ..., "data": base64, ...}
    stdout -> one JSON result

Isolation (applied before any untrusted bytes are parsed):
    - no secrets: started with a minimal environment; this module
      never imports app.config, so backend/.env is not loaded
    - no database access and no encryption keys: it only ever sees
      the plaintext of the one document it was handed
    - network egress blocked in-process (DEMO IMPLEMENTATION;
      PRODUCTION REQUIRED: container/VM with no network)
    - core dumps disabled, CPU time limited, refuses to run as root
    - temporary files only inside a private directory that the
      parent deletes after every job; no caches, no debug dumps
    - all PDF/image parsing, OCR and rendering of untrusted content
      happens here, never in the API process

Operations:
    inspect  validate structure, reject active content, sanitise
             (PDF scrub/rebuild, image re-encode without metadata)
    extract  text / OCR, quality gate, rule-based extraction with
             bounding boxes and confidence
    render   one page with the evidence region highlighted, patient
             identity blacked out (text layer or OCR word boxes) and a
             viewer watermark burned in, flattened to one PNG raster.
             Fails closed to a "masking unavailable" placeholder.
"""

import base64
import io
import json
import os
import resource
import socket
import sys
import warnings


MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_SIDE = 12_000
OCR_RENDER_DPI = 200
VIEW_RENDER_DPI = 130
MAX_VIEW_WIDTH = 1400


class Rejected(Exception):
    def __init__(self, code: str, security_scan: dict = None):
        super().__init__(code)
        self.code = code
        self.security_scan = security_scan


# ============================================================
# HARDENING
# ============================================================

def _blocked(*args, **kwargs):
    raise PermissionError("Network access is disabled in the processing worker.")


def harden():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise SystemExit("Refusing to run the processing worker as root.")

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    try:
        resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    except (ValueError, OSError):
        pass

    for name in list(os.environ):
        if name.startswith(("JEEVAFLOW_", "TWILIO_")):
            del os.environ[name]

    socket.socket = _blocked
    socket.create_connection = _blocked
    socket.getaddrinfo = _blocked
    socket.socketpair = _blocked


# ============================================================
# INSPECT + SANITISE
# ============================================================

def _remove_associated_files(document):
    """
    scrub() empties the EmbeddedFiles name tree but leaves /AF arrays
    (PDF 2.0 associated files, e.g. C2PA manifests) on the catalog
    and pages. Delete both, so the rebuild discards the streams and
    the stored copy carries no attachment references at all.
    """

    import pymupdf

    m = pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(document)

    for xref in [page.xref for page in document]:
        m.pdf_dict_dels(m.pdf_load_object(pdf, xref), "AF")

    catalog = m.pdf_load_object(pdf, document.pdf_catalog())
    m.pdf_dict_dels(catalog, "AF")

    names = m.pdf_dict_gets(catalog, "Names")

    if m.pdf_is_dict(names):
        m.pdf_dict_dels(names, "EmbeddedFiles")

        if m.pdf_dict_len(names) == 0:
            m.pdf_dict_dels(catalog, "Names")

    if m.pdf_to_name(m.pdf_dict_gets(catalog, "PageMode")) == "UseAttachments":
        m.pdf_dict_puts(catalog, "PageMode", m.pdf_new_name("UseNone"))


def _inspect_pdf(data: bytes, max_pages: int) -> dict:
    import pymupdf

    from app.security.pdf_active_content import Finding, PdfScanResult, scan_bytes, scan_document

    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        scan = PdfScanResult(False, [Finding("UNPARSEABLE", "PDF could not be opened", "Document")])
        raise Rejected("CORRUPTED_FILE", scan.to_dict())

    try:
        if document.needs_pass or document.is_encrypted:
            raise Rejected("ENCRYPTED_PDF")

        if document.page_count < 1:
            raise Rejected("CORRUPTED_FILE")

        if document.page_count > max_pages:
            raise Rejected("TOO_MANY_PAGES")

        # Structural active-content scan on parsed objects (fails
        # closed on anything it cannot parse).
        scan = scan_document(document)

        if not scan.safe:
            code = "CORRUPTED_FILE" if scan.primary.feature == "UNPARSEABLE" else "SUSPICIOUS_PDF"
            raise Rejected(code, scan.to_dict())

        for page in document:
            rect = page.rect

            if rect.width <= 0 or rect.height <= 0 or rect.width > 15000 or rect.height > 15000:
                raise Rejected("CORRUPTED_FILE")

        # Flatten: drop metadata, links, form fields, thumbnails and
        # unused objects, then rebuild the file from scratch.
        document.scrub(
            attached_files=True,
            clean_pages=False,
            embedded_files=True,
            hidden_text=False,
            javascript=True,
            metadata=True,
            redactions=False,
            remove_links=True,
            reset_fields=True,
            reset_responses=True,
            thumbnails=True,
            xml_metadata=True,
        )
        _remove_associated_files(document)

        sanitized = document.tobytes(garbage=4, deflate=True, no_new_id=True)
        page_count = document.page_count

    finally:
        document.close()

    # The rebuilt copy is what gets stored: it must carry no active
    # content and no attachments at all (not even allowed ones).
    rescan = scan_bytes(sanitized)

    if not rescan.safe or rescan.allowed:
        raise Rejected("SUSPICIOUS_PDF", rescan.to_dict())

    return {
        "sanitized": sanitized,
        "page_count": page_count,
        "width": None,
        "height": None,
        "security_scan": scan.to_dict(),
    }


def _open_image(data: bytes, content_type: str):
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)

    expected = {"image/png": "PNG", "image/jpeg": "JPEG"}[content_type]

    try:
        probe = Image.open(io.BytesIO(data))
        fmt = probe.format
        probe.verify()
        image = Image.open(io.BytesIO(data))
        image.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise Rejected("DECOMPRESSION_BOMB")
    except Exception:
        raise Rejected("CORRUPTED_FILE")

    if fmt != expected:
        raise Rejected("CONTENT_MISMATCH")

    width, height = image.size

    if width < 1 or height < 1 or width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        raise Rejected("INVALID_DIMENSIONS")

    if getattr(image, "n_frames", 1) > 1:
        raise Rejected("ANIMATED_IMAGE")

    return image, expected


def _inspect_image(data: bytes, content_type: str) -> dict:
    from PIL import ImageOps

    image, fmt = _open_image(data, content_type)

    # Re-encode: applies EXIF orientation, then drops EXIF/GPS,
    # ICC, text chunks and any trailing or appended payload.
    image = ImageOps.exif_transpose(image)

    if fmt == "JPEG":
        image = image.convert("RGB")
    elif image.mode not in ("L", "LA", "RGB", "RGBA"):
        image = image.convert("RGBA")

    out = io.BytesIO()

    if fmt == "JPEG":
        image.save(out, format="JPEG", quality=92, optimize=True)
    else:
        image.save(out, format="PNG", optimize=True)

    return {
        "sanitized": out.getvalue(),
        "page_count": 1,
        "width": image.size[0],
        "height": image.size[1],
    }


def op_inspect(job: dict) -> dict:
    data = base64.b64decode(job["data"])
    content_type = job["content_type"]

    if content_type == "application/pdf":
        result = _inspect_pdf(data, int(job.get("max_pages", 30)))
    else:
        result = _inspect_image(data, content_type)

    result["sanitized"] = base64.b64encode(result["sanitized"]).decode()

    return result


# ============================================================
# EXTRACT
# ============================================================

def _norm(token: str) -> str:
    return "".join(ch for ch in token.lower() if ch.isalnum() or ch in "%./")


def _ocr_words(image) -> tuple[str, list[dict], float]:
    import pytesseract

    rgb = image.convert("RGB")
    text = pytesseract.image_to_string(rgb).strip()
    data = pytesseract.image_to_data(rgb, output_type=pytesseract.Output.DICT)

    width, height = rgb.size
    words = []

    for index, raw in enumerate(data["text"]):
        token = (raw or "").strip()

        try:
            conf = float(data["conf"][index])
        except (TypeError, ValueError):
            conf = -1

        if not token or conf < 0:
            continue

        words.append({
            "norm": _norm(token),
            "x": data["left"][index] / width,
            "y": data["top"][index] / height,
            "w": data["width"][index] / width,
            "h": data["height"][index] / height,
            "conf": conf / 100.0,
        })

    mean = sum(word["conf"] for word in words) / len(words) if words else None

    return text, words, mean


def _locate_in_words(quote: str, words: list[dict]):
    tokens = [_norm(token) for token in quote.split()]
    tokens = [token for token in tokens if token]

    if not tokens:
        return None, None

    norms = [word["norm"] for word in words]

    for start in range(0, len(words) - len(tokens) + 1):
        if all(norms[start + k] == tokens[k] for k in range(len(tokens))):
            span = words[start:start + len(tokens)]
            x0 = min(word["x"] for word in span)
            y0 = min(word["y"] for word in span)
            x1 = max(word["x"] + word["w"] for word in span)
            y1 = max(word["y"] + word["h"] for word in span)
            conf = sum(word["conf"] for word in span) / len(span)

            return (x0, y0, x1 - x0, y1 - y0), conf

    return None, None


def _locate(page_info: dict, quote: str, pdf_page=None):
    """
    Bounding box (page fractions) and confidence for one quote.
    """

    if page_info["method"] == "TEXT" and pdf_page is not None:
        first_line = quote.split("\n")[0][:120]
        hits = pdf_page.search_for(first_line) if first_line else []

        if hits:
            rect = pdf_page.rect
            hit = hits[0]
            box = (
                hit.x0 / rect.width,
                hit.y0 / rect.height,
                (hit.x1 - hit.x0) / rect.width,
                (hit.y1 - hit.y0) / rect.height,
            )
            return box, 1.0

        return None, 1.0

    box, conf = _locate_in_words(quote, page_info.get("words", []))

    return box, conf if conf is not None else page_info.get("mean_conf")


def op_extract(job: dict) -> dict:
    from datetime import date

    from app.extraction import (
        extract_commitments_from_pages,
        extract_document_date,
        extract_observations_from_pages,
    )
    from app.facts import apply_confidence, extract_facts
    from app.quality import assess_image

    data = base64.b64decode(job["data"])
    content_type = job["content_type"]

    pdf = None
    quality = None
    pages = []

    if content_type == "application/pdf":
        import pymupdf
        from PIL import Image

        pdf = pymupdf.open(stream=data, filetype="pdf")

        for number, page in enumerate(pdf, start=1):
            text = page.get_text() or ""

            if text.strip():
                pages.append({"page_number": number, "text": text, "method": "TEXT"})
                continue

            pixmap = page.get_pixmap(dpi=OCR_RENDER_DPI)

            with Image.open(io.BytesIO(pixmap.tobytes("png"))) as image:
                text, words, mean = _ocr_words(image)

            pages.append({
                "page_number": number, "text": text, "method": "OCR",
                "words": words, "mean_conf": mean,
            })

        methods = {page["method"] for page in pages}
        method = methods.pop() if len(methods) == 1 else "MIXED"

    else:
        image, _ = _open_image(data, content_type)
        quality = assess_image(image)

        if quality["quality_status"] == "RETAKE":
            return {"quality": quality, "pages": None}

        text, words, mean = _ocr_words(image)
        pages.append({
            "page_number": 1, "text": text, "method": "OCR",
            "words": words, "mean_conf": mean,
        })
        method = "OCR"

    try:
        document_date = extract_document_date(pages)
        reference = document_date or (
            date.fromisoformat(job["received_on"]) if job.get("received_on") else None
        )

        by_number = {page["page_number"]: page for page in pages}

        def locate(item):
            page_info = by_number[item["page_number"]]
            pdf_page = pdf[item["page_number"] - 1] if pdf is not None else None
            box, conf = _locate(page_info, item["quote"], pdf_page)
            item["bbox"] = box
            item["confidence"] = conf
            return item

        observations = [locate(item) for item in extract_observations_from_pages(pages)]
        commitments = [locate(item) for item in extract_commitments_from_pages(pages, reference)]
        facts = [
            apply_confidence(item, locate(item)["confidence"])
            for item in extract_facts(pages)
        ]

    finally:
        if pdf is not None:
            pdf.close()

    for item in commitments:
        if item.get("due_date"):
            item["due_date"] = item["due_date"].isoformat()

    return {
        "quality": quality,
        "extraction_method": method,
        "document_date": document_date.isoformat() if document_date else None,
        "pages": [
            {"page_number": page["page_number"], "text": page["text"], "method": page["method"]}
            for page in pages
        ],
        "observations": observations,
        "commitments": commitments,
        "facts": facts,
    }


# ============================================================
# RENDER (secure evidence viewer)
# ============================================================

class MaskingUnavailable(Exception):
    pass


def _text_lines(pdf_page) -> list[list[tuple]]:
    """
    Words of a PDF text layer grouped into lines, as
    (text, x0, y0, x1, y1) in page fractions.
    """

    rect = pdf_page.rect
    lines: dict = {}

    for x0, y0, x1, y1, word, block, line, _ in pdf_page.get_text("words"):
        lines.setdefault((block, line), []).append(
            (word, x0 / rect.width, y0 / rect.height, x1 / rect.width, y1 / rect.height)
        )

    return list(lines.values())


def _ocr_lines(image) -> list[list[tuple]]:
    import pytesseract

    rgb = image.convert("RGB")
    data = pytesseract.image_to_data(rgb, output_type=pytesseract.Output.DICT)
    width, height = rgb.size
    lines: dict = {}

    for index, raw in enumerate(data["text"]):
        token = (raw or "").strip()

        if not token:
            continue

        x, y = data["left"][index], data["top"][index]
        key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
        lines.setdefault(key, []).append(
            (token, x / width, y / height, (x + data["width"][index]) / width, (y + data["height"][index]) / height)
        )

    if not lines:
        # Nothing recognised: identity cannot be ruled out.
        raise MaskingUnavailable()

    return list(lines.values())


def _mask_boxes(lines: list[list[tuple]], terms: list[str]) -> list[tuple]:
    from app.security.masking import pii_spans

    boxes = []

    for words in lines:
        text, offsets = "", []

        for word in words:
            if text:
                text += " "
            offsets.append((len(text), len(text) + len(word[0])))
            text += word[0]

        for start, end in pii_spans(text, terms):
            for (w_start, w_end), word in zip(offsets, words):
                if w_start < end and w_end > start:
                    boxes.append(word[1:])

    return boxes


def _placeholder(width: int, height: int):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (max(width, 600), max(height, 300)), (60, 60, 60))
    draw = ImageDraw.Draw(image)

    try:
        font = ImageFont.load_default(size=max(18, image.width // 40))
    except TypeError:
        font = ImageFont.load_default()

    draw.text(
        (30, image.height // 2 - 20),
        "Masking unavailable - source view withheld to protect patient identity.",
        fill=(255, 255, 255), font=font,
    )

    return image


def op_render(job: dict) -> dict:
    from PIL import Image, ImageDraw, ImageFont

    data = base64.b64decode(job["data"])
    content_type = job["content_type"]
    page_number = int(job.get("page") or 1)
    terms = [str(term) for term in job.get("mask_terms") or []][:10]

    if content_type == "application/pdf":
        import pymupdf

        pdf = pymupdf.open(stream=data, filetype="pdf")

        try:
            if not 1 <= page_number <= pdf.page_count:
                raise Rejected("PAGE_NOT_FOUND")

            page = pdf[page_number - 1]
            pixmap = page.get_pixmap(dpi=VIEW_RENDER_DPI)
            image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")

            try:
                if (page.get_text() or "").strip():
                    lines = _text_lines(page)
                else:
                    ocr_pixmap = page.get_pixmap(dpi=OCR_RENDER_DPI)

                    with Image.open(io.BytesIO(ocr_pixmap.tobytes("png"))) as ocr_image:
                        lines = _ocr_lines(ocr_image)

                mask_boxes = _mask_boxes(lines, terms)
            except Exception:
                mask_boxes = None
        finally:
            pdf.close()
    else:
        image, _ = _open_image(data, content_type)
        image = image.convert("RGB")

        try:
            mask_boxes = _mask_boxes(_ocr_lines(image), terms)
        except Exception:
            mask_boxes = None

    masked = mask_boxes is not None

    if not masked:
        # Fail closed: never return the unmasked page.
        image = _placeholder(image.width, image.height)
        job["bbox"] = None

    if image.width > MAX_VIEW_WIDTH:
        ratio = MAX_VIEW_WIDTH / image.width
        image = image.resize((MAX_VIEW_WIDTH, int(image.height * ratio)))

    # Solid black boxes drawn into the base raster (no layers).
    black = ImageDraw.Draw(image)

    for x0, y0, x1, y1 in mask_boxes or []:
        black.rectangle(
            (int(x0 * image.width) - 3, int(y0 * image.height) - 3,
             int(x1 * image.width) + 3, int(y1 * image.height) + 3),
            fill=(0, 0, 0),
        )

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    bbox = job.get("bbox")

    if bbox:
        x, y, w, h = bbox
        pad = 6
        box = (
            int(x * image.width) - pad,
            int(y * image.height) - pad,
            int((x + w) * image.width) + pad,
            int((y + h) * image.height) + pad,
        )
        draw.rectangle(box, fill=(255, 196, 0, 70), outline=(214, 120, 0, 255), width=3)

    try:
        font = ImageFont.load_default(size=max(14, image.width // 60))
    except TypeError:
        font = ImageFont.load_default()

    watermark = str(job.get("watermark", ""))[:120]
    tile = " | ".join(watermark.split(" | ")[:2])

    tile_width = int(draw.textlength(tile, font=font)) if tile else 0
    step_x = max(tile_width + 120, 200)
    step_y = max(110, image.height // 7)

    for row, top in enumerate(range(40, image.height - 40, step_y)):
        offset = (row % 2) * (step_x // 2)

        for left in range(-offset, image.width, step_x):
            draw.text((left, top), tile, fill=(200, 0, 0, 55), font=font)

    band = max(28, image.height // 40)
    draw.rectangle((0, image.height - band, image.width, image.height), fill=(20, 32, 43, 210))
    draw.text((10, image.height - band + 6), watermark, fill=(255, 255, 255, 255), font=font)

    composed = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

    out = io.BytesIO()
    composed.save(out, format="PNG")

    return {"png": base64.b64encode(out.getvalue()).decode(), "masked": masked}


OPERATIONS = {"inspect": op_inspect, "extract": op_extract, "render": op_render}


def main():
    harden()

    try:
        job = json.loads(sys.stdin.buffer.read())
        operation = OPERATIONS[job["op"]]
        result = {"ok": True, **operation(job)}
    except Rejected as exc:
        result = {"ok": False, "code": exc.code}

        if exc.security_scan is not None:
            result["security_scan"] = exc.security_scan
    except Exception as exc:
        # Never echo exception text: it can contain document content.
        result = {"ok": False, "code": "WORKER_ERROR", "error_type": type(exc).__name__}

    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
