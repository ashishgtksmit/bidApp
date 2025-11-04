# project_root/utils/security.py
from passlib.context import CryptContext
from passlib.exc import UnknownHashError

# 1) First try bcrypt_sha256 (no 72-byte limit), keep bcrypt as fallback for legacy hashes
#    The bcrypt__truncate_error=False prevents hard errors if any legacy flow still hits bcrypt.
pwd_context = CryptContext(
    schemes=["bcrypt_sha256", "bcrypt"],
    deprecated="auto",
    bcrypt__truncate_error=False,
)

def hash_password(plain: str) -> str:
    # New hashes will be bcrypt_sha256 by default
    return pwd_context.hash(plain)

def verify_password(plain: str, hashed: str) -> bool:
    """
    Verify against either bcrypt_sha256 or bcrypt.
    If you still have some rows that are PLAINTEXT (pre-migration), this will fail;
    see verify_and_update_password() below to handle that gracefully.
    """
    try:
        return pwd_context.verify(plain, hashed)
    except (ValueError, UnknownHashError):
        # Optional: temporary fallback for truly legacy PLAINTEXT rows (remove once migrated)
        return plain == hashed

def verify_and_update_password(plain: str, hashed: str):
    """
    Verify and, if the stored hash uses a deprecated scheme (e.g., bcrypt),
    return a fresh bcrypt_sha256 hash so caller can persist it.
    """
    try:
        ok, new_hash = pwd_context.verify_and_update(plain, hashed)
        print(ok)
        print(new_hash)
        return ok, new_hash
    except (ValueError, UnknownHashError):
        # Legacy plaintext fallback – ONLY if you knowingly had plaintext stored.
        if plain == hashed:
            # Force-upgrade to secure hash
            return True, hash_password(plain)
        return False, None