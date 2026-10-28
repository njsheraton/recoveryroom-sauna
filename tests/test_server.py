"""HTTP surface tests with a fake device (no sockets to any sauna)."""
import http.client
import json
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from recoveryroom_sauna import protocol as P
from recoveryroom_sauna.config import load_config_dict
from recoveryroom_sauna.device import ConnState, DeviceSnapshot, Observation
from recoveryroom_sauna.security import SessionStore
from recoveryroom_sauna.server import AppContext, make_server

WEEKLY = {d: {"open": "09:00", "close": "17:00"} for d in
          ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}


class FakeDevice:
    def __init__(self):
        self.state = ConnState.CONNECTED
        self.report = P.parse_state_payload(P.build_state_payload(power=True, target_f=176, current_f=140, set_minute=30, internal_light=True))
        self.obs_id = 7
        self.age = 2.0
        self.refreshes = 0

    def snapshot(self):
        now = time.monotonic()
        obs = None if self.report is None else Observation(self.obs_id, self.report, now - self.age, datetime.now(timezone.utc))
        return DeviceSnapshot(self.state, "fake", "127.0.0.1", 1, obs, None, 0, 10, 0, False, None, now_monotonic=now)

    def request_refresh(self):
        self.refreshes += 1
        self.obs_id += 1

    def wait_for_newer_observation(self, after_id, timeout):
        return self.snapshot().observation if self.obs_id > after_id else None


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def app(tmp_path):
    port = _free_port()
    cfg = load_config_dict({"schemaVersion": 1, "webPort": port, "simulate": True, "privateDir": str(tmp_path / "priv"),
                            "businessHours": {"weekly": WEEKLY, "revision": "t1"}})
    assert cfg.security.allowed_hosts == (f"127.0.0.1:{port}", f"localhost:{port}")
    store = SessionStore(cfg.private_dir)
    dev = FakeDevice()
    ctx = AppContext(cfg, dev, store, clock=lambda: datetime(2026, 11, 2, 10, 0, tzinfo=ZoneInfo("Australia/Brisbane")))
    srv = make_server(ctx)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield {"port": port, "store": store, "dev": dev, "ctx": ctx}
    srv.shutdown()


def call(app, method, path, body=None, headers=None, host=None, cookie=None):
    port = app["port"]
    h = {"Host": host or f"127.0.0.1:{port}"}
    if cookie:
        h["Cookie"] = cookie
    if body is not None:
        h["Content-Type"] = "application/json"
        h.setdefault("Origin", f"http://127.0.0.1:{port}")
        body = body if isinstance(body, (bytes, str)) else json.dumps(body)
    h.update(headers or {})
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    try:
        parsed = json.loads(data)
    except Exception:
        parsed = data
    return r.status, parsed, r


def pair(app):
    pin, _ = app["store"].open_pairing()
    status, body, r = call(app, "POST", "/api/pair", {"pin": pin, "label": "test"})
    assert status == 200, body
    sc = r.getheader("Set-Cookie")
    assert "HttpOnly" in sc and "SameSite=Strict" in sc
    return sc.split(";")[0]


def test_health_is_minimal_and_public(app):
    status, body, r = call(app, "GET", "/api/health")
    assert status == 200 and body["ok"] and "version" in body and "build" in body
    assert "device" not in body and "report" not in body
    assert "default-src 'none'" in r.getheader("Content-Security-Policy")
    assert r.getheader("Access-Control-Allow-Origin") is None


def test_status_requires_pairing(app):
    assert call(app, "GET", "/api/status")[0] == 401
    assert call(app, "GET", "/api/config")[0] == 401
    assert call(app, "POST", "/api/refresh", {})[0] == 401
    assert call(app, "GET", "/api/status", cookie="rr_session=forged")[0] == 401


def test_wrong_host_refused(app):
    assert call(app, "GET", "/api/bootstrap", host="evil.example:80")[0] == 421


def test_cross_origin_post_refused(app):
    pin, _ = app["store"].open_pairing()
    status, _, _ = call(app, "POST", "/api/pair", {"pin": pin, "label": "x"}, headers={"Origin": "http://evil.example"})
    assert status == 403
    status, _, _ = call(app, "POST", "/api/pair", {"pin": pin, "label": "x"}, headers={"Origin": "null"})
    assert status == 403


