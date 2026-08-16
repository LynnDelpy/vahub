"""Password hashing with scrypt from the standard library.

No third-party dependency and no home-made crypto: `hashlib.scrypt` is a memory
hard KDF, which is what a password store needs. The cost parameters are encoded
into the stored string, so they can be raised later without invalidating older
hashes, and `needs_rehash` tells the login flow when to upgrade one on the next
successful sign in.

Format: ``scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>``. Comparison is constant
time, so a wrong password does not leak how much of the hash matched.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# Interactive defaults. n=2**15 with r=8, p=1 needs about 32 MiB of memory per
# hash, which is a meaningful brute-force cost while staying fast enough for a
# login. maxmem is set explicitly because the OpenSSL default rejects this size.
_N = 2**15
_R = 8
_P = 1
_DKLEN = 32
_MAXMEM = 128 * _N * _R * 2  # headroom over the 128*N*r scrypt needs
_SALT_BYTES = 16


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_DKLEN, maxmem=_MAXMEM
    )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def hash_password(password: str) -> str:
    """Return an encoded scrypt hash for a new or changed password."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = _derive(password, salt, _N, _R, _P)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    """Whether `password` matches a stored hash. False on any malformed hash
    rather than raising, so a corrupt row denies access instead of crashing the
    login route."""
    try:
        scheme, n_s, r_s, p_s, salt_s, hash_s = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = _unb64(salt_s)
        expected = _unb64(hash_s)
    except (ValueError, TypeError):
        return False
    try:
        candidate = _derive(password, salt, n, r, p)
    except (ValueError, OverflowError):
        return False
    return hmac.compare_digest(candidate, expected)


def needs_rehash(encoded: str) -> bool:
    """Whether a stored hash uses weaker parameters than the current defaults and
    should be replaced on the next successful login."""
    try:
        scheme, n_s, r_s, p_s, _salt, _hash = encoded.split("$")
    except ValueError:
        return True
    return scheme != "scrypt" or (int(n_s), int(r_s), int(p_s)) != (_N, _R, _P)
