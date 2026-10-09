"""Secure file upload validator and handler."""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from fastapi import UploadFile, HTTPException

from .config import settings

logger = logging.getLogger("chatbot.upload")

UPLOAD_DIR = settings.UPLOAD_DIR  # runtime data (S-M6); DATA_DIR/uploads unless overridden
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
MAX_IMAGE_PIXELS = 25_000_000     # ~25 MP; Pillow's default (~89 MP) allows ~350 MB decodes
ALLOWED_MIME_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

MAGIC_NUMBERS = {
    b"\xFF\xD8\xFF": ".jpg",          # JPEG
    b"\x89PNG\r\n\x1a\n": ".png",      # PNG
    b"GIF87a": ".gif",                # GIF
    b"GIF89a": ".gif",                # GIF
    b"RIFF": ".webp",                 # WebP
}


def validate_and_save_upload(file: UploadFile) -> str:
    """Validate upload file size, MIME type, magic bytes, strip EXIF metadata, and save with a safe name."""
    mime_type = file.content_type
    if mime_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type: {mime_type or 'unknown'}. Only JPG, PNG, GIF, and WebP are allowed."
        )

    # Read first chunk to verify magic bytes
    try:
        first_chunk = file.file.read(8192)
    except Exception:
        raise HTTPException(status_code=400, detail="Unable to read upload file headers.")

    detected_ext = None
    for magic, ext in MAGIC_NUMBERS.items():
        if first_chunk.startswith(magic):
            if magic == b"RIFF":
                if len(first_chunk) >= 12 and first_chunk[8:12] == b"WEBP":
                    detected_ext = ".webp"
                    break
            else:
                detected_ext = ext
                break

    if not detected_ext:
        raise HTTPException(
            status_code=400,
            detail="File signature check failed. The file content does not match a valid image type."
        )

    # Accumulate and check file size
    total_size = len(first_chunk)
    file_content = bytearray(first_chunk)

    while True:
        try:
            chunk = file.file.read(8192)
        except Exception:
            raise HTTPException(status_code=400, detail="Error reading upload file content.")
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum allowed size is {MAX_FILE_SIZE // (1024 * 1024)}MB."
            )
        file_content.extend(chunk)

    # Generate secure, unique filename (neutralizes Path Traversal)
    secure_filename = f"{uuid.uuid4().hex}{detected_ext}"
    target_path = UPLOAD_DIR / secure_filename

    # Re-encode to strip ALL metadata (EXIF/GPS/XMP/text chunks). FAIL CLOSED (S-H3):
    # if the image can't be decoded and re-encoded, the upload is rejected. The old
    # code wrote the original bytes on failure, which could persist GPS metadata.
    try:
        img = _decode_image(bytes(file_content))
        _reencode(img, detected_ext, target_path)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - any decode/encode failure is a bad upload
        target_path.unlink(missing_ok=True)
        logger.warning("Upload rejected: image could not be re-encoded (%s).", type(exc).__name__)
        raise HTTPException(status_code=400, detail="Invalid or unreadable image.")

    logger.info("Upload stored (metadata stripped): %s", secure_filename)
    return secure_filename


def _decode_image(data: bytes):
    """Verify then fully decode, with a pixel-count ceiling against decompression bombs."""
    import io
    import warnings
    from PIL import Image, ImageOps

    with warnings.catch_warnings():
        # Pillow warns (then errors at 2x) above MAX_IMAGE_PIXELS; treat any bomb as an error.
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
        Image.open(io.BytesIO(data)).verify()        # structural check (consumes the object)
        img = Image.open(io.BytesIO(data))
        img.load()
    try:
        img = ImageOps.exif_transpose(img)            # keep orientation, then drop the EXIF
    except Exception:  # noqa: BLE001 - orientation is cosmetic
        logger.debug("exif_transpose skipped")
    return img


def _reencode(img, ext: str, target: Path) -> None:
    """Write a fresh file from pixel data only. No exif/icc/text chunks are carried over."""
    img.info.clear()
    if ext == ".jpg":
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(target, "JPEG", quality=88)
    elif ext == ".png":
        img.save(target, "PNG")
    elif ext == ".gif":
        img.save(target, "GIF")
    elif ext == ".webp":
        img.save(target, "WEBP", quality=88)
    else:  # pragma: no cover - ext comes from the magic-number allow-list
        raise ValueError(f"unsupported extension {ext}")
