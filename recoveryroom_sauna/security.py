"""Staff session management and private-file handling.

* Pairing is an explicit owner-local action: the `pair` CLI command writes a
  short-lived six-digit PIN into the private directory. The shop browser
  enters it once and receives a random session token. Tokens are stored
  only as SHA-256 hashes; comparison is constant time.
* Sessions authenticate the workstation, not an individual employee.
* Private files are 0600 inside a 0700 directory and written atomically
  (same-dir temp file, flush, fsync, replace, fsync dir).
* Corrupt security data fails closed: we refuse to start rather than
  silently creating a fresh identity.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SESSION_COOKIE = "rr_session"
SESSIONS_FILE = "sessions.json"
PAIRING_FILE = "pairing.json"
TOKEN_BYTES = 32


class SecurityStateError(RuntimeError):
    """Private security data is corrupt or has unsafe permissions."""


# --------------------------------------------------------------------------- #
# Private file helpers
# --------------------------------------------------------------------------- #

def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def write_private_json(path: Path, data: dict) -> None:
    ensure_private_dir(path.parent)
    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def read_private_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        if default is None:
            raise SecurityStateError(f"missing {path}")
        return dict(default)
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise SecurityStateError(f"{path} is readable by others (mode {mode:o}); refusing to start")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SecurityStateError(f"{path} is corrupt ({exc}); preserve it for review, do not delete") from exc
    if not isinstance(data, dict):
        raise SecurityStateError(f"{path} has unexpected shape")
    return data


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


# --------------------------------------------------------------------------- #
# Pairing + sessions
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Session:
    id: str
    label: str
    created_at: float
    expires_at: float
    last_seen_at: float


class SessionStore:
    def __init__(self, private_dir: Path, *, session_lifetime_days: int = 30,
                 pin_lifetime_minutes: int = 10, max_attempts: int = 5, clock=time.time) -> None:
        self.private_dir = private_dir
        self.sessions_path = private_dir / SESSIONS_FILE
        self.pairing_path = private_dir / PAIRING_FILE
        self.session_lifetime = session_lifetime_days * 86400
        self.pin_lifetime = pin_lifetime_minutes * 60
        self.max_attempts = max_attempts
        self._clock = clock
        self._lock = threading.Lock()
        ensure_private_dir(private_dir)
        # Fail closed on corruption at construction time.
        self._load_sessions()
        self._load_pairing()

    # ---- storage
    def _load_sessions(self) -> dict:
        data = read_private_json(self.sessions_path, default={"version": 1, "sessions": {}})
        if data.get("version") != 1 or not isinstance(data.get("sessions"), dict):
            raise SecurityStateError("sessions.json has unexpected version/shape")
        return data

    def _save_sessions(self, data: dict) -> None:
        write_private_json(self.sessions_path, data)

    def _load_pairing(self) -> dict | None:
        if not self.pairing_path.exists():
            return None
        data = read_private_json(self.pairing_path)
        if "pin_hash" not in data or "expires_at" not in data:
            raise SecurityStateError("pairing.json has unexpected shape")
        return data

    # ---- pairing (owner-local action)
    def open_pairing(self, label_hint: str = "shop-mac") -> tuple[str, float]:
        """Create a one-time PIN. Returns (pin, expires_at). The PIN is shown
        to the owner on the local console only."""
        pin = f"{secrets.randbelow(1_000_000):06d}"
        expires = self._clock() + self.pin_lifetime
        with self._lock:
            write_private_json(self.pairing_path, {
                "pin_hash": _hash_token(pin), "expires_at": expires,
                "attempts": 0, "label_hint": label_hint, "created_at": self._clock(),
            })
        return pin, expires

    def pairing_open(self) -> bool:
        with self._lock:
            p = self._load_pairing()
            return p is not None and p["expires_at"] > self._clock() and p["attempts"] < self.max_attempts

    def close_pairing(self) -> None:
        with self._lock:
            if self.pairing_path.exists():
                self.pairing_path.unlink()

    def redeem_pin(self, pin: str, label: str) -> str | None:
        """Exchange a PIN for a session token (returned once, never stored)."""
        if not (isinstance(pin, str) and len(pin) == 6 and pin.isdigit()):
            return None
        with self._lock:
            p = self._load_pairing()
            if p is None or p["expires_at"] <= self._clock():
                return None
            if p["attempts"] >= self.max_attempts:
                return None
            ok = hmac.compare_digest(p["pin_hash"], _hash_token(pin))
            if not ok:
                p["attempts"] += 1
                write_private_json(self.pairing_path, p)
                return None
            # success: enrolment is one-shot; close pairing
            self.pairing_path.unlink(missing_ok=True)
            token = secrets.token_urlsafe(TOKEN_BYTES)
            now = self._clock()
            data = self._load_sessions()
            sid = secrets.token_hex(6)
            data["sessions"][_hash_token(token)] = {
                "id": sid, "label": label[:64], "created_at": now,
                "expires_at": now + self.session_lifetime, "last_seen_at": now,
            }
            self._save_sessions(data)
            return token

    # ---- sessions
    def authenticate(self, token: str | None) -> Session | None:
        if not token or len(token) > 128:
            return None
        h = _hash_token(token)
        with self._lock:
            data = self._load_sessions()
            # constant-time scan across all hashes to avoid timing leaks on membership
            found = None
            for stored_hash, rec in data["sessions"].items():
                if hmac.compare_digest(stored_hash, h):
                    found = (stored_hash, rec)
            if found is None:
                return None
            stored_hash, rec = found
            now = self._clock()
            if rec["expires_at"] <= now:
                del data["sessions"][stored_hash]
                self._save_sessions(data)
                return None
            if now - rec.get("last_seen_at", 0) > 300:
                rec["last_seen_at"] = now
                self._save_sessions(data)
            return Session(rec["id"], rec["label"], rec["created_at"], rec["expires_at"], rec["last_seen_at"])

    def revoke(self, session_id: str) -> bool:
        with self._lock:
            data = self._load_sessions()
            for h, rec in list(data["sessions"].items()):
                if rec["id"] == session_id:
                    del data["sessions"][h]
                    self._save_sessions(data)
                    return True
            return False

    def revoke_token(self, token: str | None) -> None:
        if not token:
            return
        h = _hash_token(token)
        with self._lock:
            data = self._load_sessions()
            if h in data["sessions"]:
                del data["sessions"][h]
                self._save_sessions(data)

    def list_sessions(self) -> list[Session]:
        with self._lock:
            data = self._load_sessions()
            return [Session(r["id"], r["label"], r["created_at"], r["expires_at"], r["last_seen_at"])
                    for r in data["sessions"].values()]
