"""Status-only local HTTP server (same-Mac mode).

Bound to loopback. Strict Host and Origin checks, no CORS, restrictive CSP,
bounded JSON bodies, authenticated status. No endpoint in this build can
change device state; /api/refresh only requests a new read.

Deliberate same-Mac HTTP mode: cookies omit the Secure flag because the
transport is loopback only. Enabling remote access later requires HTTPS and
is a separate, owner-approved change.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .business_hours import effective_deadline
from .config import AppConfig
from .security import SESSION_COOKIE, SessionStore

log = logging.getLogger("rr.server")

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 4096
_RATE_WINDOW = 60.0
_RATE_LIMIT_PAIR = 10          # pairing attempts per minute per process
_RATE_LIMIT_GENERAL = 600      # requests per minute per process


def build_id() -> str:
    for candidate in (Path(__file__).parent.parent / "BUILD_ID", Path(__file__).parent / "BUILD_ID"):
        if candidate.exists():
            return candidate.read_text().strip()[:40] or "unknown"
    try:
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=Path(__file__).parent,
                             capture_output=True, text=True, timeout=2)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "dev"


class _RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str, limit: int) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < _RATE_WINDOW]
            if len(hits) >= limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


class AppContext:
    """Shared, thread-safe view the handler reads from."""

    def __init__(self, config: AppConfig, device, sessions: SessionStore, *, clock=None) -> None:
        self.config = config
        self.device = device
        self.sessions = sessions
        self.build = build_id()
        self.started_at = datetime.now(timezone.utc)
        self.rate = _RateLimiter()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def status_payload(self) -> dict:
        snap = self.device.snapshot()
        now = self.clock()
        policy = self.config.business_hours
        current = policy.current_window(now) if policy.configured else None
        nxt = policy.next_window(now) if policy.configured else None
        report = snap.observation.report if snap.observation else None

        # Appliance power is reported separately from automation mode and
        # never inferred from connectivity. Unknown stays unknown.
        if snap.fresh and report is not None:
            power = "on" if report.power else "off"
        else:
            power = "unknown"

        deadline = None
        if current is not None:
            deadline = effective_deadline(current, None, self.config.continuous_operation_limit_minutes).isoformat()

        return {
            "serverTimeUtc": now.isoformat(),
            "version": __version__,
            "build": self.build,
            "mode": "read-only",
            "automation": {
                "mode": "disabled",
                "armed": False,
                "armedBusinessDate": None,
                "pendingStop": False,
                "scheduledHeatingBlockers": self.config.scheduled_heating_blockers(),
                "manualHeatingBlockers": self.config.manual_heating_blockers(),
            },
            "appliance": {
                "power": power,
                "ready": bool(snap.fresh and report is not None and report.current_c >= self.config.ready_temp_c and report.power),
                "readyTempC": self.config.ready_temp_c,
            },
            "device": snap.to_dict(),
            "hours": {
                "configured": policy.configured,
                "revision": policy.revision,
                "timezone": policy.timezone,
                "currentWindow": current.to_dict() if current else None,
                "nextWindow": nxt.to_dict() if nxt else None,
                "effectiveDeadline": deadline,
                "preheatLeadMinutes": policy.preheat_lead_minutes,
                "shutdownAfterCloseMinutes": policy.shutdown_after_close_minutes,
            },
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "RecoveryRoomSauna"
    sys_version = ""
    ctx: AppContext  # set on the server class

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt, *args):   # keep access logs sanitised and quiet
        log.debug("%s %s", self.address_string(), fmt % args)

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                         "connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")

    def _json(self, status: int, body: dict, extra_headers: dict | None = None) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        return host in self.ctx.config.security.allowed_hosts

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            # Browsers always send Origin on cross-site POST; a missing Origin
            # with a same-site fetch is acceptable only when Sec-Fetch-Site says so.
            return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")
        host = self.headers.get("Host", "")
        return origin in (f"http://{host}", f"https://{host}") and self._host_ok()

    def _session_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        c = SimpleCookie()
        try:
            c.load(raw)
        except Exception:
            return None
        m = c.get(SESSION_COOKIE)
        return m.value if m else None

    def _authed(self):
        return self.ctx.sessions.authenticate(self._session_token())

    def _read_json(self) -> dict | None:
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("application/json"):
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "expected application/json"})
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "bad content length"})
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"})
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return None
        if not isinstance(body, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "expected JSON object"})
            return None
        return body

    def _gate(self, *, need_origin: bool) -> bool:
        if not self.ctx.rate.allow("general", _RATE_LIMIT_GENERAL):
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate limited"})
            return False
        if not self._host_ok():
            self._json(HTTPStatus.MISDIRECTED_REQUEST, {"error": "host not allowed"})
            return False
        if need_origin and not self._origin_ok():
            self._json(HTTPStatus.FORBIDDEN, {"error": "cross-origin request refused"})
            return False
        return True

    # ------------------------------------------------------------- routes
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            # Minimal, unauthenticated, no device state, no secrets.
            return self._json(HTTPStatus.OK, {"ok": True, "version": __version__, "build": self.ctx.build})
        if not self._gate(need_origin=False):
            return
        if path == "/api/bootstrap":
            sess = self._authed()
            return self._json(HTTPStatus.OK, {
                "installationName": self.ctx.config.installation_name,
                "authenticated": sess is not None,
                "pairingOpen": self.ctx.sessions.pairing_open() if sess is None else False,
                "version": __version__, "build": self.ctx.build,
            })
        if path == "/api/status":
            if self._authed() is None:
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "not paired"})
            return self._json(HTTPStatus.OK, self.ctx.status_payload())
        if path == "/api/config":
            if self._authed() is None:
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "not paired"})
            return self._json(HTTPStatus.OK, self.ctx.config.public_dict())
        if path == "/":
            return self._static("index.html")
        if path.startswith("/static/") and "/../" not in path and ".." not in path:
            return self._static(path[len("/static/"):])
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if not self._gate(need_origin=True):
            return
        if path == "/api/pair":
            if not self.ctx.rate.allow("pair", _RATE_LIMIT_PAIR):
                return self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "too many pairing attempts"})
            body = self._read_json()
            if body is None:
                return
            pin = body.get("pin")
            label = body.get("label", "shop browser")
            if not isinstance(pin, str) or not isinstance(label, str):
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "pin and label must be strings"})
            token = self.ctx.sessions.redeem_pin(pin, label)
            if token is None:
                return self._json(HTTPStatus.FORBIDDEN, {"error": "pairing refused"})
            cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={self.ctx.config.security.session_lifetime_days * 86400}"
            return self._json(HTTPStatus.OK, {"ok": True}, {"Set-Cookie": cookie})
        if path == "/api/logout":
            self.ctx.sessions.revoke_token(self._session_token())
            return self._json(HTTPStatus.OK, {"ok": True},
                              {"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
        if self._authed() is None:
            return self._json(HTTPStatus.UNAUTHORIZED, {"error": "not paired"})
        if path == "/api/refresh":
            # Requests a new observation only. Never changes device state,
            # never clears any obligation.
            body = self._read_json()
            if body is None:
                return
            snap = self.ctx.device.snapshot()
            after = snap.observation.id if snap.observation else 0
            self.ctx.device.request_refresh()
            obs = self.ctx.device.wait_for_newer_observation(after, timeout=8.0)
            payload = self.ctx.status_payload()
            payload["refresh"] = {"requestedAfterObservation": after, "advanced": obs is not None}
            return self._json(HTTPStatus.OK, payload)
        if path in ("/api/warmup", "/api/handover", "/api/adjust", "/api/lights", "/api/finish", "/api/schedules"):
            return self._json(HTTPStatus.NOT_IMPLEMENTED, {
                "error": "controls are not available in the read-only build",
                "blockers": self.ctx.config.manual_heating_blockers(),
            })
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _static(self, name: str) -> None:
        file = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in file.parents or not file.is_file():
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}.get(file.suffix, "application/octet-stream")
        data = file.read_bytes()
        self.send_response(HTTPStatus.OK)
        self._security_headers()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(ctx: AppContext) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"ctx": ctx})
    srv = ThreadingHTTPServer((ctx.config.web_bind, ctx.config.web_port), handler)
    srv.daemon_threads = True
    return srv
