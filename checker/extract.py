"""Pull text out of an uploaded agreement: PDF, Word (.docx), photo/scan, or .txt.

Everything runs locally. Photos and scanned PDFs are read with on-device OCR
(RapidOCR), so personal data is still hidden before any AI call.
"""
import io
from functools import lru_cache

import pymupdf

MAX_CHARS = 60_000  # roughly 20+ pages; longer files are cut and the user is told
MAX_OCR_PAGES = 20  # scanned PDFs longer than this are cut (OCR is slow on the free tier)

IMAGE_TYPES = ["jpg", "jpeg", "png", "webp"]
UPLOAD_TYPES = ["pdf", "docx", "doc", "txt", *IMAGE_TYPES]


class UnreadableFile(ValueError):
    """The file can't be read. The message is safe to show to the user."""


def pdf_to_text(data: bytes) -> str:
    """Return the text of a PDF given its bytes. Empty string means no text layer."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        pages = [page.get_text("text") for page in doc]
    return "\n".join(pages).strip()


def looks_scanned(text: str) -> bool:
    """A PDF with almost no extractable text is probably a scan or photo."""
    return len(text.strip()) < 200


def scanned_pdf_to_text(data: bytes) -> str:
    """OCR each page of a scanned PDF (up to MAX_OCR_PAGES)."""
    return pdf_to_text_with_ocr(data)[0]


def pdf_to_text_with_ocr(data: bytes) -> tuple[str, int, list[str]]:
    """Text of every page; pages with (almost) no text layer are read with OCR.

    Handles typed, scanned and mixed PDFs (e.g. a typed agreement with a scanned signature page
    or annexure). Returns (text, pages_ocrd, notes for the user).
    """
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        raise UnreadableFile("We couldn't open this PDF. It may be damaged.") from e
    with doc:
        if doc.needs_pass:
            raise UnreadableFile("This PDF is password-protected. Open it, save a copy without a password, "
                                 "and upload that.")
        pages, ocr_count, skipped, notes = [], 0, 0, []
        for page in doc:
            text = page.get_text("text").strip()
            if len(text) < 50 and (page.get_images() or not text):
                if ocr_count >= MAX_OCR_PAGES:
                    skipped += 1
                    continue
                png = page.get_pixmap(dpi=200).tobytes("png")
                ocr_text = image_to_text(png)
                ocr_count += 1
                text = ocr_text if len(ocr_text) > len(text) else text
            pages.append(text)
    if skipped:
        notes.append(f"We read the first {MAX_OCR_PAGES} scanned pages only; {skipped} more were skipped.")
    return "\n\n".join(p for p in pages if p).strip(), ocr_count, notes


def docx_to_text(data: bytes) -> str:
    """Text of a Word .docx: paragraphs and tables, in document order."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as e:
        raise UnreadableFile("We couldn't open this Word file. Try saving it again as .docx or PDF.") from e
    from docx.oxml.ns import qn

    P, TBL = qn("w:p"), qn("w:tbl")
    lines = []
    # Walk the whole body in order, so text inside content controls (common in templates)
    # and text boxes is found too, not only top-level paragraphs.
    for el in document.element.body.iter(P, TBL):
        if any(a.tag == TBL for a in el.iterancestors()):
            continue  # inside a table: the table handles it
        if el.tag == P:
            lines.append(Paragraph(el, document).text)
        else:
            for row in Table(el, document).rows:
                cells = []
                for cell in row.cells:  # merged cells repeat; keep each once
                    if cell.text.strip() and (not cells or cells[-1] != cell.text.strip()):
                        cells.append(cell.text.strip())
                lines.append(" | ".join(cells))
    text = "\n".join(lines).strip()
    if not text:
        raise UnreadableFile("This Word file has no text we can read. If it contains scanned pages, "
                             "save it as PDF and upload that.")
    return text


@lru_cache(maxsize=1)
def _ocr_engine():
    from rapidocr import RapidOCR  # loads small ONNX models once per process
    return RapidOCR()


