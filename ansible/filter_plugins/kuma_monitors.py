"""Ansible filter plugin deriving Uptime Kuma's ingress tiles from `containers_list` (#3690).

No template calls `kuma_ingress_monitors`, and no `containers_list` entry carries its `kuma`
key: `ingress_probes.py:ingress_probe_targets` feeds the probe job. M6's C-4b deletes this
plugin once D6 has removed the narrowing, which refuses a deleted plugin.

`kuma_malformed_push_tokens` returns the push declarations AutoKuma would refuse, whose
`push_token` is not 32 letters and digits (#3985).

`FilterModule` registers each filter through `_as_filter`, so a refusal reaches the operator as
`FilterError("<filter>: ...")` (#4505).
"""

import re

import sys as _sys
from pathlib import Path as _Path

# Ansible loads a plugin by file path, so `filter_errors` beside it imports by bare name only
# once this directory is on the path (#4527).
if str(_Path(__file__).resolve().parent) not in _sys.path:
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))

from filter_errors import _as_filter

# The keys a `kuma:` mapping may carry. A misspelt one would otherwise be ignored and the
# tile would keep its derived identity, which reads as the override having worked.
_KUMA_KEYS = frozenset({"id", "name"})

ID_SUFFIX = "-k8s"

# AutoKuma's own check, from its error text: "push token should be 32 characters and contain
# only letters and numbers". `secret_rotation.py rotate` mints `token_hex(16)`, which passes.
_PUSH_TOKEN = re.compile(r"[A-Za-z0-9]{32}")


def kuma_ingress_monitors(containers_list):
    """One ingress tile per `containers_list` entry with a `hostname`, in list order.

    Each tile is a dict: `id` (the AutoKuma id, without `.json`), `name` (the display name),
    `hostname`, `behind_sso` (whether Authelia answers first, so the probe expects its 302),
    and `entry` (the entry's `name`, for the template's tier lookup).

    Raises ValueError when an entry's `kuma` is neither `false` nor a mapping, or carries a key
    outside `id` and `name`.
    """
    tiles = []
    for entry in containers_list or []:
        if not isinstance(entry, dict) or not entry.get("hostname"):
            continue
        kuma = entry.get("kuma", {})
        if kuma is False:
            continue
        if not isinstance(kuma, dict):
            raise ValueError(
                f"containers_list entry {entry['name']!r}: `kuma` must be false or a mapping, "
                f"got {kuma!r}"
            )
        unknown = set(kuma) - _KUMA_KEYS
        if unknown:
            raise ValueError(
                f"containers_list entry {entry['name']!r}: unknown `kuma` keys "
                f"{sorted(unknown)}; known: {sorted(_KUMA_KEYS)}"
            )
        tiles.append(
            {
                "id": kuma.get("id", entry["name"]) + ID_SUFFIX,
                "name": kuma.get("name", f"k3s {entry['name']}"),
                "hostname": entry["hostname"],
                "behind_sso": bool(entry.get("auth_tier")),
                "entry": entry["name"],
            }
        )
    return tiles


def kuma_malformed_push_tokens(declarations):
    """The sorted ids of the push declarations AutoKuma would refuse for their token.

    `declarations` maps an AutoKuma id to its parsed entity. A push entity whose `push_token`
    is missing, not a string, or not exactly 32 ASCII letters and digits is returned. Only ids
    are returned, never a token, so a caller can print the result without leaking one.
    """
    return sorted(
        ident
        for ident, entity in declarations.items()
        if entity.get("type") == "push"
        and not (
            isinstance(entity.get("push_token"), str)
            and _PUSH_TOKEN.fullmatch(entity["push_token"])
        )
    )


class FilterModule:
    def filters(self):
        return {
            "kuma_ingress_monitors": _as_filter(kuma_ingress_monitors),
            "kuma_malformed_push_tokens": _as_filter(kuma_malformed_push_tokens),
        }