def test_pairing_then_status_shows_fresh_report(app):
    cookie = pair(app)
    status, s, _ = call(app, "GET", "/api/status", cookie=cookie)
    assert status == 200
    assert s["mode"] == "read-only" and s["appliance"]["power"] == "on" and s["appliance"]["ready"] is True
    assert s["device"]["fresh"] and s["device"]["report"]["currentC"] == 60
    assert s["device"]["writesEnabled"] is False
    assert s["automation"]["mode"] == "disabled" and s["automation"]["pendingStop"] is False
    assert "commissioning.writesEnabled is false" in s["automation"]["scheduledHeatingBlockers"]
    assert s["hours"]["currentWindow"]["businessDate"] == "2026-11-02"
    assert s["hours"]["effectiveDeadline"].startswith("2026-11-02T17:30:00+10:00")
    assert s["hours"]["nextWindow"]["businessDate"] == "2026-11-03"
    status, b, _ = call(app, "GET", "/api/bootstrap", cookie=cookie)
    assert b["authenticated"] is True


def test_stale_or_disconnected_never_shows_confirmed_power(app):
    cookie = pair(app)
    app["dev"].age = 25.0
    s = call(app, "GET", "/api/status", cookie=cookie)[1]
    assert s["appliance"]["power"] == "unknown" and s["device"]["fresh"] is False and s["appliance"]["ready"] is False
    app["dev"].age = 1.0
    app["dev"].state = ConnState.DISCONNECTED
    s = call(app, "GET", "/api/status", cookie=cookie)[1]
    assert s["appliance"]["power"] == "unknown"
    app["dev"].state = ConnState.CONNECTED
    app["dev"].report = None
    s = call(app, "GET", "/api/status", cookie=cookie)[1]
    assert s["appliance"]["power"] == "unknown" and s["device"]["report"] is None


def test_refresh_requests_read_and_reports_advance(app):
    cookie = pair(app)
    status, s, _ = call(app, "POST", "/api/refresh", {}, cookie=cookie)
    assert status == 200 and s["refresh"]["advanced"] is True and app["dev"].refreshes == 1


def test_control_endpoints_not_implemented(app):
    cookie = pair(app)
    for ep in ("/api/warmup", "/api/handover", "/api/adjust", "/api/lights", "/api/finish", "/api/schedules"):
        status, body, _ = call(app, "POST", ep, {"tempC": 80}, cookie=cookie)
        assert status == 501 and "blockers" in body


def test_body_validation(app):
    cookie = pair(app)
    assert call(app, "POST", "/api/refresh", "{}", headers={"Content-Type": "text/plain"}, cookie=cookie)[0] == 415
    assert call(app, "POST", "/api/refresh", "{bad", cookie=cookie)[0] == 400
    assert call(app, "POST", "/api/refresh", "[]", cookie=cookie)[0] == 400
    assert call(app, "POST", "/api/refresh", "{" + " " * 5000 + "}", cookie=cookie)[0] == 413
    pin, _ = app["store"].open_pairing()
    assert call(app, "POST", "/api/pair", {"pin": 123456, "label": "x"})[0] == 400


def test_brute_force_pairing_locks_out(app):
    pin, _ = app["store"].open_pairing()
    wrong = "000000" if pin != "000000" else "111111"
    for _ in range(5):
        assert call(app, "POST", "/api/pair", {"pin": wrong, "label": "x"})[0] == 403
    assert call(app, "POST", "/api/pair", {"pin": pin, "label": "x"})[0] == 403


def test_logout_revokes(app):
    cookie = pair(app)
    assert call(app, "GET", "/api/status", cookie=cookie)[0] == 200
    assert call(app, "POST", "/api/logout", {}, cookie=cookie)[0] == 200
    assert call(app, "GET", "/api/status", cookie=cookie)[0] == 401


def test_static_and_traversal(app):
    status, body, r = call(app, "GET", "/")
    assert status == 200 and b"READ-ONLY BUILD" in body
    assert call(app, "GET", "/static/app.js")[0] == 200
    assert call(app, "GET", "/static/../config.py")[0] == 404
    assert call(app, "GET", "/static/..%2fconfig.py")[0] == 404
    assert call(app, "GET", "/nope")[0] == 404
