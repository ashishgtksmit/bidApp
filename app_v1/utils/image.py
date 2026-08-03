import base64
import binascii
import io
import pathlib
import re
import urllib.parse
import os
import hmac
import hashlib
import uuid
from PIL import Image
from typing import Any, List, Optional, Set, Dict, Tuple
from datetime import datetime, time, timedelta, timezone
from urllib.parse import unquote, urlencode

import httpx

    
def upload_image(
    base64_image: str,                 # Base64 image string (with or without data URI)
    base_dir: str,                     # Absolute folder path where we'll save the image
    file_stem: str,                    # File name (without extension)
    base_url: str,                     # Base URL to build a public link
    allowed_extensions: Set[str] = {"jpg", "jpeg", "png", "gif", "webp"},
    max_size_mb: int = 10
) -> Dict[str, str]:
    """
    Upload an image from Base64 and return its public URL.

    Returns:
        Dict with 'message' and optional 'url' or 'error'
    """

    # 1) Strip Data URI if present (be tolerant of variants)
    base64_image = re.sub(r'^data:image/[^;]+;base64,', '', base64_image, count=1, flags=re.IGNORECASE)

    # 2) Quick preflight size check (optional but helps avoid huge decodes)
    #    Base64 payload size ≈ 4/3 of raw bytes. Raw bytes ≈ len(b64)*0.75
    approx_raw_size = int(len(base64_image) * 0.75)
    if approx_raw_size > max_size_mb * 1024 * 1024:
        return {"message": "ERROR_IMAGE_TOO_LARGE"}

    # 3) Decode Base64
    try:
        image_data = base64.b64decode(base64_image, validate=True)
    except (binascii.Error, ValueError):
        return {"message": "INVALID_IMAGE_DATA"}

    # 4) Validate final size
    if len(image_data) > max_size_mb * 1024 * 1024:
        return {"message": "ERROR_IMAGE_TOO_LARGE"}

    # 5) Open image and validate/derive extension
    try:
        with Image.open(io.BytesIO(image_data)) as img:
            # Verify image integrity
            img.verify()

            # PIL gives a format like 'JPEG', 'PNG', 'GIF', 'WEBP'
            fmt = (img.format or "").lower()
            format_to_ext = {
                "jpeg": "jpg",
                "png": "png",
                "gif": "gif",
                "webp": "webp",
            }
            ext = format_to_ext.get(fmt)
            if not ext or ext not in allowed_extensions:
                return {"message": "ERROR_INVALID_IMAGE_FORMAT"}
    except Exception as e:
        return {"message": "ERROR_INVALID_IMAGE", "error": str(e)}

    # 6) Sanitize file stem (and ensure it's not empty)
    safe_stem = re.sub(r'[^A-Za-z0-9_\-\.]', '', file_stem.replace(' ', '_'))
    if not safe_stem or safe_stem in {".", ".."}:
        return {"message": "ERROR_INVALID_FILE_NAME"}

    # 7) Ensure directory exists (only create if missing; handle race conditions)
    try:
        # Prefer not to force absolute root unless you intend it.
        # If you *do* intend absolute, keep base_dir as-is.
        path = pathlib.Path(base_dir).expanduser()

        if path.exists():
            if not path.is_dir():
                return {"message": "ERROR_PATH_NOT_DIR"}
            # Check writability: need write + execute on the dir
            if not (os.access(path, os.W_OK) and os.access(path, os.X_OK)):
                return {"message": "ERROR_DIR_NOT_WRITABLE"}
        else:
            # Attempt to create; if FS is read-only you'll get Errno 30 here
            path.mkdir(mode=0o755, parents=True, exist_ok=False)

    except PermissionError as e:
        return {"message": "ERROR_PERMISSION_DENIED", "error": str(e)}
    except OSError as e:
        # Surface read-only FS explicitly
        if e.errno == 30:  # Read-only file system
            return {"message": "ERROR_READ_ONLY_FILESYSTEM", "error": str(e)}
        return {"message": "ERROR_CANNOT_CREATE_DIR", "error": str(e)}

    # 8) Avoid overwriting: if file exists, add numeric suffix
    def ensure_unique(stem: str, extension: str, directory: pathlib.Path) -> pathlib.Path:
        candidate = directory / f"{stem}.{extension}"
        if not candidate.exists():
            return candidate
        i = 1
        while True:
            candidate = directory / f"{stem}-{i}.{extension}"
            if not candidate.exists():
                return candidate
            i += 1

    file_path = ensure_unique(safe_stem, ext, path)

    # 9) Save file atomically where possible
    try:
        # Re-open to load actual image data after .verify() step
        with Image.open(io.BytesIO(image_data)) as img:
            # Convert to a consistent mode only if needed (optional)
            # e.g., for JPEG ensure "RGB"; keep as-is otherwise
            save_kwargs = {}
            if ext == "jpg" and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(file_path, quality=95, **save_kwargs)
    except Exception as e:
        return {"message": "ERROR_SAVING_FILE", "error": str(e)}

    # 10) Build public URL
    public_name = urllib.parse.quote(file_path.name)
    public_url = f"{base_url.rstrip('/')}/{public_name}"

    return {"message": "UPLOADED", "url": public_url, "filename": file_path.name}

