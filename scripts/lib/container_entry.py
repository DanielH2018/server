"""The parsed form of `k8s/services.yml`'s `containers_list`, built once at the read.

`parse_containers_list` is the one place a malformed entry is caught. `render_guard.k8s_services`
calls it, and so does `render_context` for the Pi's `compose_services`, which three templates
pass to the same filters. Every template filter under `scripts/renderer/template_filters/` and
every Python reader of the list takes its result. So no filter re-checks an entry's shape, and one
bad entry fails the read rather than being skipped by one filter and crashing another.

`ContainerEntry` is a `dict`, not a frozen dataclass. Templates read an entry through dot
access, `| default`, `in`, `combine` and `tojson`, and a role default's Jinja expands inside
an entry's values, so a type that was not a mapping would change what the roles render.
`lib.k8s_context.resolve_vars` rebuilds an entry as a `ContainerEntry`, so the type survives
the expansion.

Typical usage example:

    entries = parse_containers_list(yaml_fast.safe_load(text)["containers_list"])
"""

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
        key: The list's name in its file, for the error message. The Pi's
            `compose_services` passes its own, since the template filters read it too.

    Raises:
        ContainerEntryError: `raw` is not a list, or an entry is not a mapping, has no
            non-empty string `name`, repeats another entry's `name`, or declares a `tier`
            outside `SERVICE_TIERS`.
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
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ContainerEntryError(f"{key} item {position} has no `name`: {item!r}")
        if name in seen:
            raise ContainerEntryError(
                f"{key} names {name!r} twice; a lookup by name would read the first"
            )
        seen.add(name)
        tier = item.get("tier")
        if tier is not None and tier not in SERVICE_TIERS:
            raise ContainerEntryError(
                f"{key} entry {name!r} declares unknown tier {tier!r}; "
                f"known: {', '.join(SERVICE_TIERS)}"
            )
        entries.append(ContainerEntry(item))
    return tuple(entries)
