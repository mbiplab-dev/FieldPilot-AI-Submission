"""Authentication and role-based authorisation for worker and site-manager accounts.

Stdlib only: `hashlib.scrypt` for passwords, `secrets` for tokens, and server-side storage of a
token *hash* so a leaked database yields no usable sessions. Records live in the shared
`storage.docstore`, so the same code runs on SQLite and PostgreSQL.

The service raises its own `AuthError` subclasses rather than `HTTPException`, keeping it
framework-agnostic; `backend/app.py` translates them to 401/403.
"""

from fieldpilot.auth.service import (
    SESSIONS_TABLE,
    USERS_TABLE,
    AuthError,
    AuthService,
    DuplicateUser,
    Forbidden,
    InvalidCredentials,
    NotAuthenticated,
    Role,
    ScryptParams,
    bearer_token,
    hash_token,
)

__all__ = [
    "SESSIONS_TABLE",
    "USERS_TABLE",
    "AuthError",
    "AuthService",
    "DuplicateUser",
    "Forbidden",
    "InvalidCredentials",
    "NotAuthenticated",
    "Role",
    "ScryptParams",
    "bearer_token",
    "hash_token",
]
