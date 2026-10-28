#!/usr/bin/env python3
"""Find the sauna on the shop LAN. Run ON THE SHOP MAC.

    python3 tools/find_sauna.py

Attempts a plain TCP connection to port 12416 on every address in the Mac's
own /24 subnet and lists the ones that accept. That is all it does: no data
is sent, no login, no control. Use the result as --host for lan_diagnose.py.

Options:
    --subnet 192.168.1.0/24   scan a different range
    --port 12416              scan a different port
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import socket
import sys

CONNECT_TIMEOUT = 1.5
CONCURRENCY = 64


def local_ipv4() -> str | None:
    """Best-effort: the address the Mac would use to reach the LAN."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))      # no packets are sent for UDP connect()
            return s.getsockname()[0]
    except OSError:
        return None


async def probe(ip: str, port: int, sem: asyncio.Semaphore) -> str | None:
    async with sem:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), CONNECT_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            return None
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return ip


async def scan(network: ipaddress.IPv4Network, port: int) -> list[str]:
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(probe(str(ip), port, sem) for ip in network.hosts()))
    return [r for r in results if r]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subnet", help="CIDR to scan; default is this Mac's /24")
    ap.add_argument("--port", type=int, default=12416)
    args = ap.parse_args()

    if args.subnet:
        network = ipaddress.ip_network(args.subnet, strict=False)
    else:
        me = local_ipv4()
        if not me:
            print("Could not work out this Mac's LAN address. Pass --subnet, e.g. --subnet 192.168.1.0/24")
            return 2
        network = ipaddress.ip_network(f"{me}/24", strict=False)
        print(f"This Mac is {me}; scanning {network} for port {args.port} ...")
    if network.num_addresses > 1024:
        print(f"{network} is too large to scan quickly; use a /24 or smaller")
        return 2

    found = asyncio.run(scan(network, args.port))
    print()
    if not found:
        print(f"Nothing on {network} accepted a connection on port {args.port}.")
        print("Possible reasons: the sauna is off or asleep, it is on a different Wi-Fi network or band,")
        print("it is the newer 5GHz-app generation (which may not use this port), or the Mac is on guest Wi-Fi.")
        return 1
    for ip in found:
        try:
            name = socket.gethostbyaddr(ip)[0]
        except OSError:
            name = ""
        print(f"  {ip}  {name}")
    print()
    print("Next step, using the address above (if more than one, try each):")
    print(f"  python3 tools/lan_diagnose.py --host {found[0]} --seconds 120 --out lan_diagnose_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
