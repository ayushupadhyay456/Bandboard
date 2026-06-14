"""
utils/storage.py
────────────────
Cloud storage helpers for BandBoard.

Supports AWS S3 (default).  Swap ``_get_client()`` for a GCS client if needed.

Allowed file types
──────────────────
  Audio : mp3, wav, flac, ogg, m4a
  Video : mp4, mov, webm, avi
  Docs  : pdf
  Images: jpg, jpeg, png, webp

Max size: configurable via MAX_UPLOAD_SIZE env var (default 50 MB).
"""

import io
import logging
import mimetypes
import os
import uuid
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

BUCKET          = os.environ.get("AWS_S3_BUCKET",    "bandboard-media")
REGION          = os.environ.get("AWS_S3_REGION",    "us-east-1")
MAX_SIZE        = int(os.environ.get("MAX_UPLOAD_SIZE", 52_428_800))  # 50 MB

ALLOWED_MIME_TYPES = {
    # Audio
    "audio/mpeg",  "audio/wav",  "audio/flac",
    "audio/ogg",   "audio/mp4",  "audio/x-m4a",
    # Video
    "video/mp4",   "video/quicktime", "video/webm", "video/x-msvideo",
    # Documents
    "application/pdf",
    # Images
    "image/jpeg",  "image/png",  "image/webp",
}

ALLOWED_EXTENSIONS = {
    "mp3", "wav", "flac", "ogg", "m4a",
    "mp4", "mov", "webm", "avi",
    "pdf",
    "jpg", "jpeg", "png", "webp",
}


def _get_client():
    """Return a boto3 S3 client. Returns None if boto3/credentials are missing."""
    try:
        import boto3
        return boto3.client(
            "s3",
            region_name=REGION,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        )
    except ImportError:
        log.warning("boto3 not installed – cloud storage disabled")
        return None
    except Exception as exc:
        log.error("Failed to create S3 client: %s", exc)
        return None


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def validate_upload(filename: str, filesize: int) -> Tuple[bool, str]:
    """
    Returns (True, '') if the file is acceptable, or (False, reason) if not.
    """
    ext = _extension(filename)
    if ext not in ALLOWED_EXTENSIONS:
        return False, f"File type '.{ext}' is not allowed."
    if filesize > MAX_SIZE:
        mb = MAX_SIZE // (1024 * 1024)
        return False, f"File exceeds the {mb} MB size limit."
    return True, ""


def upload_file(
    file_obj,
    filename: str,
    folder: str = "uploads",
    public: bool = False,
) -> Optional[dict]:
    """
    Upload *file_obj* (a file-like object) to S3.

    Returns a dict with:
        storage_key  – S3 object key
        storage_url  – HTTPS URL to the object
        mime_type    – detected MIME type
        file_size    – size in bytes

    Returns None on failure.
    """
    s3 = _get_client()
    if s3 is None:
        return None

    # Read & measure
    data = file_obj.read()
    file_size = len(data)

    ok, reason = validate_upload(filename, file_size)
    if not ok:
        log.warning("upload_file rejected '%s': %s", filename, reason)
        return None

    # Detect MIME
    mime, _ = mimetypes.guess_type(filename)
    if mime not in ALLOWED_MIME_TYPES:
        mime = "application/octet-stream"

    # Build unique key
    ext = _extension(filename)
    storage_key = f"{folder}/{uuid.uuid4().hex}.{ext}"

    extra_args = {"ContentType": mime}
    if public:
        extra_args["ACL"] = "public-read"

    try:
        s3.upload_fileobj(
            io.BytesIO(data),
            BUCKET,
            storage_key,
            ExtraArgs=extra_args,
        )
        storage_url = f"https://{BUCKET}.s3.{REGION}.amazonaws.com/{storage_key}"
        return {
            "storage_key": storage_key,
            "storage_url": storage_url,
            "mime_type":   mime,
            "file_size":   file_size,
        }
    except Exception as exc:
        log.error("S3 upload failed: %s", exc)
        return None


def delete_file(storage_key: str) -> bool:
    """Delete an object from S3. Returns True on success."""
    s3 = _get_client()
    if s3 is None:
        return False
    try:
        s3.delete_object(Bucket=BUCKET, Key=storage_key)
        return True
    except Exception as exc:
        log.error("S3 delete failed: %s", exc)
        return False


def get_presigned_url(storage_key: str, expires_in: int = 3600) -> Optional[str]:
    """
    Generate a temporary pre-signed URL for a private S3 object.
    Useful for private demo reels that shouldn't be publicly accessible.
    """
    s3 = _get_client()
    if s3 is None:
        return None
    try:
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": storage_key},
            ExpiresIn=expires_in,
        )
    except Exception as exc:
        log.error("Presigned URL generation failed: %s", exc)
        return None
