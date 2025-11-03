
# import re
# import base64
# from PIL import Image
# import pathlib
# import urllib.parse
# import io
# from typing import Optional, Tuple

import base64
import binascii
import io
import pathlib
import re
import urllib.parse
from typing import Set, Dict
import os

from PIL import Image

# def upload_image(
#     base64_image: str,   # Base64 image string (text)
#     base_dir: str,       # Folder path where we’ll save the image
#     file_stem: str,      # File name (without extension)
#     base_url: str,       # Base URL to build a public link
#     allowed_extensions: set = {"jpg", "jpeg", "png", "gif", "webp"},
#     max_size_mb: int = 10
# ) -> dict:
    
#     """
#     Upload an image from Base64 and return its public URL.
#     Args:
#         base64_image: Base64-encoded image (with or without data URI)
#         base_dir: Absolute path to store the image
#         file_stem: Base file name (without extension)
#         base_url: Base URL for the public file
#         allowed_extensions: Allowed image extensions
#         max_size_mb: Maximum file size in MB
#     Returns:
#         Dict with message and optional url or error
#     """

#     # Strip Data URI if present
#     if re.match(r'^data:image/\w+;base64,', base64_image):
#         base64_image = re.sub(r'^data:image/\w+;base64,', '', base64_image)
    
#     #Decode Base 64
#     try:
#         image_data = base64.b64decode(base64_image, validate=True)
#     except base64.binascii.Error :
#         return {"message" : "INVALID_IMAGE_DATA"}
    
#     #Validate Data
#     if len(image_data) > max_size_mb * 1024 * 1024:
#         return {"message" : "ERROR_IMAGE_TOO_LARGE"}
    
#     #Detect Extension
#     try : 
#         img = Image.open(io.BytesIO(image_data))
#         mime_to_ext = {
#             "image/jpeg" : "jpg",
#             "image/png" : "png",
#             "image/gif" : "gif",
#             "image/webp" : "webp"
#         }

#         ext = mime_to_ext.get(img.format.lower(),"png")
#         if ext not in allowed_extensions:
#             return {"message":"ERROR_INVALID_IMAGE_FORMAT"}
        
#         #Sanitize file stem
#         file_stem = re.sub(r'[^A-Za-z0-9_\-\.]', '', file_stem.replace(' ', '_'))

#         #Create Directory
#         path = pathlib.Path(base_dir)
#         try:
#             path.mkdir(mode=0o777, parents=False, exist_ok=False)
#         except Exception as e:
#             return {"message":"ERROR_CANNOT_CREATE_DIR","error":str(e)}
        
#         #Save File
#         file_path = path / f"{file_stem}.{ext}"
#         try:
#             with open(file_path, "wb") as f:
#                 f.write(image_data)
#         except Exception as e:
#             print(str(e))
#             return {"message":"ERROR_SAVING_FILE","error":str(e)}
        
#         public_url = f"{base_url.rstrip('/')}/{urllib.parse.quote(file_stem + '.' + ext)}"
#         return {"message":"UPLOADED", "url":public_url}

#     except Exception:
#         return {"message":"ERROR_INAVLID_IMAGE"}
    
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

    # # 7) Ensure directory exists (only create if missing; handle race conditions)
    # try:
    #     path = pathlib.Path(base_dir).resolve(strict=False)
    #     if path.exists() and not path.is_dir():
    #         return {"message": "ERROR_PATH_NOT_DIR"}

    #     if not path.exists():
    #         # Use safer default perms; umask may still reduce these.
    #         path.mkdir(mode=0o755, parents=True, exist_ok=False)
    # except OSError as e:
    #     return {"message": "ERROR_CANNOT_CREATE_DIR", "error": str(e)}

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