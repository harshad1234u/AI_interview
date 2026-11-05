"""Security, cryptographic primitives, and session management.

Implements:
- Standard library scrypt password hashing for legacy compatibility.
- AES-256-GCM authenticated symmetric encryption for refresh tokens with AAD.
- RFC 7636 PKCE code verifier & S256 code challenge generation.
- Tamper-proof sealed OAuth state cookies.
- Opaque session token generation and SHA-256 hashing.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# OWASP scrypt option N=2^14, r=8, p=5 for legacy backwards compatibility
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 5
SALT_BYTES = 16
KEY_BYTES = 32
TOKEN_BYTES = 32
_MAXMEM = 64 * 1024 * 1024


class DecryptionError(Exception):
    """Raised when authenticated decryption or verification fails."""
    pass


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=KEY_BYTES
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64url(s: str) -> bytes:
    padding = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + padding)


def hash_password(password: str) -> str:
    """'scrypt$n$r$p$salt$key', with a fresh random salt for every hash."""
    salt = secrets.token_bytes(SALT_BYTES)
    key = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time comparison. Malformed stored hashes return False safely."""
    try:
        scheme, n, r, p, salt, key = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(key, validate=True)
        actual = _scrypt(password, base64.b64decode(salt, validate=True), int(n), int(r), int(p))
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def new_session_token() -> str:
    """Opaque 32-byte raw token sent exclusively to the client in HttpOnly cookie."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    """Database lookup key (hex SHA-256 digest)."""
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# AES-256-GCM Refresh Token Encryption with AAD
# ---------------------------------------------------------------------------

def _build_session_aad(token_hash: str, auth_user_id: str) -> bytes:
    """Cryptographically binds the ciphertext to the session token and auth user ID."""
    return f"coach_session:{token_hash}:{auth_user_id}".encode("utf-8")


def encrypt_refresh_token(
    refresh_token: str,
    token_hash: str,
    auth_user_id: str,
    key: bytes,
    key_id: str = "k1"
) -> str:
    """
    Encrypts a Supabase refresh token using AES-256-GCM with AAD.
    Format: v1:<key_id>:<base64url(12-byte nonce)>:<base64url(ciphertext + 16-byte tag)>
    """
    nonce = secrets.token_bytes(12)
    aad = _build_session_aad(token_hash, auth_user_id)
    aesgcm = AESGCM(key)
    ciphertext_and_tag = aesgcm.encrypt(nonce, refresh_token.encode("utf-8"), aad)
    return f"v1:{key_id}:{_b64url(nonce)}:{_b64url(ciphertext_and_tag)}"


def decrypt_refresh_token(
    encrypted_data: str,
    token_hash: str,
    auth_user_id: str,
    primary_key: bytes,
    primary_key_id: str = "k1",
    fallback_key: bytes | None = None,
    fallback_key_id: str = "k0"
) -> Tuple[str, bool]:
    """
    Decrypts a refresh token.
    Returns: (plaintext_token, needs_reencryption)
    Raises DecryptionError if payload is malformed or authentication tag fails.
    """
    try:
        parts = encrypted_data.split(":")
        if len(parts) != 4 or parts[0] != "v1":
            raise DecryptionError("Unsupported or malformed ciphertext envelope format")
        
        _, key_id, nonce_b64, cipher_b64 = parts
        nonce = _unb64url(nonce_b64)
        ciphertext_and_tag = _unb64url(cipher_b64)
        
        if key_id == primary_key_id:
            active_key = primary_key
            needs_reencrypt = False
        elif fallback_key and key_id == fallback_key_id:
            active_key = fallback_key
            needs_reencrypt = True
        else:
            raise DecryptionError(f"Unknown or unconfigured key_id '{key_id}'")
        
        aad = _build_session_aad(token_hash, auth_user_id)
        aesgcm = AESGCM(active_key)
        plaintext = aesgcm.decrypt(nonce, ciphertext_and_tag, aad).decode("utf-8")
        return plaintext, needs_reencrypt
    except (InvalidTag, ValueError, Exception) as exc:
        raise DecryptionError("Token decryption or tag authentication failed") from exc


# ---------------------------------------------------------------------------
# PKCE and OAuth State Helpers (RFC 7636)
# ---------------------------------------------------------------------------

def generate_pkce_verifier() -> str:
    """Generates a high-entropy URL-safe PKCE code verifier (64 bytes entropy)."""
    return secrets.token_urlsafe(64)


def generate_pkce_challenge(verifier: str) -> str:
    """Computes code_challenge = base64url(SHA256(ASCII(code_verifier))) per RFC 7636."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64url(digest)


def generate_oauth_state() -> str:
    """Generates a random 32-byte hex state token for CSRF protection."""
    return secrets.token_hex(32)


def seal_oauth_state(state: str, verifier: str, key: bytes) -> str:
    """
    Seals state, PKCE verifier, and creation timestamp into an authenticated AEAD token
    stored in the short-lived HttpOnly `oauth_session_flow` cookie.
    """
    payload = json.dumps({
        "state": state,
        "verifier": verifier,
        "created_at": int(time.time()),
    }).encode("utf-8")
    
    nonce = secrets.token_bytes(12)
    aad = b"oauth_flow_v1"
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, payload, aad)
    return f"{_b64url(nonce)}.{_b64url(ciphertext)}"


def unseal_oauth_state(sealed: str, key: bytes, max_age_seconds: int = 300) -> Tuple[str, str]:
    """
    Unseals and validates the `oauth_session_flow` cookie.
    Returns: (state, verifier)
    Raises DecryptionError if tampered, corrupted, or older than max_age_seconds.
    """
    try:
        parts = sealed.split(".")
        if len(parts) != 2:
            raise DecryptionError("Malformed sealed OAuth cookie")
        nonce = _unb64url(parts[0])
        ciphertext = _unb64url(parts[1])
        aad = b"oauth_flow_v1"
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, aad)
        data = json.loads(plaintext.decode("utf-8"))
        
        now = int(time.time())
        created_at = data.get("created_at", 0)
        if now - created_at > max_age_seconds or now < created_at:
            raise DecryptionError("OAuth flow state expired")
        
        return str(data["state"]), str(data["verifier"])
    except (InvalidTag, ValueError, KeyError, Exception) as exc:
        raise DecryptionError("Invalid, expired, or tampered OAuth state cookie") from exc
