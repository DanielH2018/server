"""The `activate:` field of a file entry and the `ufw:` key, which feed the reconcile manifest.

An `activate:` field names what reconcile does when the installed file's hash changes
(`hosts/lib/reconcile_lib/activate.py` documents each action)::

    files:
      - {src: sshd.conf, dst: /etc/ssh/sshd_config.d/50-homelab.conf, activate: "reload:ssh.service"}

An entry with no `activate:` is activated by nothing, which is `none` spelled out. The renderer
checks the grammar that reconcile's `valid_activations` reads, so a render it would refuse fails
here.

A `ufw:` key declares firewall rules for reconcile's ufw arm (`hosts/lib/reconcile_lib/ufw.py`)::

    ufw:
      rules:
        - {id: lan-ssh, spec: "allow from {{ lan_subnet }} to any port 22 proto tcp",
           keep: true, position: first, before: ssh-limit}
        - {id: ssh-limit, spec: "limit 22/tcp", keep: true}
      legacy:
        - {spec: "allow 22/tcp", comment: "", replaced_by: ssh-limit}

Each `spec` is the rule exactly as `ufw show added` prints it after `ufw `, and each value renders
with the host's context. `keep` defaults to false, `position` to `last`, `before` to none and a
legacy `comment` to the empty string. Only a host package carries rules, because only its manifest
reaches reconcile.
"""

import sys as _sys
from collections.abc import Callable
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
from pathlib import Path
from typing import Protocol

from renderer.renderer_lib.cluster import RenderRefused

_UNIT = r"[A-Za-z0-9@_.:-]+\.(?:service|timer|socket|path)"
# The actions `hosts/lib/reconcile_lib/activate.py:ACTION` reads.
ACTION = re.compile(
    rf"none|sysctl|modules-load|reboot|(?:try-restart|reload|manual):{_UNIT}"
)
# The shapes `hosts/lib/reconcile_lib/ufw.py` reads.
UFW_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
UFW_SPEC = re.compile(r"(?:allow|deny|limit|reject) [A-Za-z0-9 ./:_-]+")
UFW_RULE_KEYS = frozenset({"id", "spec", "keep", "position", "before"})
UFW_LEGACY_KEYS = frozenset({"spec", "comment", "replaced_by"})


class Package(Protocol):
    """The part of `host.Package` these functions write, named so neither module imports the other."""

    activations: dict[str, str]
    ufw_rules: list[dict]
    ufw_legacy: list[dict]


def add_activation(package: Package, directory: Path, dst: str, action: object) -> None:
    """Record `action` for the file at `dst`; None records nothing.

    Raises:
        RenderRefused: `action` is not one reconcile knows.
    """
    if action is None:
        return
    if not isinstance(action, str) or not ACTION.fullmatch(action):
        raise RenderRefused(
            f"{directory}: {dst} activate {action!r} is not none, sysctl, modules-load, "
            "reboot, or try-restart, reload or manual followed by :<unit>"
        )
    package.activations[dst] = action


def add_ufw(
    package: Package,
    directory: Path,
    section: object,
    render: Callable[[str], str],
) -> None:
    """Record the rules and legacy rules a `ufw:` map declares, with each value rendered.

    Raises:
        RenderRefused: The map is malformed, or a value is one the arm would refuse.
    """
    if not isinstance(section, dict) or not set(section) <= {"rules", "legacy"}:
        raise RenderRefused(
            f"{directory}: ufw {section!r} is not a map of rules and legacy"
        )
    for entry in section.get("rules") or []:
        if (
            not isinstance(entry, dict)
            or not {"id", "spec"} <= set(entry) <= UFW_RULE_KEYS
        ):
            raise RenderRefused(
                f"{directory}: ufw rule {entry!r} needs id and spec only"
            )
        rule = {
            "id": entry["id"],
            "spec": render(str(entry["spec"])),
            "keep": entry.get("keep", False),
            "position": entry.get("position", "last"),
            "before": entry.get("before"),
        }
        if not (
            isinstance(rule["id"], str)
            and UFW_ID.fullmatch(rule["id"])
            and UFW_SPEC.fullmatch(rule["spec"])
            and isinstance(rule["keep"], bool)
            and rule["position"] in ("first", "last")
        ):
            raise RenderRefused(f"{directory}: ufw rule {rule!r} is malformed")
        package.ufw_rules.append(rule)
    for entry in section.get("legacy") or []:
        if (
            not isinstance(entry, dict)
            or not {"spec", "replaced_by"} <= set(entry) <= UFW_LEGACY_KEYS
        ):
            raise RenderRefused(
                f"{directory}: ufw legacy rule {entry!r} needs spec and replaced_by"
            )
        legacy = {
            "spec": render(str(entry["spec"])),
            "comment": render(str(entry.get("comment", ""))),
            "replaced_by": entry["replaced_by"],
        }
        if not UFW_SPEC.fullmatch(legacy["spec"]) or "'" in legacy["comment"]:
            raise RenderRefused(f"{directory}: ufw legacy rule {legacy!r} is malformed")
        package.ufw_legacy.append(legacy)


def ufw_section(name: str, rules: list[dict], legacy: list[dict]) -> dict | None:
    """The manifest's `ufw` object, or None when the package declares nothing.

    Raises:
        RenderRefused: Two rules share an id, a `before:` or a `replaced_by` names no rule.
    """
    if not rules and not legacy:
        return None
    ids = [rule["id"] for rule in rules]
    if len(ids) != len(set(ids)):
        raise RenderRefused(f"{name}: two ufw rules share an id")
    named = [rule["before"] for rule in rules if rule["before"]]
    named += [entry["replaced_by"] for entry in legacy]
    if set(named) - set(ids):
        raise RenderRefused(
            f"{name}: ufw before and replaced_by name {sorted(set(named) - set(ids))}, "
            "which no rule declares"
        )
    return {"rules": rules, "legacy": legacy}
