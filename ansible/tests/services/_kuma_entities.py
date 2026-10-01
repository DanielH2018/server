"""The rendered AutoKuma static entity set, as the POD receives it, shared by the guards over it.

Every monitor, notification and tag reaches the pod as one key of the `static-monitors` Secret
that `static-monitors.yaml.j2` renders. The render here starts from the same resolved inventory a
deploy of daniel-box starts from — `_k8s_render.host_context()` plus the role's defaults — so a
guard asserting on a VIP, a hostname or a runbook URL states what the cluster gets rather than
what a stub dict said. A hand-written stub set had drifted from the inventory it stood in for:
`slzb_ip` was pinned at an address the coordinator had moved off, and the jellyfin LAN-VIP tile
rendered with an EMPTY hostname, which is a tile that monitors nothing while reading as covered.

Three things the inventory alone cannot supply, each handled rather than stubbed by hand:

- **A SOPS secret is undefined in a render.** `NamedStub` renders one as `stub-<variable name>`,
  so two secret-shaped values stay DISTINCT. That is what lets a guard join a tile to the
  monitor-bridge env-secret that feeds it on the rendered value, instead of regex-scanning both
  templates for the variable name they share.
- **A `| default('')`-gated tile renders AWAY when its token is undefined**, because Jinja's
  `default` filter replaces the undefined before `NamedStub` is ever consulted. Eleven tiles —
  the drift checks, the status-page sync, both UPS secondaries, both Loki read routes — left the
  census that way, so every guard here silently stopped covering them. `_token_seed()` seeds every
  `*_push_token` the two templates name, derived from the templates rather than listed, so a new
  gated tile cannot narrow the census again. The names are a render INPUT, never an assertion.
- **`hostvars` is a play fact.** It is built from the inventory's own `host_vars/` files, which is
  where the Pi's address and the primary node's `containers_list` live.

Kept as a `_`-prefixed sibling for the same reason `_homepage_config.py` is one: pytest prepends a
test's own directory to `sys.path`, so each guard reaches this by bare name without it being
collected as a test itself.
"""

import json
import re
from functools import lru_cache

from jinja2 import ChainableUndefined

from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env, register_ansible_filters
from _helpers import ANSIBLE
from _k8s_render import host_context
from validate.k8s_manifests import (
    K8S_ROLES,
    SHARED_TPL,
    k8s_entries,
    load_yaml,
    make_lookup,
    role_defaults,
)

TEMPLATE = ANSIBLE / "roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2"
BRIDGE_ENV_SECRET = ANSIBLE / "roles/k8s/monitor-bridge/templates/env-secret.yaml.j2"
_HOST = "daniel-box"

# Tiles that exist only because `_token_seed()` seeds their gate. Named rather than counted: a
# count that fell from 115 to 104 would read as a template edit, where a named member names the
# gate that stopped opening. These five are the shapes the gate hides — a drift check, the sync
# CronJob, a per-host secondary — so one of them going missing is the whole class going missing.
GATED_MEMBERS = frozenset(
    {
        "manifest-prune-check.json",
        "status-page-sync.json",
        "ups-secondary.json",
        "loki-read-route-daniel-box.json",
        "etcd-snapshot-offbox.json",
    }
)

# The resend intervals come from the role's real defaults, not from a stub. Stubbing them would
# make every assertion below a statement about this file rather than about what deploys — the
# same "measured the wrong artifact" shape these guards exist to catch. A stubbed 360 would have
# reported a healthy resend on a monitor the role actually holds at 0.
ROLE_DEFAULTS = yaml_fast.safe_load(
    (ANSIBLE / "roles/k8s/uptime-kuma/defaults/main.yml").read_text()
)

_STUB_PREFIX = "stub-"


class NamedStub(ChainableUndefined):
    """An undefined that renders as `stub-<variable name>` instead of a shared filler.

    `render_guard.StubUndefined` renders every undefined as the one word `STUB`, which collapses
    44 distinct push tokens onto a single value and with them any join keyed on one. Carrying the
    name keeps the values distinct AND keeps them non-credential-shaped, so a failure message
    quoting one leaks nothing.
    """

    def __str__(self) -> str:
        return _STUB_PREFIX + (self._undefined_name or "anonymous")


def _token_seed() -> dict[str, str]:
    """Every `*_push_token` the two templates name, mapped to its `NamedStub` rendering.

    A seed, not an assertion: the names decide which gated tiles render at all, and a tile that
    does not render is a guard that covers nothing. Both templates are scanned because the join
    between a tile and its bridge feeder needs the same value on both sides.
    """
    names: set[str] = set()
    for template in (TEMPLATE, BRIDGE_ENV_SECRET):
        names |= set(re.findall(r"\b([a-z0-9_]+_push_token)\b", template.read_text()))
    assert len(names) >= 40, f"the push-token scan went thin: {sorted(names)}"
    return {name: _STUB_PREFIX + name for name in names}


