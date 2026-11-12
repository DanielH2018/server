#!/usr/bin/env python3
"""Deployed host paths whose content holds a credential, derived from the host package specs.

WHY THIS EXISTS. `grep-on-a-script-prints-the-secret-it-embeds` records three occurrences, every
one ending in a rotation. The leak is an *inspection*, not an execution: a `grep -nE
"rotate|--commit|sops set|push"` on a deployed wrapper matched its own export of a push URL,
token included, and printed the token into the terminal and the transcript.

WHY A DERIVATION RATHER THAN A LIST. `block-dangerous-bash.sh` guards reads by matching a fixed
alternation of conventional secret paths — `.env`, `.ssh/`, `id_rsa`. A deployed secret file's
name is whatever its component called it, so it cannot be on that list. The set is knowable
from the tree, though, so it is measured rather than enumerated.

WHAT IT MEASURES. Host mode renders no secret into a component's `files:` entries: the renderer
withholds every secret key and refuses a template that reads one
(`renderer/renderer_lib/host.py`). A credential reaches a host file only through the two routes
`homelab-secrets` writes:

- an `outputs:` entry, whose `keys:` list names exactly the secrets the file holds. It renders
  under `lib.host_secret_roots.OUTPUT_ROOTS`, and its `path` is where the consumer reads it;
- a `credentials:` entry, which lands at `/run/credstore/<key>`.

Both are read from every `hosts/components/*/component.yml` and `hosts/*/package.yml`. A key the
rotation registry tiers `ignore` (an address, a bucket name) holds no credential and is left
out. The specs and the registry are plaintext by design, so this needs no SOPS key and runs
anywhere.

WHAT IT DOES NOT MEASURE. A secret a process writes at runtime, outside `homelab-secrets`, is
invisible here. This narrows the class; it does not close it.

Run: uv run python scripts/secrets_mgmt/secret_bearing_host_paths.py
"""

from collections.abc import Iterator, Sequence
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib import yaml_fast
from lib.cli_help import answer_help
from lib.host_secret_roots import CREDSTORE, OUTPUT_ROOTS

from lib.repo_paths import ANSIBLE, REPO

REGISTRY = ANSIBLE / "secret_rotation.yml"

# Every path this module returns starts with one of these. The hook short-circuits on the same
# literal before it imports anything, so a path outside them would be invisible to it.
SECRET_PATH_ROOTS = ("/etc/", "/run/", "/var/lib/", "/usr/lib/homelab/")

# The registry's own word for "tracked, but not a value anyone rotates". `email`, `r2_bucket`
# and the `*_username` keys carry it. A file whose only declared keys carry it holds no
# credential, and a guard that fires on such files is one that gets switched off.
NON_SECRET_TIERS = frozenset({"ignore"})


def non_secret_names(registry: _Path = REGISTRY) -> frozenset[str]:
    """The registry names tiered `ignore`: tracked for completeness, holding no credential."""
    data = yaml_fast.safe_load(registry.read_text()) or {}
    return frozenset(
        name
        for name, meta in (data.get("entries") or {}).items()
        if (meta or {}).get("tier") in NON_SECRET_TIERS
    )


def _specs(repo: _Path) -> Iterator[tuple[_Path, dict]]:
    """Each host package spec and component spec in `repo`, parsed."""
    hosts = repo / "hosts"
    for spec in sorted(
        [*hosts.glob("*/package.yml"), *hosts.glob("components/*/component.yml")]
    ):
        yield spec, yaml_fast.safe_load(spec.read_text()) or {}


def _output_paths(output: dict) -> list[str]:
    """Where an output's rendered content can be read: its rendered file and its `path`."""
    path = str(output["path"])
    destination = str(output.get("destination", path.lstrip("/")))
    rendered = f"{OUTPUT_ROOTS[bool(output.get('persist', False))]}/{destination}"
    return sorted({rendered, path})


def secret_bearing_host_paths(
    repo: _Path = REPO, registry: _Path = REGISTRY
) -> dict[str, list[str]]:
    """Deployed host path -> the secret names its content holds.

    Args:
      repo: the checkout whose `hosts/` specs are read.
      registry: the rotation registry whose `ignore` tier is left out. Both are parameters so
        a test can hand this a tree it built, rather than patching module globals.
    """
    ignored = non_secret_names(registry)
    found: dict[str, set[str]] = {}
    for _, spec in _specs(repo):
        for key in spec.get("credentials") or []:
            if str(key) not in ignored:
                found.setdefault(f"{CREDSTORE}/{key}", set()).add(str(key))
        for output in spec.get("outputs") or []:
            names = {str(k) for k in output.get("keys") or []} - ignored
            if not names:
                continue
            for path in _output_paths(output):
                found.setdefault(path, set()).update(names)
    return {path: sorted(names) for path, names in sorted(found.items())}


def readable_beyond_owner_and_group(mode) -> bool:
    """True when `mode` grants anything to `other`, or is absent."""
    if mode is None:
        return True
    return int(str(mode), 8) & 0o007 != 0


def world_readable_outputs(repo: _Path = REPO) -> list[tuple[str, str, str | None]]:
    """(spec, path, mode) for every declared secret output whose mode grants `other` bits."""
    return [
        (
            str(spec.relative_to(repo)),
            str(output["path"]),
            None if output.get("mode") is None else str(output["mode"]),
        )
        for spec, data in _specs(repo)
        for output in data.get("outputs") or []
        if readable_beyond_owner_and_group(output.get("mode"))
    ]


def main(argv: Sequence[str] = ()) -> int:
    answer_help(__doc__, list(argv))
    paths = secret_bearing_host_paths()
    for dest, names in sorted(paths.items()):
        print(f"{dest}\t{','.join(names)}")
    print(
        f"\n{len(paths)} deployed host paths hold a tracked secret.", file=_sys.stderr
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(_sys.argv[1:]))
