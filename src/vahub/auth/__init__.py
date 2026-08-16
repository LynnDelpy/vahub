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

from .passwords import hash_password, needs_rehash, verify_password

__all__ = ["hash_password", "needs_rehash", "verify_password"]