def _hostvars() -> dict[str, dict]:
    """The inventory's `host_vars/` as a play's `hostvars` fact, for the three homelab hosts.

    The Pi's tiles index `hostvars['daniel-pi'].server_ip` and the bridge-probe tiles read the
    primary node's `containers_list`, so without this both render against an undefined and the
    tiles carry a stub where an address belongs.
    """
    return {
        host: load_yaml(ANSIBLE / "inventory/host_vars" / f"{host}.yml")
        for host in ("daniel-pi", "daniel-box", "daniel-server")
    }


def _render(role: str, template: str, overrides: dict | None = None) -> str:
    """One of the two templates, rendered from daniel-box's resolved inventory.

    Role defaults go UNDER the inventory, which is Ansible's own precedence, and `overrides` goes
    over both the way `-e` does — a guard that needs to see what MOVES when a variable moves
    passes it here.
    """
    base = host_context(_HOST)
    ctx: dict = {
        **role_defaults(role, base),
        **base,
        "container_item": k8s_entries()[role],
        "playbook_dir": str(ANSIBLE),
        "hostvars": _hostvars(),
        **_token_seed(),
        **(overrides or {}),
    }
    env = make_ansible_env(
        [K8S_ROLES / role / "templates", SHARED_TPL], undefined_cls=NamedStub
    )
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    # Macros imported from `ansible/templates/` read the context off globals, not off the render
    # call, the same way `render_or_error` hands it to them.
    env.globals.update(ctx)
    return env.get_template(template).render(**ctx)


def _parse_entities(rendered: str) -> dict[str, dict]:
    secret = yaml_fast.safe_load(rendered)
    assert secret.get("kind") == "Secret", (
        f"{TEMPLATE.name} no longer renders a Secret, got {secret.get('kind')!r}"
    )
    entities = {name: json.loads(body) for name, body in secret["stringData"].items()}
    missing = GATED_MEMBERS - set(entities)
    assert not missing, (
        f"the census lost {sorted(missing)} — a `| default('')`-gated tile renders away when its "
        "push token is unseeded, and every guard over it then passes on nothing"
    )
    return entities


def _entities() -> dict[str, dict]:
    """entity id (the Secret key, `<name>.json`) -> the parsed AutoKuma entity."""
    return _entities_at_inventory_values()


@lru_cache(maxsize=1)
def _entities_at_inventory_values() -> dict[str, dict]:
    return _parse_entities(_render("uptime-kuma", "static-monitors.yaml.j2"))


def entities_with(overrides: dict) -> dict[str, dict]:
    """The same entity set rendered with `overrides` laid over the inventory.

    For a guard asking which tiles a variable MOVES — the rendered equivalent of asking which
    tiles name it. Not cached: each caller passes its own overrides.
    """
    return _parse_entities(_render("uptime-kuma", "static-monitors.yaml.j2", overrides))


@lru_cache(maxsize=1)
def bridge_env() -> dict[str, str]:
    """monitor-bridge's env-secret as the bridge pod receives it: env var -> value.

    Every threshold, window and cadence the loop reads is one key here, so a guard comparing a
    tile's deadline against the loop that feeds it reads the rendered value rather than regexing
    the template for it.
    """
    secret = yaml_fast.safe_load(_render("monitor-bridge", "env-secret.yaml.j2"))
    assert secret.get("kind") == "Secret", (
        f"{BRIDGE_ENV_SECRET.name} no longer renders a Secret, got {secret.get('kind')!r}"
    )
    data = secret["stringData"]
    assert "INTERVAL" in data, "the bridge env-secret declares no INTERVAL"
    return data


def bridge_push_tokens() -> set[str]:
    """The push-token VALUES monitor-bridge itself pushes to.

    Derived from the rendered env-secret, not listed: several monitors carry a `monitor_bridge_*`
    token name while being fed by something else entirely (CrowdSec Home Allowlist is fed by a
    cron on daniel-box, and the two Pi monitors and Arr Auto-Block are not bridge checks). A
    hand-kept list here would put those on the bridge's heartbeat window and relax a tile whose
    feeder runs on another clock.
    """
    tokens = {
        value
        for key, value in bridge_env().items()
        if key.startswith("KUMA_PUSH_") and value
    }
    assert len(tokens) >= 40, f"the bridge's push-token census went thin: {len(tokens)}"
    return tokens


def push_tokens_by_monitor() -> dict[str, str]:
    """monitor display name -> the push token the tile renders, for every push tile."""
    return {
        entity["name"]: entity.get("push_token", "")
        for entity in _entities().values()
        if entity["type"] == "push"
    }


def domain() -> str:
    """`domain` at the value the render used, for a guard naming a routed hostname."""
    return host_context(_HOST)["domain"]
