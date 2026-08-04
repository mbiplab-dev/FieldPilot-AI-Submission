"""Authentication and authorisation for the two roles this site actually has.

A construction site has workers wearing the devices and a site manager supervising them. Those
two roles want opposite things from the same platform: a worker should see their own advisories,
a manager should see everyone's alerts and be the only one who can retune detection, approve an
RFI or promote a model. Before this module every route was effectively unauthenticated, which is
not a defensible posture for a safety system that stores site imagery.

Stdlib only, deliberately: `hashlib.scrypt` for passwords, `secrets` for tokens, `hmac` for
constant-time comparison. Adding passlib/PyJWT for this would buy nothing that the standard
library does not already do correctly.

Shape of it
    * passwords      per-user 16-byte random salt -> scrypt -> stored as hex, with the KDF cost
                     parameters stored ALONGSIDE each record so they can be raised later without
                     invalidating anybody (see `ScryptParams` and `_maybe_upgrade`).
    * sessions       opaque 256-bit `secrets.token_urlsafe` tokens. Only the SHA-256 of a token is
                     stored, and it is the primary key, so verification is a single indexed read
                     and a stolen database yields no usable tokens.
    * no JWT         a revocable server-side session is the right primitive for a system where a
                     supervisor must be able to log a lost pair of glasses out immediately. A
                     stateless token cannot be revoked; this can, and a password change does.

Framework-agnostic on purpose: the dependency factories at the bottom raise `NotAuthenticated` /
`Forbidden`, never `HTTPException`. `backend/app.py` translates them (each exception carries a
`status_code` for exactly that). `fastapi` is imported lazily so this module stays importable —
and testable — without a web framework present.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fieldpilot.logging_.logger import get_logger
from fieldpilot.storage import Column, DocStore, TableSpec

log = get_logger("fieldpilot.auth")

USERS_TABLE = TableSpec(
    "users",
    key="user_id",
    columns=(
        Column("username"),                     # as typed by the operator, for display
        Column("username_lower", indexed=True),  # the uniqueness/lookup key
        Column("role", indexed=True),
        Column("display_name"),
        Column("worker_id", indexed=True),      # links to the worker_id events carry; may be NULL
        Column("active", "bool", indexed=True),
        Column("password_hash"),                # hex scrypt output — never leaves this module
        Column("salt"),                         # hex, per user
        Column("kdf"),                          # algorithm name, for a future migration
        Column("kdf_n", "int"),
        Column("kdf_r", "int"),
        Column("kdf_p", "int"),
        Column("kdf_dklen", "int"),
        Column("created_at", "real"),
        Column("updated_at", "real"),
        Column("password_changed_at", "real"),
        Column("last_login_at", "real"),
    ),
    order_by="username_lower",
)

SESSIONS_TABLE = TableSpec(
    "sessions",
    # the PRIMARY KEY is the token hash: verification is one keyed read, and there is no column
    # anywhere in this schema from which a usable token could be reconstructed.
    key="token_hash",
    columns=(
        Column("session_id", indexed=True),     # safe to show in a UI or a log line
        Column("user_id", indexed=True),
        Column("created_at", "real"),
        Column("expires_at", "real", indexed=True),
        Column("revoked_at", "real"),
        Column("user_agent"),
    ),
)

AUTH_TABLES: tuple[TableSpec, ...] = (USERS_TABLE, SESSIONS_TABLE)

#: 12 hours — a site shift plus overtime. One login per shift, no mid-shift re-auth.
DEFAULT_TOKEN_TTL_S = 12 * 3600

MIN_PASSWORD_LEN = 8
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._@+-]{1,63}$")
_WORKER_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
#: the shape `events.bridge` produces (`w-<track_id>`); anything else still stores, but warns
_WORKER_ID_CONVENTION = re.compile(r"^w-\d+$")

#: one identical message for every authentication failure — see `authenticate`
_BAD_CREDENTIALS = "invalid username or password"


class Role(StrEnum):
    WORKER = "worker"
    SITE_MANAGER = "site_manager"


ROLES: tuple[str, ...] = tuple(r.value for r in Role)


class AuthError(Exception):
    """Base for every failure this module raises. Carries the HTTP status app.py should map to."""

    status_code = 400


class InvalidCredentials(AuthError):
    """Username unknown, password wrong, or the account disabled — indistinguishably."""

    status_code = 401


class NotAuthenticated(AuthError):
    """No token, or a token that is expired, revoked, or no longer backed by a live user."""

    status_code = 401


class Forbidden(AuthError):
    """Authenticated, but the wrong role for this operation."""

    status_code = 403


class DuplicateUser(AuthError, ValueError):
    """Username or worker_id already taken.

    Deliberately also a `ValueError`: the existing backend routes turn `ValueError` into a 400,
    so this integrates with them unchanged while still being catchable as an `AuthError`.
    """

    status_code = 409


@dataclass(frozen=True)
class ScryptParams:
    """scrypt cost parameters, stored per user record so they can be raised in place.

    Defaults: n=2**15, r=8, p=1, 32-byte key. That is 128*r*n = 32 MiB of memory and ~80 ms on
    the development box — comfortably interactive for a login, and memory-hard enough that the
    GPU/ASIC advantage which makes a bare SHA-256 of a password worthless does not apply. It sits
    below OWASP's 2**17 ideal on purpose: this process shares a 6 GB laptop GPU box with
    YOLO11m-pose, a PPE detector and a damage detector (BUILD_LOG §10), and a login that costs a
    second of CPU under that load is a self-inflicted outage. Raising the numbers later costs
    nothing: `authenticate` re-hashes a record whose parameters are weaker than the current
    defaults, so users migrate transparently on their next successful login.
    """

    n: int = 2**15
    r: int = 8
    p: int = 1
    dklen: int = 32
    name: str = "scrypt"

    @property
    def maxmem(self) -> int:
        """OpenSSL's default 32 MiB ceiling would reject n=2**15, so state the budget explicitly."""

        return 128 * self.r * (self.n + self.p + 2) + (1 << 20)

    def derive(self, password: bytes, salt: bytes) -> bytes:
        return hashlib.scrypt(
            password, salt=salt, n=self.n, r=self.r, p=self.p,
            dklen=self.dklen, maxmem=self.maxmem,
        )

    def to_record(self) -> dict[str, Any]:
        return {"kdf": self.name, "kdf_n": self.n, "kdf_r": self.r,
                "kdf_p": self.p, "kdf_dklen": self.dklen}

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> ScryptParams:
        """Read back the parameters a record was written with, defaulting to today's."""

        d = cls()
        name = str(record.get("kdf") or d.name)
        if name != "scrypt":
            raise AuthError(f"unsupported password KDF {name!r} on user record")
        return cls(
            n=int(record.get("kdf_n") or d.n),
            r=int(record.get("kdf_r") or d.r),
            p=int(record.get("kdf_p") or d.p),
            dklen=int(record.get("kdf_dklen") or d.dklen),
            name=name,
        )

    def weaker_than(self, other: ScryptParams) -> bool:
        return (self.n, self.r, self.p, self.dklen) < (other.n, other.r, other.p, other.dklen)


