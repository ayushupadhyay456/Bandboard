"""
utils/storage.py
────────────────
All S3 logic for BandBoard in one place.

  • validate_file()  – checks size, extension and the file's real content
                       (magic bytes), not just what the browser claims
  • upload_file()    – validates, then uploads to a PRIVATE S3 bucket and
                       returns the object key (store this key in the DB)
  • presigned_url()  – short-lived download link for a private object

Environment variables (set them in Render):
  S3_BUCKET              name of your bucket
  AWS_REGION             region of your bucket, e.g. ap-south-1
  AWS_ACCESS_KEY_ID      from the IAM user you create
  AWS_SECRET_ACCESS_KEY  from the IAM user you create
"""

import logging
import os
import uuid
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from werkzeug.utils import secure_filename

log = logging.getLogger(__name__)

PRESIGNED_URL_TTL = 300  # seconds (5 minutes)


class StorageError(Exception):
    """Raised with a message that is safe to show to the user."""


# ── What each extension must look like inside ────────────────────────────────
# ext -> (mime type we store on the object, function that checks the header)
def _starts(prefix):
    return lambda head: head.startswith(prefix)


def _mp3(head):
    return head.startswith(b"ID3") or (
        len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0
    )


def _wav(head):
    return head[:4] == b"RIFF" and head[8:12] == b"WAVE"


def _mp4(head):
    return head[4:8] == b"ftyp"


FILE_TYPES = {
    "pdf":  ("application/pdf", _starts(b"%PDF")),
    "png":  ("image/png",       _starts(b"\x89PNG\r\n\x1a\n")),
    "jpg":  ("image/jpeg",      _starts(b"\xff\xd8\xff")),
    "jpeg": ("image/jpeg",      _starts(b"\xff\xd8\xff")),
    "mp3":  ("audio/mpeg",      _mp3),
    "wav":  ("audio/wav",       _wav),
    "m4a":  ("audio/mp4",       _mp4),
    "mp4":  ("video/mp4",       _mp4),
    "webm": ("video/webm",      _starts(b"\x1a\x45\xdf\xa3")),
    "ogg":  ("audio/ogg",       _starts(b"OggS")),
}

MB = 1024 * 1024

# Per-upload-kind rules
SHEET_MUSIC_RULES = {"extensions": {"pdf", "png", "jpg", "jpeg"}, "max_bytes": 10 * MB}
DEMO_RULES = {
    "extensions": {"mp3", "wav", "m4a", "mp4", "webm", "ogg", "pdf"},
    "max_bytes": 50 * MB,
}


# ── S3 client ────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _client():
    """Create the S3 client once. boto3 reads the AWS_* keys from the environment."""
    return boto3.client(
        "s3",
        region_name=os.environ.get("AWS_REGION", "ap-south-1"),
        config=Config(signature_version="s3v4"),
    )


def _bucket():
    name = os.environ.get("S3_BUCKET")
    if not name:
        raise StorageError("File storage is not configured.")
    return name


# ── Validation ───────────────────────────────────────────────────────────────
def validate_file(file_storage, rules):
    """
    Check a Werkzeug FileStorage. Returns (safe_name, extension, mime_type).
    Raises StorageError with a user-friendly message if anything is wrong.
    """
    safe_name = secure_filename(file_storage.filename or "")
    if "." not in safe_name:
        raise StorageError("Please upload a file with a valid extension.")

    ext = safe_name.rsplit(".", 1)[1].lower()
    if ext not in rules["extensions"]:
        allowed = ", ".join(sorted(rules["extensions"]))
        raise StorageError(f"File type .{ext} is not allowed. Allowed: {allowed}.")

    stream = file_storage.stream
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    if size == 0:
        raise StorageError("The uploaded file is empty.")
    if size > rules["max_bytes"]:
        raise StorageError(
            f"File is too large. Maximum size is {rules['max_bytes'] // MB} MB."
        )

    # Check the real content: the first bytes must match the claimed type.
    head = stream.read(16)
    stream.seek(0)
    mime, matches = FILE_TYPES[ext]
    if not matches(head):
        raise StorageError("The file content does not match its extension.")

    return safe_name, ext, mime


# ── Upload / download ────────────────────────────────────────────────────────
def upload_file(file_storage, prefix, rules):
    """
    Validate and upload. Returns the S3 object key, for example
    'auditions/3f9c...e1_sheet.pdf'. Save this key in the database.
    """
    safe_name, _ext, mime = validate_file(file_storage, rules)
    key = f"{prefix}/{uuid.uuid4().hex}_{safe_name}"
    try:
        _client().upload_fileobj(
            file_storage.stream,
            _bucket(),
            key,
            ExtraArgs={"ContentType": mime},
        )
    except (ClientError, BotoCoreError):
        log.exception("S3 upload failed for key %s", key)
        raise StorageError("Upload failed. Please try again.")
    return key


def presigned_url(key, expires=PRESIGNED_URL_TTL):
    """Temporary download link for a private object."""
    filename = key.rsplit("/", 1)[-1].split("_", 1)[-1]
    try:
        return _client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": _bucket(),
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=expires,
        )
    except (ClientError, BotoCoreError):
        log.exception("Could not create presigned URL for %s", key)
        raise StorageError("Could not create the download link.")