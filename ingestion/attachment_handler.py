"""
Attachment handler for Excel Q&A ingestion.

Detects image-reference columns in Excel/CSV sheets, then for each cell:
  - URL  → download image, validate MIME, upload to Google Drive (or store locally)
  - filename → match against knowledge/attachments/ directory or DB
  - stores result in knowledge_attachments table

Public API:
    detect_attachment_columns(headers)         → [col_index, ...]
    parse_attachment_cell(cell_value)          → [ref, ...]
    process_excel_attachments(workbook_data, file_id, ...) → report dict
    get_attachments_for_file(sb, file_id)      → [attachment_record, ...]
"""

import mimetypes
import re
import tempfile
import time as _time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import requests as _requests

from storage import get_storage_service

ATTACHMENTS_DIR = Path("knowledge/attachments")
KNOWLEDGE_DIR   = Path("knowledge")

# ── Column detection ──────────────────────────────────────────────
# This regex is a HINT, not a gate — a column matching it also gets
# bare-filename matching (_find_local_file / knowledge_files lookup). Every
# OTHER column in a Q&A row is still scanned for URL-shaped cell values (see
# _scan_row_for_attachments) so customers are never forced to name a column
# one specific way. Kept broad/flexible on purpose.
_ATTACHMENT_PATTERNS = re.compile(
    r"สื่อรูปภาพ|รูปภาพ|image[s]?|attachment[s]?|media|url[s]?|link[s]?|photo[s]?|"
    r"picture[s]?|ไฟล์|file[s]?|clip[s]?",
    re.IGNORECASE,
)

VALID_IMAGE_MIMES = {
    "image/jpeg", "image/jpg", "image/png", "image/gif",
    "image/webp", "image/bmp", "image/svg+xml", "image/tiff",
}
VALID_PDF_MIMES = {"application/pdf"}
VALID_DOC_MIMES = {
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
VALID_SPREADSHEET_MIMES = {
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
}
VALID_PRESENTATION_MIMES = {
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
VALID_ARCHIVE_MIMES = {"application/zip", "application/x-zip-compressed"}
VALID_VIDEO_MIMES = {"video/mp4", "video/quicktime", "video/x-msvideo", "video/webm"}
VALID_TEXT_MIMES = {"text/plain", "application/json", "application/xml", "text/xml"}

IMAGE_EXTS        = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".tiff", ".tif"}
PDF_EXTS          = {".pdf"}
DOC_EXTS          = {".doc", ".docx"}
SPREADSHEET_EXTS  = {".xls", ".xlsx", ".csv"}
PRESENTATION_EXTS = {".ppt", ".pptx"}
ARCHIVE_EXTS      = {".zip"}
VIDEO_EXTS        = {".mp4", ".mov", ".avi", ".webm"}
TEXT_EXTS         = {".txt", ".json", ".xml"}
ALL_KNOWN_EXTS = (IMAGE_EXTS | PDF_EXTS | DOC_EXTS | SPREADSHEET_EXTS |
                  PRESENTATION_EXTS | ARCHIVE_EXTS | VIDEO_EXTS | TEXT_EXTS)

DOWNLOAD_MAX_RETRIES = 3


def _classify_attachment_type(content_type: str, magic_guess: Optional[str], ext: str) -> str:
    """Content-Type header -> magic bytes -> extension, in that priority —
    except a generic/uninformative signal (application/octet-stream, or
    the bare "application/zip" that EVERY zip-based Office format shares)
    is skipped in favor of the next signal, since trusting it would call
    every .docx/.xlsx/.pptx a generic "archive"."""
    def _from(guess: Optional[str]) -> Optional[str]:
        g = (guess or "").lower()
        if not g or g in ("application/octet-stream", "application/zip"):
            return None
        if g.startswith("image/"): return "image"
        if g == "application/pdf": return "pdf"
        if g in VALID_DOC_MIMES: return "document"
        if g in VALID_SPREADSHEET_MIMES: return "spreadsheet"
        if g in VALID_PRESENTATION_MIMES: return "presentation"
        if g in VALID_ARCHIVE_MIMES: return "archive"
        if g in VALID_VIDEO_MIMES: return "video"
        if g in VALID_TEXT_MIMES: return "text"
        return None

    for guess in (content_type, magic_guess):
        result = _from(guess)
        if result:
            return result

    ext = (ext or "").lower()
    if ext in IMAGE_EXTS: return "image"
    if ext in PDF_EXTS: return "pdf"
    if ext in DOC_EXTS: return "document"
    if ext in SPREADSHEET_EXTS: return "spreadsheet"
    if ext in PRESENTATION_EXTS: return "presentation"
    if ext in ARCHIVE_EXTS: return "archive"
    if ext in VIDEO_EXTS: return "video"
    if ext in TEXT_EXTS: return "text"

    if (content_type or "").lower() == "application/zip" or (magic_guess or "") == "application/zip":
        return "archive"
    return "file"


def _sniff_magic_bytes(data: bytes) -> Optional[str]:
    """Best-guess MIME type from the first bytes of a downloaded file.
    Covers only the formats this importer cares about — not a general
    file-type library (avoids adding a new dependency; Content-Type and
    extension cover the rest via _classify_attachment_type's fallback
    chain)."""
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:4] == b"PK\x03\x04":
        # Shared by docx/xlsx/pptx/zip — extension resolves the ambiguity
        # in _classify_attachment_type's fallback chain.
        return "application/zip"
    if data[:4] == b"\xd0\xcf\x11\xe0":
        # Legacy OLE container — shared by .doc/.xls/.ppt.
        return "application/msword"
    if len(data) > 11 and data[4:8] == b"ftyp":
        return "video/mp4"
    return None


