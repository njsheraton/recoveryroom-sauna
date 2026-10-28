"""CLI: python -m recoveryroom_sauna <command>

  serve  --config PATH            run the status-only dashboard
  pair   --config PATH            owner-local: open a one-time pairing PIN
  sessions --config PATH          list paired browsers
  revoke --config PATH --id ID    revoke a paired browser
  check-config --config PATH      validate config and print blockers
  simulate [--port N]             run the simulated sauna alone
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from . import __version__
from .config import ConfigError, load_config
from .device import DeviceThread, LanDeviceClient
from .security import SecurityStateError, SessionStore
from .server import AppContext, build_id, make_server
from .simulator import SimulatedSauna, SimulatorServer


def _store(cfg) -> SessionStore:
    s = cfg.security
    return SessionStore(cfg.private_dir, session_lifetime_days=s.session_lifetime_days,
                        pin_lifetime_minutes=s.pairing_pin_lifetime_minutes, max_attempts=s.pairing_max_attempts)


def cmd_check_config(args) -> int:
    cfg = load_config(args.config)
    print(f"config OK: {cfg.source_path}")
    print(f"installation: {cfg.installation_name}  tz={cfg.timezone}  simulate={cfg.simulate}")
    print(f"sauna: {'(unset)' if cfg.sauna_host is None else cfg.sauna_host}:{cfg.sauna_port}  web: {cfg.web_bind}:{cfg.web_port}")
    print(f"hours configured: {cfg.business_hours.configured} (revision {cfg.business_hours.revision})")
    print("scheduled heating blockers:")
    for b in cfg.scheduled_heating_blockers() or ["(none at config level; runtime gates still apply)"]:
        print(f"  - {b}")
    print("manual heating blockers:")
    for b in cfg.manual_heating_blockers() or ["(none at config level; runtime gates still apply)"]:
        print(f"  - {b}")
    return 0


def cmd_pair(args) -> int:
    cfg = load_config(args.config)
    store = _store(cfg)
    pin, expires = store.open_pairing()
    mins = cfg.security.pairing_pin_lifetime_minutes
    print(f"Pairing PIN: {pin}   (valid {mins} min, single use, {cfg.security.pairing_max_attempts} attempts)")
    print(f"Open http://{cfg.security.allowed_hosts[0]}/ on this Mac and enter the PIN.")
    return 0


def cmd_sessions(args) -> int:
    cfg = load_config(args.config)
    for s in _store(cfg).list_sessions():
        print(f"{s.id}  {s.label!r}  created {datetime.fromtimestamp(s.created_at, timezone.utc):%Y-%m-%d}  "
              f"expires {datetime.fromtimestamp(s.expires_at, timezone.utc):%Y-%m-%d}  last seen {datetime.fromtimestamp(s.last_seen_at, timezone.utc):%Y-%m-%d %H:%M}Z")
    return 0


def cmd_revoke(args) -> int:
    cfg = load_config(args.config)
    ok = _store(cfg).revoke(args.id)
    print("revoked" if ok else "no such session")
    return 0 if ok else 1


def _run_simulator_thread(port: int, start_state: SimulatedSauna | None = None) -> tuple[threading.Thread, SimulatorServer, asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    sim = SimulatorServer(port=port, sauna=start_state or SimulatedSauna(), push_state_every=None)

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(sim.start())
        loop.run_forever()

    t = threading.Thread(target=run, name="rr-simulator", daemon=True)
    t.start()
    while sim.port == 0 or sim._server is None:
        time.sleep(0.05)
    return t, sim, loop


def cmd_simulate(args) -> int:
    logging.info("starting simulated sauna on 127.0.0.1:%s", args.port)
    _, sim, loop = _run_simulator_thread(args.port)
    print(f"simulated sauna listening on 127.0.0.1:{sim.port} (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        loop.call_soon_threadsafe(loop.stop)
    return 0


def cmd_serve(args) -> int:
    cfg = load_config(args.config)
    if cfg.commissioning.writes_enabled:
        print("ERROR: commissioning.writesEnabled is true but this is the read-only build. Refusing to start.", file=sys.stderr)
        return 2
    try:
        store = _store(cfg)
    except SecurityStateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    sim = None
    if cfg.simulate:
        _, sim, sim_loop = _run_simulator_thread(0)
        host, port, route = "127.0.0.1", sim.port, "simulator"
        logging.warning("SIMULATED sauna in use on 127.0.0.1:%s; no real appliance is connected", port)
    else:
        if cfg.sauna_host is None:
            print("ERROR: saunaHost is null. Set the confirmed private address (or simulate=true).", file=sys.stderr)
            return 2
        host, port, route = cfg.sauna_host, cfg.sauna_port, "lan"

    device = DeviceThread(lambda: LanDeviceClient(host, port, route=route, writes_enabled=False))
    device.start()
    ctx = AppContext(cfg, device, store)
    srv = make_server(ctx)
    logging.info("%s v%s build %s serving on http://%s:%s/ (read-only)", cfg.installation_name, __version__, ctx.build, cfg.web_bind, cfg.web_port)
    print(f"{cfg.installation_name}: http://{cfg.security.allowed_hosts[0]}/  (v{__version__} build {ctx.build}, read-only)")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    t = threading.Thread(target=srv.serve_forever, name="rr-http", daemon=True)
    t.start()
    try:
        while not stop.is_set():
            stop.wait(0.5)
    finally:
        srv.shutdown()
        device.stop()
        if sim is not None:
            sim_loop.call_soon_threadsafe(sim_loop.stop)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="recoveryroom_sauna", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log-level", default="INFO")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("serve", cmd_serve), ("pair", cmd_pair), ("sessions", cmd_sessions), ("check-config", cmd_check_config)):
        p = sub.add_parser(name); p.add_argument("--config", required=True); p.set_defaults(fn=fn)
    p = sub.add_parser("revoke"); p.add_argument("--config", required=True); p.add_argument("--id", required=True); p.set_defaults(fn=cmd_revoke)
    p = sub.add_parser("simulate"); p.add_argument("--port", type=int, default=12416); p.set_defaults(fn=cmd_simulate)
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.fn(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
