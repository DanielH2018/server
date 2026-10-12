#!/usr/bin/env python3
"""Mask the home public IPv6 address to the /64 the `home-ips` allowlist holds (#4605).

The home LAN's devices share one /64 and rotate addresses inside it, so the allowlist entry is
the prefix, not the address ipify returns. Parsing is also the validation: a value ipify
returned that is not an IPv6 address fails here instead of becoming an allowlist entry.

This is a file rather than an inline `python -c` so that `lib.stdlib_only` can walk its
imports: the cron runs it `--no-project`, where only the standard library exists.

Usage:

    uv run --no-project --python <pin> /opt/crowdsec-home-allowlist/ipv6_prefix.py <address>

`crowdsec-update-home-allowlist.sh` runs it as root on daniel-box and reads the last line of
its combined output. Exit 0 prints the /64 network, such as `2001:db8:1:2::/64`, to stdout.
Exit 1 means the argument is not an IPv6 address, and one stderr line says why. Exit 2 means
the argument count was wrong. `-h` and `--help` print this text and exit 0.
"""

import ipaddress
import sys


def prefix64(address: str) -> str:
    """The /64 network holding `address`. Raises ValueError on anything but an IPv6 address."""
    return str(ipaddress.IPv6Network(f"{address}/64", strict=False))


def main(argv: list[str]) -> int:
    if argv in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    if len(argv) != 1:
        print("usage: ipv6_prefix.py <ipv6-address>", file=sys.stderr)
        return 2
    try:
        print(prefix64(argv[0]))
    except ValueError as exc:
        print(f"{argv[0]!r} is not an IPv6 address: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