#: keys that must never appear in anything this module returns
_SECRET_KEYS = frozenset({
    "password", "password_hash", "salt", "kdf", "kdf_n", "kdf_r", "kdf_p", "kdf_dklen",
    "username_lower",
})

#: a fixed salt used only to burn the same CPU on an unknown username as on a known one
_DUMMY_SALT = b"fieldpilot-timing-equaliser-salt"


def _public(user: dict[str, Any]) -> dict[str, Any]:
    """The only user dict that leaves this module: no hash, no salt, no KDF parameters."""

    return {k: v for k, v in user.items() if k not in _SECRET_KEYS}


def _public_session(session: dict[str, Any]) -> dict[str, Any]:
    """Session metadata without the token hash — nothing here identifies a token."""

    return {k: v for k, v in session.items() if k != "token_hash"}


def hash_token(token: str) -> str:
    """SHA-256 hex of a session token.

    A single unsalted round is the right choice here and a wrong one for passwords: the token is
    256 bits of `secrets` output, so there is no guessable input to iterate over, and lookup must
    stay cheap enough to run on every request.
    """

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


DEFAULT_SEED: tuple[dict[str, Any], ...] = (
    {"username": "manager", "password": "manager123", "role": Role.SITE_MANAGER,
     "display_name": "Site Manager"},
    {"username": "worker1", "password": "worker123", "role": Role.WORKER,
     "display_name": "Ravi Kumar", "worker_id": "w-1"},
    {"username": "worker2", "password": "worker123", "role": Role.WORKER,
     "display_name": "Anita Sharma", "worker_id": "w-2"},
)


