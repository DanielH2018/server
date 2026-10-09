# ansible/roles/setup/gitops_deploy/files/deploy_inventory.py
"""What this host declares, read from host_vars text.

`declared_k8s_services` parses `containers_list` without yaml (the unit runs under
`uv run --no-project`), and `declares_no_gitops` reads whether this host is the deployer at all.
"""

from __future__ import annotations

import re

# One containers_list entry: the `- name:` line plus everything indented under it up to the next
# `- name:` at the same (2-space) indent, or EOF. Two-space list indent is the repo-wide inventory
# convention; matching on it (rather than YAML-parsing) keeps this module stdlib-only and immune to
# the Jinja expressions inventory values carry.
_DECLARED_ENTRY = re.compile(
    r"^  - name: (\S+)(.*?)(?=^  - name: |\Z)", re.MULTILINE | re.DOTALL
)
# `platform: <value>` at the sub-key indent (4 spaces) within one entry's block.
_ENTRY_PLATFORM = re.compile(r"^    platform:\s*(\S+)", re.MULTILINE)


def declared_k8s_services(hostvars_text: str) -> set[str]:
    """The `platform: k8s` service names declared in a host's containers_list.

    `deploy_k8s_owed.alert_and_record_deferred` reads it to name the k8s roles a tick merged
    and will not apply. An entry with no `platform:` key is Docker, and is not returned.
    """
    out: set[str] = set()
    for m in _DECLARED_ENTRY.finditer(hostvars_text):
        name, block = m.group(1), m.group(2)
        pm = _ENTRY_PLATFORM.search(block)
        platform = pm.group(1) if pm else "docker"
        if platform == "k8s":
            out.add(name)
    return out


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
    unreadable host_vars file (None), an absent key (group_vars defaults it to true), `true`,
    an indented occurrence, and a commented-out line all proceed. A false refusal on the real
    deployer parks every landing in the fleet, which is strictly worse than one stale tick.
    """
    if hostvars_text is None:
        return False
    return _NO_GITOPS.search(hostvars_text) is not None
