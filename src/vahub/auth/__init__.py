"""Built-in authentication: password hashing, users and sessions.

The hub can require a login of its own rather than relying only on a reverse
proxy. Accounts are named (username plus password) so the audit log can record
which person confirmed an action. Passwords are stored as a salted scrypt hash,
never in the clear, and sessions are opaque random tokens kept in the database
so they can be revoked.

This package holds the password primitives. The user and session records live in
the store, the login flow lives in the web layer, and account management is a
CLI command: the hub never invents a credential for you.
"""

import re

from .passwords import hash_password, needs_rehash, verify_password

# The rules for a valid account, shared by the CLI (`vahub user add`) and the
# web first-run setup, so the two cannot drift into accepting different names or
# password lengths. A username is 2-32 characters: a lowercase letter or digit,
# then lowercase letters, digits, dot, underscore or hyphen.
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,31}$")
MIN_PASSWORD_LEN = 8


def username_error(username: str) -> str | None:
    """None if the username is valid, else a one-line reason it is not."""
    if not USERNAME_RE.match(username):
        return "username must be 2-32 chars: a lowercase letter or digit, then a-z 0-9 . _ -"
    return None


def password_error(password: str) -> str | None:
    """None if the password is acceptable, else a one-line reason it is not."""
    if len(password) < MIN_PASSWORD_LEN:
        return f"password must be at least {MIN_PASSWORD_LEN} characters"
    return None


__all__ = [
    "MIN_PASSWORD_LEN",
    "USERNAME_RE",
    "hash_password",
    "needs_rehash",
    "password_error",
    "username_error",
    "verify_password",
]