def generate_vendor_document_sas(blob_path: str) -> str:
    account_name = os.getenv("AZURE_ACCOUNT_NAME", "")
    account_key = os.getenv("AZURE_ACCOUNT_KEY", "")
    container = os.getenv("AZURE_CONTAINER", "vendor-documents")

    if not account_name or not account_key or not container:
        raise ValueError("Azure blob configuration is missing")

    return generate_azure_blob_sas(
        blob_path=blob_path,
        account_name=account_name,
        account_key=account_key,
        container=container,
    )

def normalize_blob_path(blob_path: Any, account_name: str, container: str) -> str:
    """
    Normalize Azure blob input into a path relative to the container.

    Accepted inputs:
    - dict with {"blobPath": "..."}
    - "vendor-documents/8637/file.jpg"
    - "/vendor-documents/8637/file.jpg"
    - "https://<account>.blob.core.windows.net/vendor-documents/8637/file.jpg"
    - "8637/file.jpg"

    Returns:
    - "8637/file.jpg"
    """
    if isinstance(blob_path, dict):
        blob_path = blob_path.get("blobPath", "")

    blob_path = str(blob_path or "").strip()

    if not blob_path:
        return ""

    prefix = f"https://{account_name}.blob.core.windows.net/{container}/"
    if blob_path.startswith(prefix):
        blob_path = blob_path[len(prefix):]

    blob_path = blob_path.lstrip("/")

    container_prefix = f"{container}/"
    while blob_path.startswith(container_prefix):
        blob_path = blob_path[len(container_prefix):]

    return urllib.parse.unquote(blob_path)
  

