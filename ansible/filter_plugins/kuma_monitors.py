"""Ansible filter plugin deriving Uptime Kuma's ingress tiles from `containers_list` (#3690).

Every entry with a `hostname` is served at `https://<hostname>.local.<domain>`, and a tile
probing that URL needs nothing the entry does not already carry: the hostname gives the URL,
`auth_tier` gives the expected status, and the entry's `tier` gives the alerting through the
template's `service_alerting` macro. So uptime-kuma's `static-monitors.yaml.j2` loops over
`containers_list | kuma_ingress_monitors` instead of repeating each hostname by hand.

An entry opts out with `kuma: false`. Two kinds of entry do, and each says which in a comment:
one whose tile is hand-written because it probes something else (a path, a VIP, a ClusterIP,
an expected status Authelia does not set), and one with no tile yet.

`kuma: {id: <stem>, name: <display name>}` overrides the derived identity. Both exist for the
estate that predates the derivation, whose ids and names were hand-picked:

- The AutoKuma id is `<stem>-k8s`, and the stem defaults to the entry's `name`. AutoKuma runs
  with `ON_DELETE=delete` (#2076), so a changed id deletes the monitor and its history. The
  `-k8s` suffix is forced, not defaulted, because the status page's `Services` group rule
  matches `-k8s$` (uptime-kuma's `uptime_kuma_k8s_status_page_groups`); a generated tile can
  therefore never land outside a named group.
- The display name defaults to `k3s <name>`. It is what Kuma shows and what `probe.py
  kuma-drift` reconciles against Prometheus, but it is not the identity.

`kuma_malformed_push_tokens` guards the other direction: AutoKuma refuses a push declaration
whose `push_token` is not 32 letters and digits, logs one WARN per sync, and never creates the
monitor (#3985). The pusher then gets HTTP 404 and nothing pages. uptime-kuma's tasks assert
over it so the deploy fails instead.

No Ansible import, so `probe_lib/kuma_table_loop.py` and the status-page census read the same
function the playbook runs. Ansible wraps a filter's `ValueError` in its own error.
"""

import re

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
            "kuma_ingress_monitors": kuma_ingress_monitors,
            "kuma_malformed_push_tokens": kuma_malformed_push_tokens,
        }