def image_to_text(data: bytes) -> str:
    """Read the text in a photo or scan, on this machine."""
    import numpy as np
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        img.load()  # surface truncated or corrupt files here, not inside OCR
        img = ImageOps.exif_transpose(img).convert("RGB")  # phone photos are often stored sideways
    except Exception as e:
        raise UnreadableFile("We couldn't open this image. Please upload a JPG or PNG.") from e
    # Very large phone photos slow OCR down without improving it.
    if max(img.size) > 3000:
        img.thumbnail((3000, 3000))
    elif max(img.size) < 1200:  # small screenshots: enlarging helps OCR read small print
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    img = img.rotate(_best_rotation(img), expand=True)
    # use_cls=False: the per-line angle classifier wrongly flips some long lines; page rotation is
    # handled by _best_rotation instead.
    result = _ocr_engine()(np.array(img)[:, :, ::-1], use_cls=False)  # RapidOCR expects BGR
    return _join_lines(result.boxes, result.txts)


def _best_rotation(img) -> int:
    """Find which way up the page is (photos are often sideways or upside down, with no EXIF tag).

    Reads a small copy upright first. Only if the text looks sideways (most text boxes taller
    than wide) or poorly read does it try the other angles, so upright pages cost one quick pass.
    """
    import numpy as np

    def read(angle):
        small = img.rotate(angle, expand=True)
        small.thumbnail((1000, 1000))
        r = _ocr_engine()(np.array(small)[:, :, ::-1], use_cls=False)
        txts, scores = r.txts or (), r.scores or ()
        good = sum(len(t) * s for t, s in zip(txts, scores) if s >= 0.85)
        total = sum(len(t) for t in txts) or 1
        boxes = r.boxes if r.boxes is not None else []
        tall = sum(1 for b in boxes if (max(p[1] for p in b) - min(p[1] for p in b))
                   > 1.5 * (max(p[0] for p in b) - min(p[0] for p in b)))
        return good, good / total, (tall > len(boxes) / 2 if len(boxes) else False)

    score0, confident0, sideways = read(0)
    if not sideways and confident0 >= 0.9 and score0 > 0:
        return 0
    candidates = (90, 270) if sideways else (90, 180, 270)
    best_angle, best_score = 0, score0
    for angle in candidates:
        score = read(angle)[0]
        if score > best_score:
            best_angle, best_score = angle, score
    return best_angle


def _join_lines(boxes, texts) -> str:
    """Put OCR pieces back into reading order: top to bottom, left to right within a line."""
    if boxes is None or texts is None or len(texts) == 0:
        return ""
    items = []
    for box, text in zip(boxes, texts):
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        items.append((min(ys), max(ys), min(xs), text))
    items.sort()
    lines, current, line_bottom = [], [], None
    for top, bottom, left, text in items:
        # Same line if this piece starts above the middle of the current line's last piece.
        if current and top < line_bottom:
            current.append((left, text))
            line_bottom = max(line_bottom, (top + bottom) / 2)
        else:
            if current:
                lines.append(" ".join(t for _, t in sorted(current)))
            current, line_bottom = [(left, text)], (top + bottom) / 2
    if current:
        lines.append(" ".join(t for _, t in sorted(current)))
    return "\n".join(lines)


def decode_text(data: bytes) -> str:
    """Plain text in UTF-8, UTF-16 (Notepad's 'Unicode') or Windows-1252."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def file_to_text(name: str, data: bytes) -> tuple[str, bool]:
    """Return (text, used_ocr) for one uploaded file. Raises UnreadableFile with a user-facing message."""
    text, used_ocr, _ = file_to_text_with_notes(name, data)
    return text, used_ocr


def file_to_text_with_notes(name: str, data: bytes) -> tuple[str, bool, list[str]]:
    """Like file_to_text, plus notes to show the user (e.g. pages skipped)."""
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if not data:
        raise UnreadableFile("This file is empty.")
    if ext == "pdf":
        text, ocr_pages, notes = pdf_to_text_with_ocr(data)
        return text, ocr_pages > 0, notes
    if ext == "docx":
        return docx_to_text(data), False, []
    if ext == "doc":
        raise UnreadableFile("Old Word files (.doc) can't be read. Open it in Word or Google Docs and "
                             "save it as .docx or PDF, then upload that.")
    if ext in IMAGE_TYPES:
        return image_to_text(data), True, []
    if ext == "txt":
        return decode_text(data), False, []
    raise UnreadableFile("Please upload a PDF, Word (.docx), photo (JPG/PNG) or .txt file.")


def clip(text: str) -> tuple[str, bool]:
    """Cut very long text so the request stays fast and cheap."""
    if len(text) <= MAX_CHARS:
        return text, False
    return text[:MAX_CHARS], True