def generate_azure_blob_sas(
    blob_path: Any,
    account_name: str,
    account_key: str,
    container: str,
    expiry_minutes: int = 5,
    start_skew_seconds: int = 60,
    sas_version: str = "2023-11-03",
) -> str:
    """
    Generate a read-only SAS URL for a single blob.

    The returned URL is valid for a short time and is suitable for temporary reads.
    """

    normalized_blob_path = normalize_blob_path(blob_path, account_name, container)
    if not normalized_blob_path:
        raise ValueError("Invalid blob path")
    
    sp = "r"          # permissions: read
    sr = "b"          # resource: blob
    spr = "https"

    now_utc = datetime.now(timezone.utc)
    start = (now_utc - timedelta(seconds=start_skew_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    expiry = (now_utc + timedelta(minutes=expiry_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    canonical_resource = f"/blob/{account_name}/{container}/{normalized_blob_path}"

    signed_identifier = ""
    signed_ip = ""
    signed_snapshot_time = ""
    signed_encryption_scope = ""
    rscc = rscd = rsce = rscl = rsct = ""

    string_to_sign = (
        f"{sp}\n"
        f"{start}\n"
        f"{expiry}\n"
        f"{canonical_resource}\n"
        f"{signed_identifier}\n"
        f"{signed_ip}\n"
        f"{spr}\n"
        f"{sas_version}\n"
        f"{sr}\n"
        f"{signed_snapshot_time}\n"
        f"{signed_encryption_scope}\n"
        f"{rscc}\n"
        f"{rscd}\n"
        f"{rsce}\n"
        f"{rscl}\n"
        f"{rsct}"
    )

    decoded_key = base64.b64decode(account_key)
    signature = base64.b64encode(
        hmac.new(decoded_key, string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    query = urlencode(
        {
            "sp": sp,
            "st": start,
            "se": expiry,
            "spr": spr,
            "sv": sas_version,
            "sr": sr,
            "sig": signature,
        }
    )

    return (
        f"https://{account_name}.blob.core.windows.net/"
        f"{container}/{normalized_blob_path}?{query}"
    )


def _sanitize_blob_name(blob_name: str) -> str:
    """
    Keep folder separators, sanitize each segment.
    """
    blob_name = str(blob_name or "").strip().replace("\\", "/")
    blob_name = re.sub(r"/+", "/", blob_name).strip("/")

    segments = []
    for segment in blob_name.split("/"):
        safe = re.sub(r"[^A-Za-z0-9._\- ]", "", segment).strip()
        safe = safe.replace(" ", "_")
        if safe and safe not in {".", ".."}:
            segments.append(safe)

    if not segments:
        raise ValueError("Invalid blob name")

    return "/".join(segments)


def _detect_image_type(
    image_bytes: bytes,
    mime_from_header: Optional[str] = None,
    allowed_mimes: Optional[Set[str]] = None,
) -> Tuple[str, str]:
    """
    Returns (mime, ext)
    """
    if allowed_mimes is None:
        allowed_mimes = {
            "image/jpeg",
            "image/jpg",
            "image/png",
            "image/webp",
            "image/gif",
        }

    mime = mime_from_header.lower() if mime_from_header else None

    if not mime:
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.verify()
                fmt = (img.format or "").lower()
        except Exception:
            raise ValueError("INVALID_IMAGE")

        format_to_mime_ext = {
            "jpeg": ("image/jpeg", "jpg"),
            "jpg": ("image/jpeg", "jpg"),
            "png": ("image/png", "png"),
            "webp": ("image/webp", "webp"),
            "gif": ("image/gif", "gif"),
        }

        if fmt not in format_to_mime_ext:
            raise ValueError("INVALID_IMAGE")

        mime, ext = format_to_mime_ext[fmt]
    else:
        mime_to_ext = {
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
        }
        ext = mime_to_ext.get(mime)
        if not ext:
            raise ValueError("UNSUPPORTED_IMAGE_TYPE")

    if mime not in allowed_mimes:
        raise ValueError("UNSUPPORTED_IMAGE_TYPE")

    return mime, ext


def azure_blob_upload(
    blob_name: str,
    base64_data: str,
    make_public: bool = False,
    max_upload_bytes: int = 20 * 1024 * 1024,
) -> Tuple[bool, str]:
    """
    Upload a base64 image to Azure Blob Storage.

    Env required:
    - AZURE_VENDOR_SAS
    - AZURE_VENDOR_CONTAINER_URL
    - optionally AZURE_PUBLIC_READ_SAS
    """
    sas_token = os.getenv("AZURE_VENDOR_SAS", "").strip()
    base_url = os.getenv("AZURE_VENDOR_CONTAINER_URL", "").strip()
    public_read_sas = os.getenv("AZURE_PUBLIC_READ_SAS", "").strip()

    if not sas_token or not base_url:
        return False, "AZURE_CONFIG_MISSING"

    mime_from_header = None

    header_match = re.match(
        r"^data:(image/[a-zA-Z0-9.+-]+);base64,",
        base64_data,
        flags=re.IGNORECASE,
    )
    if header_match:
        mime_from_header = header_match.group(1).lower()
        base64_data = base64_data[len(header_match.group(0)):]

    clean_base64 = re.sub(r"\s+", "", base64_data or "")
    if not clean_base64:
        return False, "INVALID_BASE64"

    try:
        image_bytes = base64.b64decode(clean_base64, validate=True)
    except (binascii.Error, ValueError):
        return False, "INVALID_BASE64"

    if not image_bytes:
        return False, "INVALID_BASE64"

    if len(image_bytes) > max_upload_bytes:
        return False, "FILE_TOO_LARGE"

    try:
        mime, ext = _detect_image_type(image_bytes, mime_from_header=mime_from_header)
    except ValueError as e:
        return False, str(e)

    try:
        safe_blob_name = _sanitize_blob_name(blob_name)
    except ValueError as e:
        return False, str(e)

    final_blob = f"{safe_blob_name}.{ext}"

    encoded_path = "/".join(
        urllib.parse.quote(part, safe="") for part in final_blob.split("/")
    )

    upload_url = f"{base_url.rstrip('/')}/{encoded_path}?{sas_token.lstrip('?')}"

    headers = {
        "x-ms-blob-type": "BlockBlob",
        "Content-Type": mime,
    }

    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.put(upload_url, content=image_bytes, headers=headers)
    except Exception as e:
        return False, f"UPLOAD_EXCEPTION: {str(e)}"

    if response.status_code != 201:
        return False, f"UPLOAD_FAIL:{response.status_code} | {response.text}"

    final_url = f"{base_url.rstrip('/')}/{encoded_path}"
    if make_public and public_read_sas:
        final_url = f"{final_url}?{public_read_sas.lstrip('?')}"

    return True, final_url


def azure_blob_delete_by_url(file_url: Optional[str]) -> bool:
    """
    Delete a blob from Azure by its URL.
    Uses AZURE_VENDOR_SAS, which must include delete permission.
    """
    if not file_url:
        return True

    sas_token = os.getenv("AZURE_VENDOR_SAS", "").strip()
    if not sas_token:
        return False

    base_url = file_url.split("?", 1)[0]
    delete_url = f"{base_url}?{sas_token.lstrip('?')}"

    headers = {
        "x-ms-version": "2024-11-04",
    }

    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.delete(delete_url, headers=headers)
    except Exception:
        return False

    return response.status_code in (202, 404)


def mime_to_extension(mime_type: str) -> str:
    mime_map = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        "application/pdf": "pdf",
        "application/msword": "doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.ms-excel": "xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/vnd.ms-powerpoint": "ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
        "text/plain": "txt",
        "text/csv": "csv",
        "application/zip": "zip",
        "application/x-zip-compressed": "zip",
    }
    return mime_map.get(mime_type.lower(), "bin")


def _sanitize_blob_segment(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"[^A-Za-z0-9._\- ]", "", value)
    value = value.replace(" ", "_")
    return value


def azure_blob_upload_base64_file(
    blob_name_without_ext: str,
    base64_file: str,
    container_url: str,
    sas_token: str,
    max_upload_bytes: int = 5 * 1024 * 1024,
    allowed_mime_types: Optional[set[str]] = None,
) -> Tuple[bool, str, Optional[str]]:
    """
    Upload a generic base64 file to Azure Blob.

    Returns:
        (success, message_or_url, mime_type)
    """
    if allowed_mime_types is None:
        allowed_mime_types = {
            "image/jpeg",
            "image/jpg",
            "image/png",
            "image/webp",
            "image/gif",
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-powerpoint",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "text/plain",
            "text/csv",
            "application/zip",
            "application/x-zip-compressed",
        }

    base64_file = str(base64_file or "").strip()
    if ";base64," not in base64_file:
        return False, "INVALID_FILE_FORMAT", None

    parts = base64_file.split(";base64,", 1)
    if len(parts) != 2:
        return False, "INVALID_FILE_FORMAT", None

    mime_info, base64_data = parts

    mime_match = re.match(r"^data:(.+)$", mime_info.strip(), flags=re.IGNORECASE)
    if not mime_match:
        return False, "INVALID_MIME_TYPE", None

    mime_type = mime_match.group(1).strip().lower()
    if mime_type not in allowed_mime_types:
        return False, "UNSUPPORTED_FILE_TYPE", mime_type

    clean_base64 = re.sub(r"\s+", "", base64_data)
    try:
        file_data = base64.b64decode(clean_base64, validate=True)
    except (binascii.Error, ValueError):
        return False, "INVALID_BASE64", mime_type

    if not file_data:
        return False, "INVALID_BASE64", mime_type

    if len(file_data) > max_upload_bytes:
        return False, "FILE_TOO_LARGE", mime_type

    ext = mime_to_extension(mime_type)

    safe_name = _sanitize_blob_segment(blob_name_without_ext)
    if not safe_name:
        return False, "INVALID_FILE_NAME", mime_type

    final_blob_name = f"{safe_name}.{ext}"
    encoded_path = "/".join(
        urllib.parse.quote(part, safe="") for part in final_blob_name.split("/")
    )

    upload_url = f"{container_url.rstrip('/')}/{encoded_path}?{sas_token.lstrip('?')}"

    headers = {
        "x-ms-blob-type": "BlockBlob",
        "Content-Type": mime_type or "application/octet-stream",
    }

    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.put(upload_url, content=file_data, headers=headers)
    except Exception as e:
        return False, f"UPLOAD_EXCEPTION: {str(e)}", mime_type

    if response.status_code != 201:
        return False, f"UPLOAD_FAIL:{response.status_code}", mime_type

    blob_url = f"{container_url.rstrip('/')}/{encoded_path}"
    return True, blob_url, mime_type


def upload_support_docs_to_azure(files: List[str]) -> Dict[str, object]:
    """
    Upload chat support documents to Azure chat-docs container.
    """
    container_url = os.getenv("AZURE_CHAT_DOCS_CONTAINER_URL", "").strip()
    sas_token = os.getenv("AZURE_CHAT_DOCS_SAS", "").strip()

    if not container_url or not sas_token:
        return {"status": "error", "message": "Azure chat docs config missing"}

    uploaded_urls: List[str] = []

    for base64_file in files:
        unique_name = f"{int(time.time())}_{uuid.uuid4().hex}"

        success, result, _mime = azure_blob_upload_base64_file(
            blob_name_without_ext=unique_name,
            base64_file=base64_file,
            container_url=container_url,
            sas_token=sas_token,
            max_upload_bytes=5 * 1024 * 1024,
        )

        if success:
            uploaded_urls.append(result)

    if uploaded_urls:
        return {"status": "success", "urls": uploaded_urls}

    return {"status": "error", "message": "No files uploaded to Azure"}

def upload_vendor_profile_picture_azure(user_app_id: str, base64_data: str) -> dict:
    """
    Azure replacement for profile picture upload.
    Returns old-style dict for smoother migration.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    blob_name = f"{user_app_id}/Profile_{timestamp}"

    success, result = azure_blob_upload(
        blob_name=blob_name,
        base64_data=base64_data,
        make_public=False,
    )

    if not success:
        return {"message": result}

    return {"message": "UPLOADED", "url": result}


# ---------------------------------------------------------------------------
# PR28 — focused chat-media JPEG/PNG validation + Azure chat-docs helpers
# ---------------------------------------------------------------------------

_CHAT_MEDIA_MAX_BYTES = 2 * 1024 * 1024
_CHAT_MEDIA_MAX_PIXELS = 25_000_000
_CHAT_MEDIA_ALLOWED_MIMES = frozenset({"image/jpeg", "image/png"})
_CHAT_MEDIA_FORMATS = {
    "jpeg": ("image/jpeg", "jpg"),
    "png": ("image/png", "png"),
}


class ChatMediaImageError(Exception):
    """Typed image validation failure for chat media."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def detect_chat_media_signature(image_bytes: bytes) -> Optional[str]:
    """Return image/jpeg or image/png from magic bytes, else None."""
    if len(image_bytes) >= 3 and image_bytes[0:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(image_bytes) >= 8 and image_bytes[0:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


def decode_chat_media_payload(raw: str) -> Tuple[bytes, Optional[str]]:
    """
    Strict base64 / data-URI decode for chat media.

    Returns (bytes, claimed_mime_or_None).
    Raises ChatMediaImageError with INVALID_CHAT_MEDIA / UNSUPPORTED_CHAT_MEDIA_TYPE.
    """
    image_str = str(raw or "").strip()
    if not image_str:
        raise ChatMediaImageError("INVALID_CHAT_MEDIA")

    claimed_mime: Optional[str] = None
    header_match = re.match(
        r"^data:(image/[a-zA-Z0-9.+-]+);base64,",
        image_str,
        flags=re.IGNORECASE,
    )
    if header_match:
        claimed_mime = header_match.group(1).lower()
        if claimed_mime == "image/jpg":
            claimed_mime = "image/jpeg"
        image_str = image_str[len(header_match.group(0)) :]
    elif image_str.lower().startswith("data:"):
        raise ChatMediaImageError("INVALID_CHAT_MEDIA")

    if claimed_mime is not None and claimed_mime not in _CHAT_MEDIA_ALLOWED_MIMES:
        raise ChatMediaImageError("UNSUPPORTED_CHAT_MEDIA_TYPE")

    clean = re.sub(r"\s+", "", image_str)
    if not clean:
        raise ChatMediaImageError("INVALID_CHAT_MEDIA")

    try:
        binary = base64.b64decode(clean, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ChatMediaImageError("INVALID_CHAT_MEDIA") from exc

    if not binary:
        raise ChatMediaImageError("INVALID_CHAT_MEDIA")

    return binary, claimed_mime


def validate_chat_media_image_bytes(
    binary: bytes,
    claimed_mime: Optional[str] = None,
) -> Tuple[str, str]:
    """
    PR23-style JPEG/PNG validation. Returns (mime, ext).

    Does not recompress. Client MIME is a hint only; bytes are authoritative.
    """
    if not binary:
        raise ChatMediaImageError("INVALID_CHAT_MEDIA")

    if len(binary) > _CHAT_MEDIA_MAX_BYTES:
        raise ChatMediaImageError("CHAT_MEDIA_TOO_LARGE")

    if claimed_mime is not None and claimed_mime not in _CHAT_MEDIA_ALLOWED_MIMES:
        raise ChatMediaImageError("UNSUPPORTED_CHAT_MEDIA_TYPE")

    sig_mime = detect_chat_media_signature(binary)
    if sig_mime is None:
        raise ChatMediaImageError("UNSUPPORTED_CHAT_MEDIA_TYPE")

    previous_max = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = _CHAT_MEDIA_MAX_PIXELS
    try:
        try:
            with Image.open(io.BytesIO(binary)) as img:
                img.verify()
                fmt = (img.format or "").lower()
        except Image.DecompressionBombError as exc:
            raise ChatMediaImageError("INVALID_CHAT_MEDIA") from exc
        except Exception as exc:
            raise ChatMediaImageError("INVALID_CHAT_MEDIA") from exc

        try:
            with Image.open(io.BytesIO(binary)) as img:
                img.load()
                fmt = (img.format or fmt or "").lower()
        except Image.DecompressionBombError as exc:
            raise ChatMediaImageError("INVALID_CHAT_MEDIA") from exc
        except Exception as exc:
            raise ChatMediaImageError("INVALID_CHAT_MEDIA") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_max

    if fmt not in _CHAT_MEDIA_FORMATS:
        raise ChatMediaImageError("UNSUPPORTED_CHAT_MEDIA_TYPE")

    mime, ext = _CHAT_MEDIA_FORMATS[fmt]
    if sig_mime != mime:
        raise ChatMediaImageError("INVALID_CHAT_MEDIA")
    if claimed_mime is not None and claimed_mime != mime:
        raise ChatMediaImageError("INVALID_CHAT_MEDIA")

    return mime, ext


def _chat_docs_credentials() -> Tuple[str, str]:
    container_url = os.getenv("AZURE_CHAT_DOCS_CONTAINER_URL", "").strip()
    sas_token = os.getenv("AZURE_CHAT_DOCS_SAS", "").strip()
    return container_url, sas_token


def chat_docs_blob_public_url(relative_blob_path: str) -> str:
    """SAS-less durable public URL for a chat-docs relative path."""
    container_url, _ = _chat_docs_credentials()
    encoded_path = "/".join(
        urllib.parse.quote(part, safe="") for part in relative_blob_path.split("/")
    )
    return f"{container_url.rstrip('/')}/{encoded_path}"


class ChatDocsStorageError(Exception):
    """Provider failure for chat-docs Azure operations (safe; no provider text)."""


class ChatDocsNotFound(Exception):
    """Blob missing on chat-docs container."""


def chat_docs_head_metadata(relative_blob_path: str) -> Optional[Dict[str, str]]:
    """
    HEAD blob and return lowercase metadata map, or None if not found.

    Raises ChatDocsStorageError on provider/config failure.
    """
    container_url, sas_token = _chat_docs_credentials()
    if not container_url or not sas_token:
        raise ChatDocsStorageError("AZURE_CONFIG_MISSING")

    encoded_path = "/".join(
        urllib.parse.quote(part, safe="") for part in relative_blob_path.split("/")
    )
    head_url = f"{container_url.rstrip('/')}/{encoded_path}?{sas_token.lstrip('?')}"
    headers = {"x-ms-version": "2024-11-04"}

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.head(head_url, headers=headers)
    except Exception as exc:
        raise ChatDocsStorageError("HEAD_FAILED") from exc

    if response.status_code == 404:
        return None
    if response.status_code not in (200, 206):
        raise ChatDocsStorageError("HEAD_FAILED")

    meta: Dict[str, str] = {}
    for key, value in response.headers.items():
        lower = key.lower()
        if lower.startswith("x-ms-meta-"):
            meta[lower[len("x-ms-meta-") :]] = str(value)
    return meta


def chat_docs_upload_bytes(
    *,
    relative_blob_path: str,
    content: bytes,
    content_type: str,
    metadata: Dict[str, str],
) -> str:
    """
    Upload validated bytes to chat-docs with metadata. Returns public URL.

    Raises ChatDocsStorageError on failure. Never returns SAS.
    """
    container_url, sas_token = _chat_docs_credentials()
    if not container_url or not sas_token:
        raise ChatDocsStorageError("AZURE_CONFIG_MISSING")

    encoded_path = "/".join(
        urllib.parse.quote(part, safe="") for part in relative_blob_path.split("/")
    )
    upload_url = f"{container_url.rstrip('/')}/{encoded_path}?{sas_token.lstrip('?')}"

    headers = {
        "x-ms-blob-type": "BlockBlob",
        "x-ms-version": "2024-11-04",
        "Content-Type": content_type,
    }
    for meta_key, meta_val in metadata.items():
        safe_key = re.sub(r"[^a-z0-9_]", "", str(meta_key).lower())
        if not safe_key:
            continue
        headers[f"x-ms-meta-{safe_key}"] = str(meta_val)[:256]

    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.put(upload_url, content=content, headers=headers)
    except Exception as exc:
        raise ChatDocsStorageError("UPLOAD_FAILED") from exc

    # 201 created; 200 may occur on overwrite — PR28 avoids overwrite via conflict check.
    if response.status_code not in (200, 201):
        raise ChatDocsStorageError("UPLOAD_FAILED")

    return f"{container_url.rstrip('/')}/{encoded_path}"


def chat_docs_delete_blob(relative_blob_path: str) -> None:
    """
    Delete deterministic chat-docs blob. Missing blob is success.

    Raises ChatDocsStorageError on provider/config failure.
    """
    container_url, sas_token = _chat_docs_credentials()
    if not container_url or not sas_token:
        raise ChatDocsStorageError("AZURE_CONFIG_MISSING")

    encoded_path = "/".join(
        urllib.parse.quote(part, safe="") for part in relative_blob_path.split("/")
    )
    delete_url = f"{container_url.rstrip('/')}/{encoded_path}?{sas_token.lstrip('?')}"
    headers = {"x-ms-version": "2024-11-04"}

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.delete(delete_url, headers=headers)
    except Exception as exc:
        raise ChatDocsStorageError("DELETE_FAILED") from exc

    if response.status_code in (202, 404):
        return
    raise ChatDocsStorageError("DELETE_FAILED")