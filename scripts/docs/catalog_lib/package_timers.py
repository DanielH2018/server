"""The timers the host packages ship, read from each package as the renderer builds it.

Rendering rather than parsing `component.yml` resolves each `when:` and Jinja schedule the way
the package does, so a timer a toggle drops is absent here too. Secrets stay out of it: the
renderer reads only the key names of `secrets.yml`.

Typical usage example:

    for timer in package_timers():
        print(timer.unit, timer.host, cadence_minutes(timer.triggers))
"""

import functools
import re
import sys as _sys
from dataclasses import dataclass
from pathlib import Path as _Path
from pathlib import PurePosixPath

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

from lib import yaml_fast
from lib.systemd_timer import calendar_fires, span_s, timer_settings
from renderer.renderer_lib import host as host_mode

UNIT_DIR = PurePosixPath("/usr/lib/systemd/system")
_MINUTES_BY_CALENDAR_WORD = {"weekly": 10080.0, "daily": 1440.0, "hourly": 60.0}
_ONE_WEEKDAY = re.compile(r"[A-Z][a-z]{2} \*-\*-\* \S+( \S+)?")


@dataclass(frozen=True)
class PackageTimer:
    """One timer unit as one host receives it.

    Attributes:
        unit: The unit's name, such as `homelab-docs-refresh.timer`.
        host: The host whose package set ships it.
        triggers: Each systemd trigger key mapped to its values, drop-ins applied.
        enabled: Whether the host's packages ship the timer's enable link.
        service: The rendered text of the service the timer starts, empty if none ships.
        source: The repo path that declares the timer.
        role: The `role:` of the component that ships it, None for any other source.
    """

    unit: str
    host: str
    triggers: dict[str, list[str]]
    enabled: bool
    service: str
    source: str
    role: str | None


def _component_timers(hosts_root: _Path) -> dict[str, tuple[str, str | None]]:
    """Each timer's name mapped to the file declaring it and its component's `role:`."""
    found: dict[str, tuple[str, str | None]] = {}
    for spec in sorted(hosts_root.glob("components/*/component.yml")):
        data = yaml_fast.safe_load(spec.read_text()) or {}
        for entry in data.get("files") or []:
            dst = PurePosixPath(str(entry.get("dst", "")))
            if dst.parent == UNIT_DIR and dst.suffix == ".timer":
                source = f"hosts/components/{spec.parent.name}/{entry['src']}"
                found[dst.name] = (source, data.get("role"))
    return found


def _unit_texts(packages: list[host_mode.Package]) -> dict[str, str]:
    """Every file under the unit directory across `packages`, keyed by its path below it."""
    return {
        PurePosixPath(dst).relative_to(UNIT_DIR).as_posix(): body.decode()
        for package in packages
        for dst, (body, _mode) in package.files.items()
        if PurePosixPath(dst).is_relative_to(UNIT_DIR)
    }


def host_timers(
    host: str,
    own: host_mode.Package,
    shared: list[host_mode.Package],
    declared: dict[str, tuple[str, str | None]],
) -> list[PackageTimer]:
    """The timers one host receives from its own package and the shared ones.

    A shared package's timer counts only where the host's packages link it, because a shared
    package ships the unit to hosts that leave it unarmed.

    Args:
        host: The host's name.
        own: The host's rendered `Package`.
        shared: The rendered shared `Package`s.
        declared: What `_component_timers` returns.

    Returns:
        One `PackageTimer` per timer unit, sorted by unit name.
    """
    packages = [*shared, own]
    links = {link for package in packages for link in package.enable_links()}
    texts = _unit_texts(packages)
    own_units = set(_unit_texts([own]))
    timers = []
    for name in sorted(n for n in texts if n.endswith(".timer")):
        if name not in own_units and name not in links:
            continue
        dropins = [
            texts[path] for path in sorted(texts) if path.startswith(f"{name}.d/")
        ]
        triggers, _delay = timer_settings(texts[name], dropins)
        owner = next(p for p in reversed(packages) if name in _unit_texts([p]))
        fallback = f"hosts/{owner.directory.removeprefix('hosts/')}/package.yml"
        source, role = declared.get(name, (fallback, None))
        timers.append(
            PackageTimer(
                unit=name,
                host=host,
                triggers=triggers,
                enabled=name in links,
                service=texts.get(name.removesuffix(".timer") + ".service", ""),
                source=source,
                role=role,
            )
        )
    return timers


@functools.cache
def package_timers(hosts_root: _Path = host_mode.HOSTS) -> tuple[PackageTimer, ...]:
    """Every timer on every host that has a `hosts/<host>/package.yml`.

    Raises:
        RenderRefused: A package spec is malformed.
        RenderError: A package's template failed to render.
    """
    shared = [
        host_mode.render_package(spec.parent.name, hosts_root=hosts_root)
        for spec in sorted(hosts_root.glob("packages/*/package.yml"))
    ]
    declared = _component_timers(hosts_root)
    timers: list[PackageTimer] = []
    for spec in sorted(hosts_root.glob("*/package.yml")):
        host = spec.parent.name
        own = host_mode.render_host(host, hosts_root=hosts_root)
        timers.extend(host_timers(host, own, shared, declared))
    return tuple(timers)


def cadence_minutes(triggers: dict[str, list[str]]) -> float | None:
    """The approximate interval between a timer's fires, None for a shape it cannot size.

    Args:
        triggers: A `PackageTimer.triggers` map.
    """
    for key in ("OnUnitActiveSec", "OnUnitInactiveSec"):
        if key in triggers:
            return span_s(triggers[key][0]) / 60
    calendars = triggers.get("OnCalendar")
    if not calendars:
        return None
    expression = calendars[0]
    if expression in _MINUTES_BY_CALENDAR_WORD:
        return _MINUTES_BY_CALENDAR_WORD[expression]
    if _ONE_WEEKDAY.fullmatch(expression):
        return 10080.0
    try:
        return 1440.0 / len(calendar_fires(expression))
    except ValueError:
        return None
