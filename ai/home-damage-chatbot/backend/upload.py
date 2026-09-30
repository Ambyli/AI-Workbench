"""Secure file upload validator and handler."""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from fastapi import UploadFile, HTTPException

logger = logging.getLogger("chatbot.upload")

UPLOAD_DIR = Path(__file__).parent / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
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

    # Save and attempt EXIF stripping
    saved = False
    try:
        from PIL import Image, ImageOps
        import io

        # Load binary content into PIL Image
        img = Image.open(io.BytesIO(file_content))

        # exif_transpose will rotate the image correctly if it has orientation tags,
        # then we save it without metadata
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass

        # Save without EXIF data
        if detected_ext == ".jpg":
            img.save(target_path, "JPEG", exif=b"")
        elif detected_ext == ".png":
            img.save(target_path, "PNG")
        elif detected_ext == ".gif":
            img.save(target_path, "GIF")
        elif detected_ext == ".webp":
            img.save(target_path, "WEBP", exif=b"")
        saved = True
        logger.info("Successfully stripped EXIF and saved image: %s", secure_filename)
    except Exception as exc:
        logger.warning("PIL metadata strip failed or not installed (%s) - falling back to direct write.", exc)

    if not saved:
        # Fallback to direct safe write
        try:
            with open(target_path, "wb") as f:
                f.write(file_content)
            logger.info("Saved image directly (fallback): %s", secure_filename)
        except OSError:
            raise HTTPException(status_code=500, detail="Failed to write upload file to disk.")

    return secure_filename
