#!/usr/bin/env python3
"""The `binary` gate of H6.3e: a node's k3s binary against the release's sha256.

A `system-upgrade-verify` Job runs it with the node's `/` mounted at `--host-root`. It reads
the node's `kubeletVersion` through the API as `k3s_gates.py` does and executes no host binary.
A node on another version than `--expect-version` reads pending, with a note and exit 0,
because a hold can leave a node on the old version while the pin moved. A node at the pinned
version whose binary hashes to another sum refuses, naming the node and both sums. Any read
error refuses.

It sits beside `k3s_gates.py` and imports it, so it ships to the same directory. The mode
cannot live in that file: `module_length_allowlist.txt` only ever falls, and the file is at
its cap.

Exit codes: 0 the binary matches (or the node is pending); 1 a refusal; 2 a usage error.

Usage:
    k3s_binary_gate.py --host-root /host --node N --expect-version V --expect-sha256 H
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys

import k3s_gates

BINARY = "usr/local/bin/k3s"


def check_binary(
    get: k3s_gates.Getter,
    root: str,
    node_name: str,
    version: str,
    sha256: str,
    notes: list[str],
) -> list[str]:
    """Refuse when the node runs `version` and its k3s binary does not hash to `sha256`."""
    if not version or not sha256 or not node_name:
        return ["binary check needs --node, --expect-version and --expect-sha256"]
    running = ""
    for item in k3s_gates._items(get(k3s_gates.NODES_PATH)):
        if k3s_gates._name(item) == node_name:
            info = (item.get("status") or {}).get("nodeInfo") or {}
            running = str(info.get("kubeletVersion", ""))
    if not running:
        return [f"node {node_name} reports no kubeletVersion"]
    if k3s_gates._munge(running) != k3s_gates._munge(version):
        notes.append(f"node {node_name} runs {running}, not {version}: pending")
        return []
    digest = hashlib.sha256()
    try:
        with open(k3s_gates.resolve_under(root, BINARY), "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError as exc:
        return [f"node {node_name}: cannot read the k3s binary: {exc}"]
    if digest.hexdigest() != sha256.lower():
        return [
            f"node {node_name}: the k3s binary hashes to {digest.hexdigest()}, "
            f"the release's sum is {sha256.lower()}"
        ]
    return []


def main(argv: list[str] | None = None, get: k3s_gates.Getter | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host-root", default="/host")
    parser.add_argument("--node", default=os.environ.get("NODE_NAME", ""))
    parser.add_argument("--expect-version", default="")
    parser.add_argument("--expect-sha256", default="")
    parser.add_argument("--server", default=k3s_gates._default_server())
    parser.add_argument("--token-file", default=f"{k3s_gates._SA_DIR}/token")
    parser.add_argument("--ca-file", default=f"{k3s_gates._SA_DIR}/ca.crt")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    if get is None:
        if not args.server:
            print(
                "REFUSED: no API server: pass --server or run in a pod", file=sys.stderr
            )
            return 1
        get = k3s_gates.Api(args.server, args.token_file, args.ca_file, args.timeout)
    notes: list[str] = []
    checks: list[k3s_gates.Check] = [
        (
            "binary sum",
            lambda: check_binary(
                get,
                args.host_root,
                args.node,
                args.expect_version,
                args.expect_sha256,
                notes,
            ),
        )
    ]
    refusals = k3s_gates.run_round(checks)
    for note in notes:
        print(f"NOTE: {note}")
    for refusal in refusals:
        print(f"REFUSED: {refusal}", file=sys.stderr)
    return 1 if refusals else 0


if __name__ == "__main__":
    sys.exit(main())
