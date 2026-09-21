#!/usr/bin/env python3
"""Fetch every pinned download in the roles' `defaults/` and check its checksum.

A Renovate PR that bumps a version used to build a download URL is green whether or not the
URL it wrote exists: nothing in CI fetches it. The 2026-09-02 case (`renovate-prs` skill, §3)
was a `jellyfin-ani-sync` bump that left a 404 URL beside a checksum from the old release,
with green CI. The skill answered with two `curl` lines the operator ran by hand per PR; this
script is those lines over every pin at once (#2163).

A pin is a URL and the digest the role checks the download against. Two shapes carry one:

  * A flat pair in `defaults/main.yml`: `<prefix>_url` beside `<prefix>_sha256` or
    `<prefix>_md5` (`jellyfin_k8s_anisync_url` / `_md5`, `k3s_install_script_url` /
    `_sha256`).
  * A list of mappings each carrying `url` and `sha256`/`md5` (`valheim_k8s_mods`).

A URL may reference other keys of the same file (`{{ k3s_version }}`); it is rendered from that
file alone, and one that does not resolve is reported as a failure, never skipped. A digest key
with no URL beside it (`optimize_pi_node_exporter_sha256`, whose `get_url` lives in `tasks/`) is
listed as unpaired so the gap is visible; it does not fail the run.

`KNOWN_PINS` names pins the census must find. A scanner that finds its subjects by pattern
returns an empty set the moment the keys are renamed, and a loop over nothing passes — the
non-vacuity rule in CLAUDE.md's *Python & Tests*.

This is NOT a pytest and not in `run_all.py`: the suite runs under `-p leakguard`, which fails
any test that reaches the network, and `run_all` is what a broad change runs offline. The
census (`discover_pins`) is what the tests cover; the fetch is what you run by hand or from
the skill.

Usage:
    uv run python scripts/validate/asset_pins.py               # fetch and check every pin
    uv run python scripts/validate/asset_pins.py --list        # census only, no network
    uv run python scripts/validate/asset_pins.py --only jellyfin_k8s_anisync,k3s_install_script
    uv run python scripts/validate/asset_pins.py --skip hypervisor_staging_vm_image  # 600 MB

Exit codes: 0 every checked pin matched; 1 at least one pin failed (the report names each);
64 a `--only` name the census does not carry.
"""

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path as _Path

# `lib` is a sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import jinja2

from lib import yaml_fast
from lib.repo_paths import ROLES

DIGEST_KEYS = ("sha256", "md5")
FETCH_TIMEOUT = 60.0
CHUNK = 1 << 20

# Pins the census must find, by name. A member going missing names what moved. Cover every
# shape and every plane: a flat `_md5` pair, a flat `_sha256` pair whose URL is templated, a
# setup-role pin and a list-shaped pin.
KNOWN_PINS = frozenset(
    {
        "jellyfin_k8s_anisync",
        "jellyfin_k8s_introskipper",
        "k3s_install_script",
        "k3s_host_coredns",
        "chezmoi_setup_installer",
        "valheim_k8s_mods[Server_devcommands]",
    }
)


@dataclass(frozen=True)
class Pin:
    """One download the tree checks against a digest.

    Attributes:
        name: the defaults key prefix, or `<list key>[<item name>]` for a list entry.
        url: the rendered URL, or the raw template when `error` is set.
        algorithm: `sha256` or `md5`.
        digest: the pinned hex digest, lower-cased.
        source: the defaults file the pin was read from, relative to the repo.
        error: why the pin cannot be fetched (an unresolved template), else None.
    """

    name: str
    url: str
    algorithm: str
    digest: str
    source: str
    error: str | None = None


def _render(template: str, context: dict) -> tuple[str, str | None]:
    """Render `{{ var }}` references against the same defaults file; (value, error)."""
    if "{{" not in template:
        return template, None
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
    try:
        return env.from_string(template).render(context), None
    except (jinja2.UndefinedError, jinja2.TemplateSyntaxError) as exc:
        return template, f"unresolved template: {exc}"


def _digest_of(mapping: dict, prefix: str = "") -> tuple[str, str] | None:
    """(algorithm, digest) from `<prefix>sha256`/`<prefix>md5` in `mapping`, else None."""
    for algorithm in DIGEST_KEYS:
        value = mapping.get(f"{prefix}{algorithm}")
        if isinstance(value, str) and value.strip():
            return algorithm, value.strip().lower()
    return None


def pins_in(defaults: dict, source: str) -> tuple[list[Pin], list[str]]:
    """The pins one defaults mapping carries, and the digest keys it carries with no URL.

    Args:
        defaults: the parsed `defaults/main.yml`.
        source: how to name the file in a report.

    Returns:
        `(pins, unpaired)` — `unpaired` lists `<prefix>_sha256`/`_md5` keys with no
        `<prefix>_url` beside them.
    """
    pins: list[Pin] = []
    unpaired: list[str] = []
    for key, value in defaults.items():
        if key.endswith("_url") and isinstance(value, str):
            prefix = key[: -len("_url")]
            found = _digest_of(defaults, f"{prefix}_")
            if found is None:
                continue
            algorithm, digest = found
            url, error = _render(value, defaults)
            pins.append(Pin(prefix, url, algorithm, digest, source, error))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                    continue
                found = _digest_of(item)
                if found is None:
                    continue
                algorithm, digest = found
                label = str(item.get("name", index))
                url, error = _render(item["url"], defaults)
                pins.append(
                    Pin(f"{key}[{label}]", url, algorithm, digest, source, error)
                )
    paired = {pin.name for pin in pins}
    for key in defaults:
        for algorithm in DIGEST_KEYS:
            suffix = f"_{algorithm}"
            if key.endswith(suffix) and key[: -len(suffix)] not in paired:
                unpaired.append(key)
    return pins, unpaired


