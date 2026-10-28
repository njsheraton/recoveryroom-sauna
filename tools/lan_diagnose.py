#!/usr/bin/env python3
"""Read-only LAN diagnostic for the shop sauna. Run ON THE SHOP MAC.

    python3 tools/lan_diagnose.py --host 192.0.2.10 --seconds 90 --out report.json

What it does:  TCP connect, passcode request, login, heartbeat, state polls.
What it NEVER does: send a control frame (0x0093). There is no code path
for it in this script.

Output: a sanitised JSON report (no passcode material) plus a console
summary. Send the report back for review. Compare the reported power,
lights, target and timer against the physical panel while it runs; note
any mismatch.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from recoveryroom_sauna import __version__                      # noqa: E402
from recoveryroom_sauna import protocol as P                     # noqa: E402
from recoveryroom_sauna.device import LanDeviceClient, ConnState  # noqa: E402


async def run(host: str, port: int, seconds: int) -> dict:
    client = LanDeviceClient(host, port, route="lan-diagnostic", writes_enabled=False)
    await client.start()
    samples: list[dict] = []
    first_connected: float | None = None
    t0 = time.monotonic()
    last_id = 0
    try:
        while time.monotonic() - t0 < seconds:
            await asyncio.sleep(1.0)
            snap = client.snapshot()
            if snap.state == ConnState.CONNECTED and first_connected is None:
                first_connected = time.monotonic() - t0
            obs = snap.observation
            if obs is not None and obs.id != last_id:
                last_id = obs.id
                r = obs.report
                samples.append({
                    "t": round(time.monotonic() - t0, 1),
                    "utc": obs.wall_utc.isoformat(timespec="seconds"),
                    **r.to_dict(),
                    "rawHex": r.raw.hex(),
                    "rawLen": len(r.raw),
                })
                print(f"[{samples[-1]['t']:6.1f}s] power={'ON ' if r.power else 'off'} "
                      f"cur={r.current_c}C tgt={r.target_c}C timer={r.set_hour}h{r.set_minute:02d} "
                      f"inL={int(r.internal_light)} outL={int(r.external_light)} "
                      f"flagsC={int(r.celsius_display)} preset={int(r.preset_enabled)} uvb={int(r.uvb)} "
                      f"serial={r.serial} heart={r.heart} raw={r.raw.hex()}")
            elif snap.state != ConnState.CONNECTED:
                print(f"[{time.monotonic() - t0:6.1f}s] {snap.state.value} {snap.last_error or ''}")
    finally:
        snap = client.snapshot()
        await client.stop()

    raw_lengths = Counter(s["rawLen"] for s in samples)
    return {
        "tool": "lan_diagnose", "version": __version__,
        "ranAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target": {"host": host, "port": port},
        "durationSeconds": seconds,
        "connectedAfterSeconds": first_connected,
        "finalConnectionState": snap.state.value,
        "lastError": snap.last_error,
        "reconnects": snap.reconnects,
        "framesReceived": snap.frames_received,
        "protocolErrors": snap.protocol_errors,
        "stateReports": len(samples),
        "stateReportRawLengths": dict(raw_lengths),
        "distinctRawPayloads": len({s["rawHex"] for s in samples}),
        "samples": samples,
        "controlFramesSent": 0,
        "notes": [
            "No control frames (0x0093) were sent; this tool has no code path for them.",
            "Passcode material was used in-memory for login only and is not included.",
            "Compare each sample against the physical panel; record mismatches in the commissioning log.",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="confirmed private IP of the sauna (DHCP reserved)")
    ap.add_argument("--port", type=int, default=12416)
    ap.add_argument("--seconds", type=int, default=90)
    ap.add_argument("--out", default="lan_diagnose_report.json")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    report = asyncio.run(run(args.host, args.port, args.seconds))
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print()
    print(f"connected after: {report['connectedAfterSeconds']}s   final state: {report['finalConnectionState']}")
    print(f"state reports: {report['stateReports']}   frames: {report['framesReceived']}   "
          f"reconnects: {report['reconnects']}   protocol errors: {report['protocolErrors']}")
    print(f"raw payload lengths seen: {report['stateReportRawLengths']}")
    print(f"report written to {args.out}")
    if report["stateReports"] == 0:
        print("RESULT: no valid state reports. Do not proceed to any write testing.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