def detect_attachment_columns(headers: List[str]) -> List[int]:
    """Return 0-based column indices whose header name matches attachment patterns."""
    return [i for i, h in enumerate(headers) if _ATTACHMENT_PATTERNS.search(str(h))]


_ATTACHMENT_SPLIT_RE = re.compile(r"[\n\r,;|]+")


def parse_attachment_cell(cell_value: str) -> List[str]:
    """Parse one cell value into individual attachment references.

    Splits on newline, comma, semicolon, or pipe. Strips whitespace and
    drops empty entries.
    """
    if not cell_value or not str(cell_value).strip():
        return []
    parts: List[str] = []
    for chunk in _ATTACHMENT_SPLIT_RE.split(str(cell_value)):
        v = chunk.strip()
        if v:
            parts.append(v)
    return parts


# ── Storage helpers ───────────────────────────────────────────────

def _is_url(value: str) -> bool:
    """True only if the ENTIRE string is a well-formed http(s) URL."""
    try:
        r = urlparse(value.strip())
        return r.scheme in ("http", "https") and bool(r.netloc)
    except Exception:
        return False


# Matches an http(s) URL anywhere inside a larger string — newline-,
# comma-, semicolon-, pipe-, and space-separated, or embedded mid-sentence
# ("Please see https://x.com/a.png for reference"). Stops at whitespace or
# the separator characters above, since none of those are ever legal
# inside an http(s) URL for our purposes.
_INLINE_URL_RE = re.compile(r"https?://[^\s,;|]+", re.IGNORECASE)
_TRAILING_PUNCT_RE = re.compile(r"[.,;:!?)\]}\"'>]+$")


def extract_urls_from_text(text: str) -> List[str]:
    """Find every URL anywhere in a cell's text — not just cells that ARE
    entirely one URL. Handles a URL on its own line inside a longer note,
    multiple URLs separated by any of newline/comma/semicolon/pipe/space,
    and a URL embedded mid-sentence. Trailing punctuation picked up by the
    regex (a period ending a sentence, a closing parenthesis, etc.) is
    stripped so "...see https://x.com/a.png." doesn't include the dot."""
    if not text:
        return []
    urls = []
    for m in _INLINE_URL_RE.finditer(str(text)):
        url = _TRAILING_PUNCT_RE.sub("", m.group(0))
        if url and _is_url(url) and url not in urls:
            urls.append(url)
    return urls


def _looks_like_filename(token: str) -> bool:
    """A bare (non-URL) token that ends in a recognized extension — used to
    let flexible (non-attachment-header) columns still catch plain
    filenames like "warehouse.png" typed without a full path/URL, without
    treating arbitrary text as a filename reference."""
    token = (token or "").strip()
    if not token or len(token) > 120:
        return False
    return Path(token).suffix.lower() in ALL_KNOWN_EXTS


def find_attachment_refs_in_cell(cell_value, is_known_attachment_header: bool) -> List[Tuple[str, bool]]:
    """The single place that decides "is this cell worth treating as an
    attachment reference, and what are the individual refs" — used by BOTH
    the real importer (process_excel_attachments) and the Import Preview
    dry-run analyzer (ingestion/import_preview.py), so the two can never
    drift into detecting different things for the same file (exactly the
    kind of divergence that caused a real bug earlier in this codebase).

    Returns [(ref, is_url), ...] — is_url distinguishes a URL (always a
    candidate) from a bare filename (only a candidate for known
    attachment-header columns, or when it has a recognized extension).
    """
    if not cell_value:
        return []
    cell_str = str(cell_value)
    refs: List[Tuple[str, bool]] = []

    urls = extract_urls_from_text(cell_str)
    for url in urls:
        refs.append((url, True))

    residual = cell_str
    for url in urls:
        residual = residual.replace(url, " ")
    for token in parse_attachment_cell(residual):
        if is_known_attachment_header or _looks_like_filename(token):
            refs.append((token, False))

    return refs


_FILENAME_NORMALIZE_SUFFIX_RE = re.compile(
    r"[\s_-]*(\(\d+\)|final|copy|v\d+|copy\d*)$", re.IGNORECASE)


def _normalize_filename_for_match(name: str) -> str:
    """Fold cosmetic filename variants together for matching: case,
    whitespace/underscore/dash differences, and common suffixes like
    " (1)", "-final", "_copy", "v2" — so "Warehouse (1).PNG" and
    "warehouse-final.png" both match a stored "warehouse.png"."""
    stem, ext = Path(name).stem, Path(name).suffix.lower()
    prev = None
    while prev != stem:
        prev = stem
        stem = _FILENAME_NORMALIZE_SUFFIX_RE.sub("", stem).strip()
    stem = re.sub(r"[\s_-]+", "", stem).lower()
    return f"{stem}{ext}"