def discover_pins(roles: _Path = ROLES) -> tuple[list[Pin], list[str]]:
    """Every pin under `<roles>/*/*/defaults/main.yml`, and the unpaired digest keys."""
    pins: list[Pin] = []
    unpaired: list[str] = []
    for defaults_file in sorted(roles.glob("*/*/defaults/main.yml")):
        loaded = yaml_fast.safe_load(defaults_file.read_text())
        if not isinstance(loaded, dict):
            continue
        source = str(defaults_file.relative_to(roles.parent.parent))
        found, missing = pins_in(loaded, source)
        pins.extend(found)
        unpaired.extend(f"{source}: {key}" for key in missing)
    return pins, unpaired


# ── the fetch ───────────────────────────────────────────────────────────────────────────────

Fetcher = Callable[[str], tuple[int, Iterator[bytes]]]


def fetch(url: str) -> tuple[int, Iterator[bytes]]:
    """GET `url` following redirects; (status, chunks). A 4xx/5xx is a status, not a raise."""
    request = urllib.request.Request(url, headers={"User-Agent": "homelab-asset-pins"})
    try:
        response = urllib.request.urlopen(request, timeout=FETCH_TIMEOUT)
    except urllib.error.HTTPError as exc:
        return exc.code, iter(())

    def chunks() -> Iterator[bytes]:
        with response:
            while block := response.read(CHUNK):
                yield block

    return response.status, chunks()


def check_pin(pin: Pin, fetcher: Fetcher = fetch) -> str | None:
    """Fetch `pin.url` and compare; None when it matches, else the reason it does not."""
    if pin.error:
        return pin.error
    try:
        status, chunks = fetcher(pin.url)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return f"fetch failed: {exc}"
    if status != 200:
        return f"HTTP {status} (must be 200)"
    hasher = hashlib.new(pin.algorithm)
    for block in chunks:
        hasher.update(block)
    actual = hasher.hexdigest()
    if actual != pin.digest:
        return f"{pin.algorithm} {actual} does not match the pinned {pin.digest}"
    return None


def check_pins(pins: Iterable[Pin], fetcher: Fetcher = fetch, out=sys.stdout) -> int:
    """Check each pin, print one line per pin, and return the count that failed."""
    failures = 0
    for pin in pins:
        reason = check_pin(pin, fetcher)
        if reason is None:
            print(f"ok    {pin.name}  {pin.url}", file=out)
        else:
            failures += 1
            print(
                f"FAIL  {pin.name}  {pin.url}\n      {reason}  ({pin.source})", file=out
            )
    return failures


# ── cli ─────────────────────────────────────────────────────────────────────────────────────


def _name_set(value: str | None) -> set[str]:
    return {name.strip() for name in (value or "").split(",") if name.strip()}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--list", action="store_true", help="print the census and exit")
    parser.add_argument(
        "--only", help="comma-separated pin names to check (default: all)"
    )
    parser.add_argument("--skip", help="comma-separated pin names to leave out")
    return parser


def main(
    argv: list[str] | None = None,
    fetcher: Fetcher = fetch,
    out=sys.stdout,
    discover: Callable[[], tuple[list[Pin], list[str]]] = discover_pins,
    known: frozenset[str] = KNOWN_PINS,
) -> int:
    """The CLI. `fetcher`, `discover` and `known` are the seams a test hands fakes to."""
    args = _build_parser().parse_args(argv)
    pins, unpaired = discover()
    names = {pin.name for pin in pins}
    missing_known = known - names
    if missing_known:
        print(
            f"census is missing pins it must find: {sorted(missing_known)} — "
            "a key was renamed or the scanner stopped matching",
            file=out,
        )
        return 1
    unknown = _name_set(args.only) - names
    if unknown:
        print(
            f"unknown pin name(s): {sorted(unknown)}; --list prints the census",
            file=out,
        )
        return 64
    only, skip = _name_set(args.only), _name_set(args.skip)
    chosen = [p for p in pins if (not only or p.name in only) and p.name not in skip]
    if args.list:
        for pin in chosen:
            flag = f"  [{pin.error}]" if pin.error else ""
            print(f"{pin.name}  {pin.algorithm}  {pin.url}{flag}", file=out)
        for key in unpaired:
            print(f"unpaired digest (URL not in defaults): {key}", file=out)
        return 0
    failures = check_pins(chosen, fetcher, out)
    for key in unpaired:
        print(f"unpaired digest (URL not in defaults): {key}", file=out)
    print(f"{len(chosen) - failures}/{len(chosen)} pin(s) match", file=out)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
