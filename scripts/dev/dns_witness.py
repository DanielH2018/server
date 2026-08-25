#!/usr/bin/env python3
"""Continuously resolve a name against the Pi-hole DNS VIP and record every gap.

Run on a host that is NOT the one being deployed, so the measurement is what a
LAN client sees. Prints one line per state transition plus a summary on SIGINT.
"""

import signal
import socket
import struct
import sys
import time

VIP = sys.argv[1] if len(sys.argv) > 1 else "10.0.0.243"
NAME = sys.argv[2] if len(sys.argv) > 2 else "pi.hole"
TIMEOUT = 1.0
INTERVAL = 0.25

_running = True


def _stop(signum, frame):
    global _running
    _running = False


def query(vip: str, name: str, port: int = 53) -> bool:
    """One UDP A query. True iff we got a well-formed response with rcode 0."""
    qname = (
        b"".join(
            struct.pack("B", len(label)) + label
            for label in name.encode("ascii").split(b".")
            if label
        )
        + b"\x00"
    )
    txid = 0x4242
    packet = struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0) + qname
    packet += struct.pack(">HH", 1, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(TIMEOUT)
        sock.sendto(packet, (vip, port))
        data, _ = sock.recvfrom(2048)
    except OSError:
        return False
    finally:
        sock.close()
    if len(data) < 12:
        return False
    rid, flags, _qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    return rid == txid and (flags & 0x000F) == 0 and an > 0


def main() -> int:
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    total = 0
    failures = 0
    gaps = []
    gap_start = None
    last_ok = None
    print(f"witness: {NAME} @ {VIP} every {INTERVAL}s", flush=True)
    while _running:
        now = time.time()
        ok = query(VIP, NAME)
        total += 1
        if not ok:
            failures += 1
        if last_ok is None or ok != last_ok:
            print(f"{time.strftime('%H:%M:%S')} {'OK' if ok else 'FAIL'}", flush=True)
            last_ok = ok
        if not ok and gap_start is None:
            gap_start = now
        elif ok and gap_start is not None:
            gaps.append(now - gap_start)
            gap_start = None
        time.sleep(INTERVAL)
    if gap_start is not None:
        gaps.append(time.time() - gap_start)
    longest = max(gaps) if gaps else 0.0
    print(
        f"SUMMARY queries={total} failures={failures} "
        f"gaps={len(gaps)} longest_gap_s={longest:.2f}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