_GDRIVE_HOST_RE = re.compile(r"(?:^|\.)(?:drive|docs)\.google\.com$", re.IGNORECASE)
_GDRIVE_FILE_ID_PATH_RE = re.compile(r"/file/d/([a-zA-Z0-9_-]+)")


def _extract_gdrive_file_id(url: str) -> Optional[str]:
    """Recognizes:
      - https://drive.google.com/file/d/<FILE_ID>/view
      - https://drive.google.com/open?id=<FILE_ID>
      - https://drive.google.com/uc?id=<FILE_ID>
    Returns None for anything else (including non-Drive URLs)."""
    try:
        p = urlparse(url)
        if not _GDRIVE_HOST_RE.search(p.netloc):
            return None
        m = _GDRIVE_FILE_ID_PATH_RE.search(p.path)
        if m:
            return m.group(1)
        qs = parse_qs(p.query)
        if qs.get("id"):
            return qs["id"][0]
        return None
    except Exception:
        return None


def _gdrive_direct_download_url(file_id: str) -> str:
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def is_attachment_url(value: str) -> bool:
    """Public helper: does this cell value, taken as a whole, look like
    something we should try to download (any http(s) URL — image, PDF,
    Google Drive share link, or otherwise)? Content-type is the real
    authority; this is just "is it worth attempting a download"."""
    return _is_url(str(value))


def _guess_mime(path: Path) -> str:
    mt, _ = mimetypes.guess_type(str(path))
    return mt or "application/octet-stream"


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str) -> str:
    """Sanitize an arbitrary (possibly attacker- or Excel-user-supplied)
    filename/URL-derived name into something safe to place on disk and in
    a public URL: ASCII alnum/dot/dash/underscore only, short, and
    collision-resistant via a random prefix."""
    name = (name or "").strip() or "file"
    stem, ext = Path(name).stem, Path(name).suffix
    safe_stem = _SAFE_NAME_RE.sub("_", stem)[:80].strip("_") or "file"
    safe_ext = _SAFE_NAME_RE.sub("", ext)[:10]
    return f"{uuid.uuid4().hex[:10]}_{safe_stem}{safe_ext}"


def preview_attachment_url(url: str) -> Dict:
    """Validate a URL WITHOUT downloading its body — used by the Import
    Preview step (ingestion/import_preview.py), which must never write to
    storage or the database before the user confirms. Resolves Google
    Drive share links the same way _download_to_temp does, then opens a
    streamed request and reads only the response headers before closing
    the connection (works for servers that don't support HEAD, e.g. some
    Drive endpoints — a real HEAD is tried first since it's cheaper).

    Returns:
        {"url", "reachable", "content_type", "content_length",
         "attachment_type", "gdrive_file_id", "error"}
    """
    gdrive_id = _extract_gdrive_file_id(url)
    fetch_url = _gdrive_direct_download_url(gdrive_id) if gdrive_id else url
    out = {
        "url": url, "reachable": False, "content_type": None,
        "content_length": None, "attachment_type": "file",
        "gdrive_file_id": gdrive_id, "error": None,
    }
    try:
        resp = _requests.head(fetch_url, timeout=10, allow_redirects=True,
                               headers={"User-Agent": "ShipifyBot/1.0"})
        if resp.status_code >= 400 or not resp.headers.get("content-type"):
            # Some servers (Drive included) don't implement HEAD properly —
            # fall back to a streamed GET, closed immediately after headers.
            resp = _requests.get(fetch_url, timeout=10, stream=True,
                                  headers={"User-Agent": "ShipifyBot/1.0"})
            resp.close()
        if resp.status_code >= 400:
            out["error"] = f"HTTP {resp.status_code}"
            return out
        ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if ct.startswith("text/html"):
            out["error"] = (
                "Returns an HTML page, not a file — Drive link may be private "
                "or need the large-file confirmation." if gdrive_id else
                "URL returns an HTML page, not a direct file link."
            )
            return out
        cl = resp.headers.get("content-length")
        ext = Path(urlparse(url).path).suffix.lower()
        out.update({
            "reachable": True,
            "content_type": ct or None,
            "content_length": int(cl) if cl and cl.isdigit() else None,
            "attachment_type": _classify_attachment_type(ct, None, ext),
        })
        return out
    except Exception as exc:
        out["error"] = str(exc)
        return out


