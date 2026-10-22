"""Fixture helper for the cross-plane gate's real-render pairs."""

import re
from pathlib import Path

from validate import cross_plane_order as gate


def hand_back_authelia(tree: Path) -> Path:
    """Remove `owner: flux` from authelia's inventory entry in the copy at `tree`.

    authelia is flux-owned at HEAD since D5's flip. The real-render pairs need an Ansible-owned
    member beside the flux-owned bento-pdf, so that arm 1 has a dependency crossing planes.

    Returns:
        `tree`, so a fixture can return the call.
    """
    inventory = tree / gate.HOST_VARS
    text = inventory.read_text()
    entry = re.search(
        r"  - name: authelia\n(?:(?!  - name:).*\n)*?    owner: flux\n", text
    )
    assert entry, "the inventory has no flux-owned authelia entry to hand back"
    inventory.write_text(
        text.replace(entry.group(0), entry.group(0).replace("    owner: flux\n", ""), 1)
    )
    return tree
