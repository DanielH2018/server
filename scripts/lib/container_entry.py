"""The parsed form of `k8s/services.yml`'s `containers_list`, built once at the read.

`parse_containers_list` is the one place a malformed entry is caught. `render_guard.k8s_services`
calls it, and so does `render_context` for the Pi's `compose_services`, which three templates
pass to the same filters. Every template filter under `scripts/renderer/template_filters/` and
every Python reader of the list takes its result. So no filter re-checks an entry's shape, and one
bad entry fails the read rather than being skipped by one filter and crashing another.

`ENTRY_MODELS` holds a pydantic model per list naming every key its entries may carry, so a
misspelt key fails the read with its name rather than being ignored. A model only validates: the
`ContainerEntry` handed on is built from the YAML mapping itself, so no default, coercion or key
order of the model's reaches a template.

`ContainerEntry` is a `dict`, not a frozen dataclass. Templates read an entry through dot
access, `| default`, `in`, `combine` and `tojson`, and a role default's Jinja expands inside
an entry's values, so a type that was not a mapping would change what the roles render.
`lib.k8s_context.resolve_vars` rebuilds an entry as a `ContainerEntry`, so the type survives
the expansion.

Typical usage example:

    entries = parse_containers_list(yaml_fast.safe_load(text)["containers_list"])
"""

from typing import Annotated, Any

import pydantic
import yaml
from yaml.representer import SafeRepresenter

# The tiers an entry's `tier` may name, each with the PriorityClass its pods run at. One tier
# maps to exactly one class, which is why the house's services split in two: the edge and SSO
# run at homelab-critical, the automation chain one band below it. `platform` is critical
# without being home-critical (the registry, the UPS primary). A misspelt tier would silently
# drop the entry out of every set `renderer/template_filters/service_tier.py` derives, so the
# parse refuses an unknown value rather than reading it as untiered.
SERVICE_TIERS = {
    "home-edge": "homelab-critical",
    "home-automation": "homelab-automation",
    "platform": "homelab-critical",
}


def _known_tier(tier: str) -> str:
    if tier not in SERVICE_TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    return tier


_NAME = Annotated[str, pydantic.StringConstraints(min_length=1)]
_STRINGS = list[str] | None
_MAPPINGS = list[dict[str, Any]] | None

# The keys each list's entries may carry, as `pydantic.create_model` fields: (type, default).
_COMPOSE_FIELDS: dict[str, Any] = {
    "name": (_NAME, ...),
    "port": (int | None, None),
    "udp_port": (int | None, None),
    "use_authelia": (bool | None, None),
    "networks": (_STRINGS, None),
}
_SERVICE_FIELDS: dict[str, Any] = {
    "name": (_NAME, ...),
    "tier": (Annotated[str, pydantic.AfterValidator(_known_tier)] | None, None),
    "tags": (_STRINGS, None),
    "platform": (str | None, None),
    "depends_on": (_STRINGS, None),
    "port": (int | None, None),
    "udp_port": (int | None, None),
    "service_port_name": (str | None, None),
    "service_extra_ports": (_MAPPINGS, None),
    "hostname": (str | None, None),
    "extra_hostnames": (_STRINGS, None),
    "public_route": (bool | None, None),
    "use_authelia": (bool | None, None),
    "auth_tier": (str | None, None),
    "bridge_bypass_prefixes": (_STRINGS, None),
    "default_dns": (str | None, None),
    "probe": (bool | None, None),
    "metrics": (_MAPPINGS, None),
    "homepage_widget": (bool | None, None),
    "netpol_app": (str | None, None),
    "netpol_from": (_STRINGS, None),
    "netpol_fences": (_MAPPINGS, None),
    "netpol_role_owned": (bool | None, None),
    "backup_claims": (_STRINGS, None),
    "no_backup_claims": (_STRINGS, None),
    "r2_backup_claims": (_STRINGS, None),
    "weekly_backup_claims": (dict[str, int] | None, None),
}
_STRICT = pydantic.ConfigDict(extra="forbid", strict=True)

# The model a list's entries are validated against, by the list's key in its file.
ENTRY_MODELS: dict[str, type[pydantic.BaseModel]] = {
    "containers_list": pydantic.create_model(
        "ServiceEntry", __config__=_STRICT, **_SERVICE_FIELDS
    ),
    "compose_services": pydantic.create_model(
        "ComposeEntry", __config__=_STRICT, **_COMPOSE_FIELDS
    ),
}


class ContainerEntryError(ValueError):
    """A `containers_list` entry that no reader can use."""


class ContainerEntry(dict):
    """One `containers_list` entry: a mapping with a non-empty `name` and a known `tier`.

    Only `parse_containers_list` builds one from YAML. `name` is the only attribute: it never
    collides with a key a template reads differently, because every entry carries the key.
    """

    @property
    def name(self) -> str:
        return self["name"]


# SafeDumper refuses a dict subclass it has no representer for; `json` needs no such entry.
for _dumper in (yaml.SafeDumper, getattr(yaml, "CSafeDumper", None)):
    if _dumper is not None:
        _dumper.add_representer(ContainerEntry, SafeRepresenter.represent_dict)


def parse_containers_list(
    raw, key: str = "containers_list"
) -> tuple[ContainerEntry, ...]:
    """`raw`'s entries as `ContainerEntry` values, in list order. A null list reads as empty.

    Args:
        raw: The list as YAML loaded it.
        key: The list's name in its file, one of `ENTRY_MODELS`: it picks the entry model and
            names the list in an error. The Pi's `compose_services` passes its own, since the
            template filters read it too.

    Raises:
        ContainerEntryError: `raw` is not a list, or an entry is not a mapping, has no
            non-empty string `name`, repeats another entry's `name`, declares a `tier`
            outside `SERVICE_TIERS`, carries a key its model does not name, or gives a key
            a value of the wrong type.
    """
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ContainerEntryError(f"{key} must be a list, got {type(raw).__name__}")
    entries: list[ContainerEntry] = []
    seen: set[str] = set()
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ContainerEntryError(
                f"{key} item {position} must be a mapping, got {item!r}"
            )
        try:
            ENTRY_MODELS[key].model_validate(item)
        except pydantic.ValidationError as exc:
            raise ContainerEntryError(_refusal(key, position, item, exc)) from exc
        name = item["name"]
        if name in seen:
            raise ContainerEntryError(
                f"{key} names {name!r} twice; a lookup by name would read the first"
            )
        seen.add(name)
        entries.append(ContainerEntry(item))
    return tuple(entries)


def _refusal(key: str, position: int, item: dict, exc: pydantic.ValidationError) -> str:
    """The message for `item`'s first validation error, naming the entry and the key."""
    error = exc.errors()[0]
    field = str(error["loc"][0])
    if field == "name":
        return f"{key} item {position} has no `name`: {item!r}"
    name = item["name"]
    if field == "tier":
        return (
            f"{key} entry {name!r} declares unknown tier {item['tier']!r}; "
            f"known: {', '.join(SERVICE_TIERS)}"
        )
    if error["type"] == "extra_forbidden":
        return f"{key} entry {name!r} has unknown key {field!r}"
    return f"{key} entry {name!r} key {field!r}: {error['msg']}"