def _download_to_temp(url: str) -> Tuple[Optional[Path], str, Optional[str], Optional[str], dict]:
    """Download a URL to a temp file (NOT the final storage location — the
    active StorageService decides where the file actually ends up).

    Transparently rewrites recognized Google Drive share links to their
    direct-download form first. Retries up to DOWNLOAD_MAX_RETRIES times
    on any network/HTTP error before giving up.

    File TYPE is never a reason to reject a download — images, PDFs,
    Office documents, archives, video, and anything else all download
    successfully and get classified via _classify_attachment_type
    (Content-Type -> magic bytes -> extension). The one content-based
    rejection is an HTML response where a real file was expected (Drive's
    "can't scan this file for viruses" / permission-denied interstitial
    page instead of the actual file — a common failure mode for share
    links that aren't set to "Anyone with the link").

    Returns (temp_path, mime, original_filename, error, extra_meta) where
    extra_meta carries {"gdrive_file_id": ...} when applicable, for the
    caller to stash in knowledge_attachments.metadata.
    """
    extra_meta: dict = {}
    fetch_url = url
    gdrive_id = _extract_gdrive_file_id(url)
    if gdrive_id:
        fetch_url = _gdrive_direct_download_url(gdrive_id)
        extra_meta["gdrive_file_id"] = gdrive_id
        extra_meta["original_url"] = url

    last_error = "Download failed"
    for attempt in range(1, DOWNLOAD_MAX_RETRIES + 1):
        try:
            resp = _requests.get(fetch_url, timeout=20, stream=True, headers={
                "User-Agent": "ShipifyBot/1.0",
            })
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()

            if ct.startswith("text/html"):
                reason = (
                    "Google Drive returned an HTML page instead of the file — "
                    "the link is likely private or needs the large-file virus-scan "
                    "confirmation. Share the file as \"Anyone with the link\" and retry."
                    if gdrive_id else
                    "URL returned an HTML page instead of a file — check the link is a "
                    "direct file URL, not a webpage."
                )
                # Not retryable — the server will keep returning the same
                # page every time (it's a permission/config issue, not a
                # transient failure).
                return None, "", None, reason, extra_meta

            tmp_dir = Path(tempfile.gettempdir()) / "shipify_attachments_incoming"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            dest = tmp_dir / f"{uuid.uuid4().hex[:8]}_download.tmp"

            first_bytes = b""
            with open(dest, "wb") as fh:
                for i, block in enumerate(resp.iter_content(8192)):
                    if i == 0:
                        first_bytes = block[:32]
                    fh.write(block)

            magic_guess = _sniff_magic_bytes(first_bytes)
            url_path  = urlparse(url).path
            orig_name = Path(url_path).name or ""
            ext = Path(orig_name).suffix.lower()

            resolved_type = _classify_attachment_type(ct, magic_guess, ext)
            resolved_mime = ct or magic_guess or _guess_mime(dest) or "application/octet-stream"

            if not orig_name or orig_name in (".", "/") or "." not in orig_name:
                guessed_ext = ext or mimetypes.guess_extension(resolved_mime) or ".bin"
                orig_name = f"file_{uuid.uuid4().hex[:8]}{guessed_ext}"

            renamed = dest.with_name(f"{dest.stem}_{orig_name}")
            dest.rename(renamed)
            extra_meta["resolved_attachment_type"] = resolved_type
            return renamed, resolved_mime, orig_name, None, extra_meta

        except Exception as exc:
            last_error = str(exc)
            if attempt < DOWNLOAD_MAX_RETRIES:
                _time.sleep(0.5 * attempt)
                continue

    return None, "", None, f"Failed after {DOWNLOAD_MAX_RETRIES} attempts: {last_error}", extra_meta


def _find_local_file(filename: str) -> Optional[Path]:
    """Search for a filename in common local directories. Tries an exact
    (case-insensitive) match first, then a normalized match so
    "Warehouse (1).PNG" / "warehouse-final.png" / "warehouse_copy.png" all
    resolve to a stored "warehouse.png"."""
    fname_lower = filename.lower()
    target_norm = _normalize_filename_for_match(filename)
    normalized_candidates = []
    for search_dir in (ATTACHMENTS_DIR, KNOWLEDGE_DIR):
        if not search_dir.exists():
            continue
        for f in search_dir.iterdir():
            if f.name.lower() == fname_lower:
                return f
            if _normalize_filename_for_match(f.name) == target_norm:
                normalized_candidates.append(f)
    return normalized_candidates[0] if normalized_candidates else None


# ── Single attachment processing ──────────────────────────────────

_THUMBNAIL_MAX_SIZE = (480, 480)


