# ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_inventory.py
"""What this host declares, read from host_vars text.

`declares_no_gitops` reads whether this host is the deployer at all, with no yaml, because the
unit runs under `uv run --no-project`.
"""

from __future__ import annotations

import re

_NO_GITOPS = re.compile(
    r"^has_gitops:\s*(?:false|no|off)\s*(?:#.*)?$", re.MULTILINE | re.IGNORECASE
)


def declares_no_gitops(hostvars_text: str | None) -> bool:
    """Whether this host's inventory says it is NOT a GitOps deployer.

    The role installs the deployer only `when: has_gitops`, but the gate lived in the role and
    nowhere in the code: daniel-server carried `has_gitops: false` and ticked a live timer for
    three weeks from a payload the role had stopped updating (#1733). This is the code's own
    opinion, read from the same host_vars the role reads, so a payload left behind on a host the
    inventory has demoted refuses to tick.

    Fails OPEN on everything but an unambiguous top-level `has_gitops: false`: a missing or
    unreadable host_vars file (None), an absent key (this reader never consults group_vars, whose
    default is false), `true`, an indented occurrence, and a commented-out line all proceed. A
    false refusal on the real deployer parks every landing in the fleet, which is strictly worse
    than one stale tick.
    """
    if hostvars_text is None:
        return False
    return _NO_GITOPS.search(hostvars_text) is not None