class AuthService:
    """Users, passwords and sessions over a `DocStore`.

    Every method is async because the KDF runs on a worker thread (`asyncio.to_thread`): an 80 ms
    synchronous scrypt would stall the event loop that is also serving MJPEG frames.
    """

    def __init__(
        self,
        store: DocStore,
        *,
        token_ttl_s: float = DEFAULT_TOKEN_TTL_S,
        kdf: ScryptParams | None = None,
    ) -> None:
        self._store = store
        self._users = store.table(USERS_TABLE)
        self._sessions = store.table(SESSIONS_TABLE)
        self.token_ttl_s = float(token_ttl_s)
        #: cost parameters for NEW hashes; existing records keep (and are verified with) their own
        self._kdf = kdf or ScryptParams()

    # -- lifecycle -------------------------------------------------------------

    async def start(self, *, seed: list[dict[str, Any]] | None = None) -> None:
        """Ensure the tables exist, seed demo accounts if there are none, drop dead sessions.

        Safe to call whether or not the caller already declared `AUTH_TABLES` when starting the
        `DocStore` (which is how `backend/app.py` declares every other table).
        """

        await self._ensure_tables()
        records = DEFAULT_SEED if seed is None else tuple(seed)
        if await self._users.count() == 0 and records:
            for spec in records:
                await self.create_user(
                    username=str(spec["username"]),
                    password=str(spec["password"]),
                    role=spec.get("role", Role.WORKER),
                    display_name=str(spec.get("display_name", "")),
                    worker_id=spec.get("worker_id"),
                )
            if seed is None:
                log.warning(
                    "seeded %d DEFAULT DEMO ACCOUNTS (%s) with well-known passwords. These are "
                    "for local demos only — change every one of them with set_password (or delete "
                    "them) before this reaches a real site. Anyone who has read this repository "
                    "knows these credentials.",
                    len(records), ", ".join(str(r["username"]) for r in records),
                )
            else:
                log.info("seeded %d account(s) from the caller-supplied seed list", len(records))
        purged = await self.purge_expired_sessions()
        log.info(
            "auth ready: %d user(s), %d live session(s)%s",
            await self._users.count(), await self._sessions.count(),
            f", {purged} expired session(s) purged" if purged else "",
        )

    async def _ensure_tables(self) -> None:
        """Create our tables unless the shared store already declared them.

        `DocStore.start` re-opens the connection, so calling it a second time when `app.py` has
        already declared `AUTH_TABLES` would leak the first one. Checking what the store knows
        keeps both wiring styles correct.
        """

        known = getattr(self._store, "_specs", {})
        if all(spec.name in known for spec in AUTH_TABLES):
            return
        await self._store.start([*AUTH_TABLES])

    # -- users -----------------------------------------------------------------

    async def create_user(
        self,
        *,
        username: str,
        password: str,
        role: str | Role,
        display_name: str = "",
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Create one user. Raises `DuplicateUser` on a taken username or worker_id."""

        name = _clean_username(username)
        parsed_role = _clean_role(role)
        _check_password(password)
        wid = _clean_worker_id(worker_id)
        lower = name.lower()

        if await self._find_by_username_lower(lower) is not None:
            raise DuplicateUser(f"username {name!r} is already taken")
        if wid is not None and await self._find_by_worker_id(wid) is not None:
            raise DuplicateUser(f"worker_id {wid!r} is already linked to another user")

        now = time.time()
        salt = secrets.token_bytes(16)
        derived = await self._derive(password, salt, self._kdf)
        record = {
            "user_id": uuid.uuid4().hex,
            "username": name,
            "username_lower": lower,
            "role": parsed_role.value,
            "display_name": str(display_name or name),
            "worker_id": wid,
            "active": True,
            "password_hash": derived.hex(),
            "salt": salt.hex(),
            **self._kdf.to_record(),
            "created_at": now,
            "updated_at": now,
            "password_changed_at": now,
            "last_login_at": None,
        }
        stored = await self._users.put(record)
        log.info("user created: %s (%s%s)", name, parsed_role.value,
                 f", {wid}" if wid else "")
        return _public(stored)

    async def list_users(self) -> list[dict[str, Any]]:
        rows = await self._users.list(limit=1000, descending=False)
        return [_public(r) for r in rows]

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        row = await self._users.get(str(user_id)) if user_id else None
        return _public(row) if row else None

    async def get_by_username(self, username: str) -> dict[str, Any] | None:
        row = await self._find_by_username_lower(_squash(username).lower())
        return _public(row) if row else None

    async def get_by_worker_id(self, worker_id: str) -> dict[str, Any] | None:
        """Which account owns the `worker_id` stamped on an event — how a worker sees own alerts."""

        wid = _squash(worker_id)
        row = await self._find_by_worker_id(wid) if wid else None
        return _public(row) if row else None

    async def set_password(self, user_id: str, password: str) -> bool:
        """Change a password and log every existing session of that user out.

        The second half is not optional. A password is changed either because it leaked or
        because somebody left; in both cases a session that outlived the change is the exact
        thing the change was meant to stop.
        """

        _check_password(password)
        user = await self._users.get(str(user_id))
        if user is None:
            return False
        salt = secrets.token_bytes(16)
        derived = await self._derive(password, salt, self._kdf)
        now = time.time()
        await self._users.patch(user["user_id"], {
            "password_hash": derived.hex(), "salt": salt.hex(), **self._kdf.to_record(),
            "password_changed_at": now, "updated_at": now,
        })
        revoked = await self.revoke_user_sessions(user["user_id"])
        log.info("password changed for %s; %d session(s) revoked", user["username"], revoked)
        return True

    async def set_active(self, user_id: str, active: bool) -> dict[str, Any] | None:
        """Enable/disable an account. Disabling revokes its sessions and blocks new logins."""

        updated = await self._users.patch(str(user_id), {
            "active": bool(active), "updated_at": time.time(),
        })
        if updated is None:
            return None
        if not active:
            await self.revoke_user_sessions(updated["user_id"])
        return _public(updated)

    async def delete_user(self, user_id: str) -> bool:
        await self.revoke_user_sessions(str(user_id))
        return await self._users.delete(str(user_id))

    # -- authentication --------------------------------------------------------

    async def authenticate(self, username: str, password: str) -> tuple[dict[str, Any], str]:
        """Verify credentials and open a session. Returns `(public_user, token)`.

        Unknown username, wrong password and disabled account all raise the SAME
        `InvalidCredentials` with the SAME message, and the unknown-username path still runs one
        scrypt derivation, so neither the response nor the response time tells an attacker which
        usernames exist.
        """

        user = await self._find_by_username_lower(_squash(username).lower())
        if user is None:
            await self._derive(password, _DUMMY_SALT, self._kdf)
            raise InvalidCredentials(_BAD_CREDENTIALS)

        params = ScryptParams.from_record(user)
        derived = await self._derive(password, bytes.fromhex(str(user["salt"])), params)
        if not hmac.compare_digest(derived.hex(), str(user["password_hash"])):
            log.warning("failed login for %s", user["username"])
            raise InvalidCredentials(_BAD_CREDENTIALS)
        if not user.get("active", True):
            log.warning("login refused for disabled account %s", user["username"])
            raise InvalidCredentials(_BAD_CREDENTIALS)

        user = await self._maybe_upgrade(user, password, params)
        token = await self._open_session(user["user_id"])
        updated = await self._users.patch(user["user_id"], {"last_login_at": time.time()})
        log.info("login: %s (%s)", user["username"], user["role"])
        return _public(updated or user), token

    async def resolve_token(self, token: str) -> dict[str, Any] | None:
        """The user behind a token, or None if it is unknown, expired, revoked, or orphaned."""

        if not token or not isinstance(token, str):
            return None
        session = await self._sessions.get(hash_token(token))
        if session is None:
            return None
        if session.get("revoked_at") is not None:
            return None
        if float(session.get("expires_at") or 0.0) <= time.time():
            return None
        user = await self._users.get(str(session.get("user_id") or ""))
        # a token can outlive its user: deletion and deactivation both invalidate it here even if
        # the session row itself was somehow missed
        if user is None or not user.get("active", True):
            return None
        return _public(user)

    async def logout(self, token: str) -> bool:
        """Revoke the session behind `token`. False if there was no live session to revoke."""

        if not token or not isinstance(token, str):
            return False
        token_hash = hash_token(token)
        session = await self._sessions.get(token_hash)
        if session is None or session.get("revoked_at") is not None:
            return False
        await self._sessions.patch(token_hash, {"revoked_at": time.time()})
        return True

    async def revoke_user_sessions(self, user_id: str) -> int:
        """Revoke every live session of one user. Returns how many were revoked."""

        now = time.time()
        rows = await self._sessions.list(
            where={"user_id": str(user_id), "revoked_at": ("isnull", True)}, limit=1000
        )
        for row in rows:
            await self._sessions.patch(row["token_hash"], {"revoked_at": now})
        return len(rows)

    async def list_sessions(
        self, user_id: str | None = None, *, live_only: bool = True
    ) -> list[dict[str, Any]]:
        where: dict[str, Any] = {}
        if user_id:
            where["user_id"] = str(user_id)
        if live_only:
            where["revoked_at"] = ("isnull", True)
            where["expires_at"] = ("gte", time.time())
        rows = await self._sessions.list(where=where or None, limit=1000)
        return [_public_session(r) for r in rows]

    async def purge_expired_sessions(self) -> int:
        """Delete sessions past their expiry. Returns how many rows went.

        Revoked-but-unexpired rows are kept: they are already unusable and they are the record
        that somebody logged out. They disappear on expiry like everything else.
        """

        rows = await self._sessions.list(
            where={"expires_at": ("lt_placeholder", None)} if False else None, limit=5000
        )
        now = time.time()
        dead = [r for r in rows if float(r.get("expires_at") or 0.0) <= now]
        for row in dead:
            await self._sessions.delete(row["token_hash"])
        return len(dead)

    # -- internals -------------------------------------------------------------

    async def _derive(self, password: str, salt: bytes, params: ScryptParams) -> bytes:
        return await asyncio.to_thread(params.derive, str(password).encode("utf-8"), salt)

    async def _maybe_upgrade(
        self, user: dict[str, Any], password: str, params: ScryptParams
    ) -> dict[str, Any]:
        """Re-hash a record written under weaker parameters, now that we hold the password.

        This is the point of storing the parameters per record: raising the defaults costs one
        extra derivation on each user's next login and nothing else.
        """

        if not params.weaker_than(self._kdf):
            return user
        salt = secrets.token_bytes(16)
        derived = await self._derive(password, salt, self._kdf)
        upgraded = await self._users.patch(user["user_id"], {
            "password_hash": derived.hex(), "salt": salt.hex(), **self._kdf.to_record(),
            "updated_at": time.time(),
        })
        log.info("upgraded password hash cost for %s (n=%d -> %d)",
                 user["username"], params.n, self._kdf.n)
        return upgraded or user

    async def _open_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)          # 256 bits
        now = time.time()
        await self._sessions.put({
            "token_hash": hash_token(token),
            "session_id": uuid.uuid4().hex,
            "user_id": user_id,
            "created_at": now,
            "expires_at": now + self.token_ttl_s,
            "revoked_at": None,
        })
        return token

    async def _find_by_username_lower(self, lower: str) -> dict[str, Any] | None:
        if not lower:
            return None
        rows = await self._users.list(where={"username_lower": lower}, limit=2)
        return rows[0] if rows else None

    async def _find_by_worker_id(self, worker_id: str) -> dict[str, Any] | None:
        if not worker_id:
            return None
        rows = await self._users.list(where={"worker_id": worker_id}, limit=2)
        return rows[0] if rows else None


# --------------------------------------------------------------------------- validation helpers


def _squash(value: Any) -> str:
    return str(value or "").strip()


def _clean_username(username: str) -> str:
    name = _squash(username)
    if not _USERNAME_RE.match(name):
        raise ValueError(
            "username must be 2-64 characters of letters, digits, dot, underscore, at, plus or "
            f"hyphen, starting alphanumeric; got {username!r}"
        )
    return name


def _clean_role(role: str | Role) -> Role:
    try:
        return Role(str(role).strip().lower())
    except ValueError as exc:
        raise ValueError(f"role must be one of {ROLES}; got {role!r}") from exc


def _clean_worker_id(worker_id: str | None) -> str | None:
    """Normalise a worker link. Blank and None both mean "not linked" so they cannot collide."""

    wid = _squash(worker_id)
    if not wid:
        return None
    if not _WORKER_ID_RE.match(wid):
        raise ValueError(f"worker_id {worker_id!r} is not a valid identifier")
    if not _WORKER_ID_CONVENTION.match(wid):
        # not fatal — sites may carry their own numbering — but a worker_id that does not match
        # what events.bridge stamps links this account to nothing, which is worth saying out loud
        log.warning(
            "worker_id %r does not look like the 'w-<n>' ids events carry; this account may not "
            "match any alert", wid,
        )
    return wid


def _check_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LEN} characters")


# --------------------------------------------------------------------------- HTTP-facing helpers


def bearer_token(authorization: str | None) -> str | None:
    """Pull the token out of an `Authorization` header, tolerantly.

    Accepts any capitalisation of the scheme and any run of whitespace around it, and accepts a
    bare token with no scheme at all (some device clients send one). Rejects other schemes rather
    than treating a Basic credential as a session token.
    """

    raw = _squash(authorization)
    if not raw:
        return None
    parts = raw.split()
    if len(parts) == 1:
        return None if parts[0].lower() in _KNOWN_SCHEMES else parts[0]
    if parts[0].lower() == "bearer" and len(parts) == 2:
        return parts[1] or None
    return None


_KNOWN_SCHEMES = frozenset({"bearer", "basic", "digest", "negotiate", "ntlm", "oauth", "token"})


async def user_from_header(
    service: AuthService, authorization: str | None, *, roles: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Resolve an `Authorization` header to a user, enforcing `roles` if given.

    The framework-agnostic core of the dependency factories below. Raises `NotAuthenticated` or
    `Forbidden`; never an `HTTPException`.
    """

    token = bearer_token(authorization)
    if not token:
        raise NotAuthenticated("missing bearer token")
    user = await service.resolve_token(token)
    if user is None:
        raise NotAuthenticated("invalid or expired session")
    if roles and str(user.get("role")) not in roles:
        raise Forbidden(
            f"role {user.get('role')!r} may not perform this operation "
            f"(requires {' or '.join(roles)})"
        )
    return user


def _header_default() -> Any:
    """`fastapi.Header(...)` when FastAPI is installed, else a plain `None` default.

    Imported lazily so this module — and its tests — need no web framework, while the callables
    returned below are still usable directly as `Depends(...)` targets.
    """

    try:
        from fastapi import Header
    except ImportError:                                    # pragma: no cover - fastapi is a dep
        return None
    return Header(default=None, alias="Authorization")


def require_user(service: AuthService) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Dependency: any authenticated user. `Depends(require_user(auth))`."""

    return require_role(service)


def require_role(
    service: AuthService, *roles: str | Role
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Dependency: an authenticated user whose role is one of `roles` (any role if none given).

    Roles are matched exactly — a site_manager does not implicitly satisfy a worker-only
    dependency, because "worker-only" endpoints are about whose device is asking, not seniority.
    """

    wanted = tuple(_clean_role(r).value for r in roles)
    default = _header_default()

    async def dependency(authorization: str | None = default) -> dict[str, Any]:
        return await user_from_header(service, authorization, roles=wanted)

    dependency.__name__ = f"require_role_{'_'.join(wanted) or 'any'}"
    return dependency


def require_site_manager(service: AuthService) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Dependency: site managers only — settings, model promotion, RFI approval."""

    return require_role(service, Role.SITE_MANAGER)


def require_worker(service: AuthService) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Dependency: workers only — device-side routes."""

    return require_role(service, Role.WORKER)