def _generate_preview(source_path: Path, attachment_type: str, storage, yyyy: str, mm: str
                       ) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort preview generation. Returns (preview_url, storage_path)
    or (None, None) if no preview applies/could be generated — this is
    never a failure condition for the attachment itself.

      image -> a downscaled JPEG thumbnail (Pillow)
      pdf   -> a JPEG render of page 1 (PyMuPDF, already a dependency for
               PDF text extraction elsewhere in this codebase)
      video, office documents, archives, text -> no per-file preview
               generated (would need ffmpeg / office-conversion tooling
               this deployment doesn't have); the frontend shows a
               generic icon for these based on attachment_type instead.
    """
    if attachment_type not in ("image", "pdf"):
        return None, None

    tmp_dir = Path(tempfile.gettempdir()) / "shipify_attachments_incoming"
    thumb_path = tmp_dir / f"thumb_{uuid.uuid4().hex[:10]}.jpg"

    try:
        if attachment_type == "image":
            from PIL import Image
            with Image.open(source_path) as im:
                im = im.convert("RGB")
                im.thumbnail(_THUMBNAIL_MAX_SIZE)
                im.save(thumb_path, "JPEG", quality=80)
        else:  # pdf
            import fitz  # PyMuPDF
            doc = fitz.open(source_path)
            try:
                if doc.page_count == 0:
                    return None, None
                page = doc.load_page(0)
                pix = page.get_pixmap(matrix=fitz.Matrix(0.6, 0.6))
                pix.save(str(thumb_path))
            finally:
                doc.close()

        if not thumb_path.exists():
            return None, None

        stored = storage.upload_file(thumb_path, dest_name=thumb_path.name,
                                      folder=f"attachments/{yyyy}/{mm}/previews")
        preview_url = stored.public_url
        if stored.provider == "local":
            from config import ATTACHMENT_BASE_URL
            preview_url = f"{ATTACHMENT_BASE_URL}/{yyyy}/{mm}/previews/{thumb_path.name}"
        return preview_url, stored.storage_path
    finally:
        try:
            thumb_path.unlink(missing_ok=True)
        except Exception:
            pass


def _process_one(
    value: str,
    *,
    file_id: str,
    row_index: int,
    sheet_name: str,
    chunk_id: Optional[str],
    knowledge_item_id: Optional[str] = None,
    sb,
) -> Dict:
    """Process one attachment reference string. Returns a knowledge_attachments record.

    Storage placement (local disk / Supabase Storage / Google Drive / ...)
    is entirely delegated to the active StorageService — this function never
    assumes a specific provider.

    knowledge_item_id, when given (Q&A-sheet rows), is the row's own UUID —
    the attachment belongs to THAT row only, never matched by filename.
    """
    storage = get_storage_service()

    rec: Dict = {
        "id":                str(uuid.uuid4()),
        "knowledge_file_id": file_id,
        "knowledge_item_id": knowledge_item_id,
        "chunk_id":          chunk_id,
        "row_index":         row_index,
        "sheet_name":        sheet_name,
        "filename":          value,
        "original_filename": value,
        "original_url":      None,
        "storage_provider":  "local",
        "storage_file_id":   None,
        "storage_path":      None,
        "public_url":        None,
        "mime_type":         None,
        "file_size":         None,
        "checksum":          None,
        "attachment_type":   "image",
        "description":       None,
        "status":            "missing",
        "error_message":     None,
        "metadata":          {},
    }

    # ── URL: download to temp, then hand off to StorageService ──────
    if _is_url(value):
        rec["original_url"] = value
        tmp_path, mime, orig_name, err, extra_meta = _download_to_temp(value)
        resolved_type = extra_meta.pop("resolved_attachment_type", None)
        if extra_meta:
            rec["metadata"] = extra_meta

        if err or not tmp_path:
            rec["status"]        = "failed"
            rec["error_message"] = err or "Download failed"
            return rec

        rec["attachment_type"] = resolved_type or "file"

        safe_name = _safe_filename(orig_name or "file")
        now = datetime.now(timezone.utc)
        yyyy, mm = now.strftime("%Y"), now.strftime("%m")
        try:
            stored = storage.upload_file(tmp_path, dest_name=safe_name, folder=f"attachments/{yyyy}/{mm}")
        except Exception as exc:
            rec["status"]        = "failed"
            rec["error_message"] = f"Storage upload failed: {exc}"
            return rec

        # Best-effort preview/thumbnail — generated from the SAME temp
        # file, before it's deleted below. Never fails the attachment: a
        # thumbnail failure just means no preview_url, not a failed import.
        preview_url, preview_storage_path = None, None
        try:
            preview_url, preview_storage_path = _generate_preview(
                tmp_path, rec["attachment_type"], storage, yyyy, mm)
        except Exception as exc:
            print(f"[attachment_handler] preview generation skipped for {orig_name}: {exc}")
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

        # For the "local" provider, compute the public URL ourselves rather
        # than trust StoredFile.public_url — that value is relative to the
        # storage ROOT, but the route that actually serves these files
        # (/admin/attachments/{path}) is rooted at the "attachments/"
        # subfolder specifically, and doubling that segment would produce a
        # 404ing URL. Every other provider (Supabase/S3/Google Drive)
        # already returns a correct, self-contained public_url.
        public_url = stored.public_url
        if stored.provider == "local":
            from config import ATTACHMENT_BASE_URL
            public_url = f"{ATTACHMENT_BASE_URL}/{yyyy}/{mm}/{safe_name}"

        if preview_url:
            rec["metadata"]["preview_url"] = preview_url
            rec["metadata"]["preview_storage_path"] = preview_storage_path

        rec.update({
            "filename":         safe_name,
            "original_filename": orig_name,
            "mime_type":        stored.mime_type,
            "file_size":        stored.file_size,
            "checksum":         stored.checksum,
            "storage_provider": stored.provider,
            "storage_path":     stored.storage_path,
            "public_url":       public_url,
            "status":           "downloaded",
        })
        return rec

    # ── Filename: match locally or in DB ─────────────────────────
    local = _find_local_file(value)
    if local:
        try:
            stored = storage.upload_file(local, dest_name=local.name, folder="attachments")
            rec.update({
                "filename":         local.name,
                "mime_type":        stored.mime_type,
                "file_size":        stored.file_size,
                "checksum":         stored.checksum,
                "storage_provider": stored.provider,
                "storage_path":     stored.storage_path,
                "public_url":       stored.public_url,
                "status":           "linked",
            })
        except Exception as exc:
            rec["status"]        = "failed"
            rec["error_message"] = f"Storage upload failed: {exc}"
        return rec

    # Try matching in knowledge_files table by partial filename
    try:
        res = sb.table("knowledge_files")\
            .select("id,filename")\
            .ilike("filename", f"%{value}%")\
            .limit(1).execute()
        if res.data:
            rec["status"]      = "linked"
            rec["description"] = f"Matched knowledge_file: {res.data[0]['filename']}"
            return rec
    except Exception:
        pass

    rec["status"]        = "missing"
    rec["error_message"] = f"Not found locally or in knowledge base: {value!r}"
    return rec


# ── Workbook-level processing ──────────────────────────────────────

def _create_knowledge_items(workbook_data: Dict, file_id: str, sb, page_to_chunk: Dict[int, str]) -> Dict[tuple, str]:
    """Create one knowledge_items row per Q&A-sheet row. Returns
    {(sheet_name, row_index): knowledge_item_id} so attachments (and
    callers generally) can link to the exact row by UUID — never by
    filename, never by re-deriving position from scratch."""
    from ingestion.excel_extractor import is_qa_sheet, detect_qa_columns

    item_ids: Dict[tuple, str] = {}
    records = []
    for sheet in workbook_data.get("sheets", []):
        headers = sheet.get("headers", [])
        if not is_qa_sheet(headers):
            continue
        sname = sheet.get("sheet_name", "")
        qa_cols = detect_qa_columns(headers)

        for row in sheet.get("rows", []):
            page_number = row.get("_page_number")
            if page_number is None:
                continue  # empty question+answer row — no chunk, no item
            rd = row["row_data"]
            item_id = str(uuid.uuid4())
            records.append({
                "id":                item_id,
                "knowledge_file_id": file_id,
                "chunk_id":          page_to_chunk.get(page_number),
                "question":          rd.get(qa_cols["question"]),
                "answer":            rd.get(qa_cols["answer"]),
                "sheet_name":        sname,
                "row_index":         row["row_index"],
                # Populated by excel_extractor.workbook_to_summary_pages —
                # real column values if the sheet had them, generated/
                # inferred otherwise (see ingestion/smart_enrichment.py).
                # .get() defaults keep this safe even if that step didn't
                # run (e.g. an older cached workbook_data shape).
                "category":          row.get("_generated_category"),
                "tags":              row.get("_generated_tags") or [],
                "alt_questions":     row.get("_generated_alt_questions") or [],
                "language":          row.get("_generated_language"),
                "channel":           row.get("_generated_channel"),
            })
            item_ids[(sname, row["row_index"])] = item_id

    if records:
        try:
            # Replace any previous items for this file (re-sync produces a
            # fresh set — old item UUIDs are gone, so their attachments will
            # be re-created against the new UUIDs below).
            sb.table("knowledge_items").delete().eq("knowledge_file_id", file_id).execute()
            BATCH = 100
            for i in range(0, len(records), BATCH):
                sb.table("knowledge_items").insert(records[i:i + BATCH]).execute()
            print(f"[attachment_handler] created {len(records)} knowledge_items for file_id={file_id}")
        except Exception as exc:
            # category/tags/alt_questions/language columns (migration 013)
            # not present yet on this database — fall back to the pre-013
            # column set so item creation (and attachment linking, which
            # depends on these UUIDs existing) still works. Never let an
            # enrichment feature regress the core "1 row = 1 knowledge_item"
            # behavior that predates it.
            print(f"[attachment_handler] knowledge_items insert failed ({exc}); "
                  f"retrying without migration-013 columns")
            try:
                legacy_records = [
                    {k: v for k, v in r.items()
                     if k not in ("category", "tags", "alt_questions", "language")}
                    for r in records
                ]
                for i in range(0, len(legacy_records), BATCH):
                    sb.table("knowledge_items").insert(legacy_records[i:i + BATCH]).execute()
                print(f"[attachment_handler] created {len(legacy_records)} knowledge_items "
                      f"(legacy columns) for file_id={file_id}")
            except Exception as exc2:
                print(f"[attachment_handler] knowledge_items insert failed even without "
                      f"migration-013 columns: {exc2}")
                return {}
    return item_ids


def process_excel_attachments(
    workbook_data: Dict,
    file_id: str,
    sb,
) -> Dict:
    """Process all attachment cells in a workbook — flexibly.

    No fixed sheet name or column names are required. A sheet qualifies as
    Q&A purely by having a detectable Question + Answer column (see
    excel_extractor.is_qa_sheet); for such a sheet, EVERY column other than
    the Question/Answer columns themselves is scanned for cell values that
    are, in their entirety, an http(s) URL (see _is_url) — not just columns
    whose header happens to match a known attachment-related name. Known
    header names (Attachment URLs/Filenames, Image(s), รูปภาพ, URL, Link,
    ...) still additionally get bare-filename matching (_find_local_file /
    knowledge_files lookup), since scanning EVERY column for bare filenames
    (not full URLs) would be too prone to false positives on ordinary text.

    Storage placement is delegated to the configured StorageService
    (see storage/factory.py, STORAGE_PROVIDER env var) — this function no
    longer takes provider-specific arguments.

    For Q&A-style sheets, this also creates one knowledge_items row per
    data row and links attachments to it by UUID — never by filename,
    never globally. Attachments on non-Q&A (data-table) sheets fall back to
    the legacy header-pattern-only scan, linking by chunk_id/file_id.

    Returns import report:
        rows_with_attachments   – rows that had at least one attachment reference
        rows_imported           – Q&A rows turned into knowledge_items (alias of knowledge_items_created)
        urls_detected           – total URL-shaped cell tokens found across all rows
        attachments_linked      – filename matched locally
        attachments_downloaded  – URL downloaded successfully
        attachments_missing     – filename not found
        attachments_failed      – URL download or Drive upload error
        knowledge_items_created – Q&A rows turned into knowledge_items
        attachment_records      – full list of knowledge_attachments dicts
        failed                  – [{"url", "sheet", "row", "error"}] for every failed attachment
    """
    report: Dict = {
        "rows_with_attachments": 0,
        "rows_imported":        0,
        "rows_skipped":         0,
        "urls_detected":        0,
        "attachments_linked":    0,
        "attachments_downloaded": 0,
        "attachments_missing":   0,
        "attachments_failed":    0,
        "knowledge_items_created": 0,
        "attachment_records":    [],
        "failed":                [],
        "generated_tags":           0,
        "generated_alt_questions":  0,
        "generated_categories":     [],
    }

    # page_number → chunk_id, built from the DB (chunk_ids are assigned
    # during upsert_chunks). page_number itself comes straight from
    # row["_page_number"]/sheet["_page_number"], set by
    # excel_extractor.workbook_to_summary_pages — the single source of
    # truth for "which page/chunk represents this row or sheet."
    page_to_chunk: Dict[int, str] = {}
    try:
        res = sb.table("knowledge_chunks")\
            .select("id,metadata")\
            .eq("file_id", file_id)\
            .eq("is_active", True)\
            .execute()
        for row in (res.data or []):
            meta = row.get("metadata") or {}
            pn = meta.get("page_number")
            if pn and pn not in page_to_chunk:
                page_to_chunk[int(pn)] = row["id"]
    except Exception as exc:
        print(f"[attachment_handler] chunk lookup failed: {exc}")

    item_ids = _create_knowledge_items(workbook_data, file_id, sb, page_to_chunk)
    report["knowledge_items_created"] = len(item_ids)
    report["rows_imported"] = len(item_ids)

    from ingestion.excel_extractor import is_qa_sheet as _is_qa_sheet_stats
    categories_seen = set()
    for sheet in workbook_data.get("sheets", []):
        if not _is_qa_sheet_stats(sheet.get("headers", [])):
            continue
        for row in sheet.get("rows", []):
            if row.get("_page_number") is None:
                report["rows_skipped"] += 1
                continue
            report["generated_tags"] += len(row.get("_generated_tags") or [])
            report["generated_alt_questions"] += len(row.get("_generated_alt_questions") or [])
            cat = row.get("_generated_category")
            if cat:
                categories_seen.add(cat)
    report["generated_categories"] = sorted(categories_seen)

    for sheet in workbook_data.get("sheets", []):
        headers = sheet.get("headers", [])
        rows    = sheet.get("rows", [])
        sname   = sheet.get("sheet_name", "")

        from ingestion.excel_extractor import is_qa_sheet, detect_qa_columns
        qa_sheet = is_qa_sheet(headers)
        qa_cols = detect_qa_columns(headers) if qa_sheet else {}
        skip_headers = {qa_cols.get("question"), qa_cols.get("answer")} if qa_sheet else set()

        att_indices = detect_attachment_columns(headers)
        att_headers = {headers[i] for i in att_indices}

        if qa_sheet:
            # Flexible mode: every column except Question/Answer is a
            # candidate — a cell only counts if the ENTIRE value is a URL
            # (see _is_url), so free-text answers mentioning a URL in
            # passing are never mistaken for an attachment cell. Known
            # attachment-header columns ADDITIONALLY get bare-filename
            # matching, same as before.
            scan_headers = [h for h in headers if h not in skip_headers]
        elif att_headers:
            # Non-Q&A (data-table) sheet — unchanged legacy behavior: only
            # scan columns whose header matches a known attachment pattern.
            scan_headers = [h for h in headers if h in att_headers]
        else:
            continue

        for row in rows:
            ri       = row.get("row_index", 0)
            row_data = row.get("row_data", {})

            page_number     = row.get("_page_number") or sheet.get("_page_number")
            chunk_id        = page_to_chunk.get(page_number) if page_number else None
            knowledge_item_id = item_ids.get((sname, ri))

            row_had_attachment = False
            seen_refs = set()  # a value appearing in two scanned columns of the same row is only processed once

            def _handle_ref(ref: str, is_url: bool):
                nonlocal row_had_attachment
                if ref in seen_refs:
                    return
                seen_refs.add(ref)
                if is_url:
                    report["urls_detected"] += 1
                row_had_attachment = True
                rec = _process_one(
                    ref, file_id=file_id, row_index=ri, sheet_name=sname,
                    chunk_id=chunk_id, knowledge_item_id=knowledge_item_id, sb=sb,
                )
                status = rec["status"]
                if status == "linked":
                    report["attachments_linked"] += 1
                elif status == "downloaded":
                    report["attachments_downloaded"] += 1
                elif status == "missing":
                    report["attachments_missing"] += 1
                else:
                    report["attachments_failed"] += 1
                    report["failed"].append({
                        "url": ref, "sheet": sname, "row": ri,
                        "error": rec.get("error_message") or "Unknown error",
                    })
                report["attachment_records"].append(rec)

            for h in scan_headers:
                cell = row_data.get(h)
                if not cell:
                    continue
                for ref, is_url in find_attachment_refs_in_cell(cell, h in att_headers):
                    _handle_ref(ref, is_url=is_url)

            if row_had_attachment:
                report["rows_with_attachments"] += 1

    # Bulk-insert records into Supabase
    records = report["attachment_records"]
    if records:
        # Remove old attachments for this file first
        try:
            sb.table("knowledge_attachments").delete()\
                .eq("knowledge_file_id", file_id).execute()
        except Exception:
            pass
        try:
            BATCH = 100
            for i in range(0, len(records), BATCH):
                sb.table("knowledge_attachments").insert(records[i:i + BATCH]).execute()
            print(f"[attachment_handler] stored {len(records)} attachments for file_id={file_id}")
        except Exception as exc:
            print(f"[attachment_handler] DB insert failed: {exc}")

    return report


# ── Query helper for RAG / LINE ────────────────────────────────────

def get_attachments_for_file(sb, file_id: str) -> List[Dict]:
    """Fetch all active (non-missing) attachments for a knowledge file."""
    try:
        res = sb.table("knowledge_attachments")\
            .select("id,filename,public_url,storage_path,storage_provider,mime_type,attachment_type,metadata,status,row_index,sheet_name,chunk_id")\
            .eq("knowledge_file_id", file_id)\
            .is_("deleted_at", "null")\
            .neq("status", "missing")\
            .execute()
        return res.data or []
    except Exception as exc:
        print(f"[attachment_handler] get_attachments_for_file failed: {exc}")
        return []


def get_attachments_for_chunks(sb, chunk_ids: List[str]) -> Dict[str, List[Dict]]:
    """Fetch attachments keyed by chunk_id for a list of chunk UUIDs.

    Covers both link paths: attachments with chunk_id set directly, AND
    attachments linked via knowledge_item_id where that knowledge_item's
    own chunk_id is one of these chunks (the Q&A-row path — an attachment
    belongs to its row's knowledge_item, and the row's knowledge_item
    belongs to the chunk that represents it).
    """
    if not chunk_ids:
        return {}
    mapping: Dict[str, List[Dict]] = {}
    ids_str = "(" + ",".join(chunk_ids) + ")"
    try:
        res = sb.table("knowledge_attachments")\
            .select("chunk_id,filename,public_url,storage_path,storage_provider,mime_type,attachment_type,metadata,status")\
            .filter("chunk_id", "in", ids_str)\
            .is_("deleted_at", "null")\
            .neq("status", "missing")\
            .execute()
        for row in (res.data or []):
            mapping.setdefault(row["chunk_id"], []).append(row)
    except Exception as exc:
        print(f"[attachment_handler] get_attachments_for_chunks (direct) failed: {exc}")

    try:
        items_res = sb.table("knowledge_items").select("id,chunk_id") \
            .filter("chunk_id", "in", ids_str).is_("deleted_at", "null").execute()
        item_to_chunk = {i["id"]: i["chunk_id"] for i in (items_res.data or [])}
        if item_to_chunk:
            item_ids_str = "(" + ",".join(item_to_chunk.keys()) + ")"
            att_res = sb.table("knowledge_attachments")\
                .select("knowledge_item_id,filename,public_url,storage_path,storage_provider,mime_type,attachment_type,metadata,status")\
                .filter("knowledge_item_id", "in", item_ids_str)\
                .is_("deleted_at", "null")\
                .neq("status", "missing")\
                .execute()
            for row in (att_res.data or []):
                cid = item_to_chunk.get(row["knowledge_item_id"])
                if cid:
                    mapping.setdefault(cid, []).append(row)
    except Exception as exc:
        print(f"[attachment_handler] get_attachments_for_chunks (via knowledge_item) failed: {exc}")

    return mapping
