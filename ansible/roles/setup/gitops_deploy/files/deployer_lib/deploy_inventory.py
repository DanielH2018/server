# ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_inventory.py
"""What this host declares, read from host_vars text.

`declared_k8s_services` and `service_owners` parse `containers_list` without yaml (the unit
runs under `uv run --no-project`), and `declares_no_gitops` reads whether this host is the
deployer at all.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from deployer_lib.deploy_changes import ChangeSet

# One containers_list entry: the `- name:` line plus everything indented under it up to the next
# `- name:` at the same (2-space) indent, or EOF. Two-space list indent is the repo-wide inventory
# convention; matching on it (rather than YAML-parsing) keeps this module stdlib-only and immune to
# the Jinja expressions inventory values carry.
_DECLARED_ENTRY = re.compile(
    r"^  - name: (\S+)(.*?)(?=^  - name: |\Z)", re.MULTILINE | re.DOTALL
)
# `platform: <value>` at the sub-key indent (4 spaces) within one entry's block.
_ENTRY_PLATFORM = re.compile(r"^    platform:\s*(\S+)", re.MULTILINE)
# The `containers_list:` key, and the first column-0 line after it that is not a comment, which
# ends the list. Without the end, the last entry's block runs on through the host's later
# top-level keys, and a later list of `- name:` items reads as more entries.
_LIST_START = re.compile(r"^containers_list:[ \t]*(?:#.*)?$", re.MULTILINE)
_LIST_END = re.compile(r"^[^\s#]", re.MULTILINE)


def _listing(hostvars_text: str) -> str:
    """The text of the `containers_list:` block alone, or "" when the file has no such key.

    Both readers run `_DECLARED_ENTRY` over this rather than the whole file, so a top-level
    list before or after `containers_list` never reads as entries (#4964).
    """
    start = _LIST_START.search(hostvars_text)
    if start is None:
        return ""
    return _LIST_END.split(hostvars_text[start.end() :], maxsplit=1)[0]


def declared_k8s_services(hostvars_text: str) -> set[str]:
    """The `platform: k8s` service names declared in a host's containers_list block.

    `deploy_k8s_owed.alert_and_record_deferred` reads it to name the k8s roles a tick merged
    and will not apply. An entry with no `platform:` key is Docker, and is not returned.
    """
    out: set[str] = set()
    for m in _DECLARED_ENTRY.finditer(_listing(hostvars_text)):
        name, block = m.group(1), m.group(2)
        pm = _ENTRY_PLATFORM.search(block)
        platform = pm.group(1) if pm else "docker"
        if platform == "k8s":
            out.add(name)
    return out


# The values a containers_list entry's `owner` takes. `flux`: Flux applies the tag. `released`:
# the first PR of a handback, which the renderer leaves out of the bundle. Every Ansible path
# skips both; an entry without `owner` is Ansible-owned. A copy of
# `scripts/lib/k8s_roles.py:OWNER_VALUES`, which this stdlib-only unit cannot import;
# `ansible/tests/deploy/test_owner_readers_agree.py` holds the copies equal. The plan is
# docs/target-architecture/deploy/d2-first-leaf.md, *The ownership flag and its three readers*.
OWNER_VALUES = frozenset({"flux", "released"})

# `owner:` and `tags:` at the sub-key indent, each with the rest of its line.
_ENTRY_OWNER = re.compile(r"^    owner:(.*)$", re.MULTILINE)
_ENTRY_TAGS = re.compile(r"^    tags:(.*)$", re.MULTILINE)


def _scalar(raw: str) -> str:
    """One YAML scalar from the rest of a `key:` line: no trailing comment, no quotes.

    A `#` opens a comment only after whitespace, as in YAML. `~` stays `~`, which no caller
    accepts as a value.
    """
    value = re.split(r"\s#", raw, maxsplit=1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _entry_tags(name: str, block: str) -> list[str]:
    """The tags an entry selects under: its `tags:` flow list, or its name.

    `deploy.yml`'s `container_item.tags | default([container_item.name])`, as
    `scripts/lib/render_guard.py:entry_tags` mirrors it. Only the flow form `[a, b]` is read,
    the only form an entry has used; any other `tags:` value raises ValueError rather than
    map the wrong tags.
    """
    m = _ENTRY_TAGS.search(block)
    if m is None:
        return [name]
    value = _scalar(m.group(1))
    if not (value.startswith("[") and value.endswith("]")):
        raise ValueError(
            f"containers_list entry {name} has tags {value!r}, which this reader cannot "
            "parse: write them as a flow list, [a, b]"
        )
    return [_scalar(t) for t in value[1:-1].split(",") if t.strip()]


def service_owners(hostvars_text: str) -> dict[str, str]:
    """Each deploy tag of a `platform: k8s` entry that sets `owner`, mapped to that value.

    The deployer's reader of D2's ownership flag, returning what
    `scripts/lib/k8s_roles.py:tag_owners` returns for the same file. A tag absent from the
    result is Ansible-owned. Only k8s entries are read, because Flux applies k8s tags alone.
    An entry that declares `tags` maps each of them.

    Raises:
        ValueError: an `owner` value outside `OWNER_VALUES`, a bare `owner:` included, or a
            `tags` value that is not a flow list. A misspelt owner read as Ansible-owned
            would hand the tag to both Ansible and Flux. The census in
            `ansible/tests/deploy/test_owner_readers_agree.py` keeps such a value off master,
            and the deployer applies only a green master, so a raise here means CI was
            bypassed: the tick crashes and pages rather than guess an owner.
    """
    owners: dict[str, str] = {}
    for m in _DECLARED_ENTRY.finditer(_listing(hostvars_text)):
        name, block = m.group(1), m.group(2)
        om = _ENTRY_OWNER.search(block)
        pm = _ENTRY_PLATFORM.search(block)
        if om is None or pm is None or _scalar(pm.group(1)) != "k8s":
            continue
        owner = _scalar(om.group(1))
        if owner not in OWNER_VALUES:
            raise ValueError(
                f"containers_list entry {name} has owner {owner!r}, "
                f"not one of {sorted(OWNER_VALUES)}"
            )
        owners.update(dict.fromkeys(_entry_tags(name, block), owner))
    return owners


def route_not_ansible_owned(cs: ChangeSet, owners: Mapping[str, str]) -> None:
    """Move the k8s roles whose tag is in `owners` from `cs.k8s` into `cs.flux`.

    `owners` is `service_owners` at the commit being applied. A tag that reaches the range only
    as a consumer of another role's changed file leaves `cs.k8s_consumers` and is not moved,
    because the range did not change it. `deploy_k8s_owed.skip_not_ansible_owned` logs what
    moved; this module stays import-pure, because `deploy_logic` re-exports it.

    `deploy_phases.plan_tick` runs this before `split_k8s_auto_deploy`, which promotes out of
    `cs.k8s` alone, so an image bump to such a role is never promoted. A k8s role's directory
    name is its tag, as everything that names a `cs.k8s` member as a `--tags` value assumes.
    """
    moved = cs.k8s & owners.keys()
    cs.k8s -= moved
    cs.k8s_consumers -= owners.keys()
    cs.flux |= moved


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
