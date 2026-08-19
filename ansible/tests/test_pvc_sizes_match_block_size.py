#!/usr/bin/env python3
"""Every Longhorn PVC size must be an integer multiple of the backup block size.

Longhorn's admission webhook enforces this, and it started mattering on 2026-08-19 when
`default-backup-block-size` moved from 2 MiB to 16 MiB. A 100Mi PVC is a multiple of 2 MiB and
not of 16 MiB, so it provisioned happily before the change and is refused after it:

    admission webhook "validator.longhorn.io" denied the request: volume size 104857600 must
    be an integer multiple of the backup block size 16777216

The failure mode is the dangerous part. Existing volumes are untouched — the constraint is
checked at *creation*. So a violating PVC keeps serving indefinitely and only fails when
something recreates it: a node rebuild, a restore, a disaster-recovery bring-up. That is
precisely the path the 16 MiB change exists to make survivable, so a silent violation here
converts a cost optimisation into a recovery failure.

`pi-peer-backup` was the one violator, found by probing with a throwaway 100Mi PVC rather than
by reading the setting. It holds the Pi's un-rebuildable WireGuard peer keys.

Run: uv run pytest ansible/tests/test_pvc_sizes_match_block_size.py
"""

import re
from pathlib import Path

import pytest
import yaml

ANSIBLE = Path(__file__).resolve().parents[1]
K8S_ROLES = ANSIBLE / "roles" / "k8s"
K3S_DEFAULTS = ANSIBLE / "roles" / "setup" / "k3s" / "defaults" / "main.yml"

UNITS = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}
SIZE_RE = re.compile(r"^(\d+)(Ki|Mi|Gi|Ti)$")
STORAGE_RE = re.compile(r"^\s*storage:\s*(\S.*?)\s*$", re.M)
JINJA_RE = re.compile(r"^\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}$")


def block_size_bytes() -> int:
    mib = yaml.safe_load(K3S_DEFAULTS.read_text())["k3s_longhorn_backup_block_size"]
    return int(mib) * UNITS["Mi"]


def _vars() -> dict:
    """Every plain scalar var Ansible would have in scope, flattened into one map.

    Sizes are set in role defaults and group_vars, never inline, so resolving a `{{ var }}`
    means merging those. Collisions do not matter here: two roles never disagree about the
    value of a size var, and if they did, both values get checked across the two files.
    """
    merged: dict = {}
    sources = list(K8S_ROLES.glob("*/defaults/main.yml"))
    sources += list((ANSIBLE / "inventory").rglob("*.yml"))
    for path in sources:
        try:
            loaded = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        if isinstance(loaded, dict):
            merged.update(
                {k: v for k, v in loaded.items() if isinstance(v, (str, int))}
            )
    return merged


def _declared_sizes() -> list[tuple[Path, str, str]]:
    """(template, raw value, resolved value) for every `storage:` in a k8s manifest template."""
    variables = _vars()
    found = []
    for template in sorted(K8S_ROLES.glob("*/templates/*.j2")):
        for raw in STORAGE_RE.findall(template.read_text()):
            resolved = raw
            match = JINJA_RE.match(raw)
            if match:
                resolved = str(variables.get(match.group(1), raw))
            found.append((template, raw, resolved))
    return found


def test_the_var_map_resolves_most_templated_sizes() -> None:
    """Guard the guard: if resolution broke, every size would skip and the check would pass."""
    sizes = _declared_sizes()
    resolved = [s for _, _, s in sizes if SIZE_RE.match(s)]
    assert len(resolved) >= 15, (
        f"only {len(resolved)} of {len(sizes)} declared sizes resolved to a literal — "
        "variable resolution is broken, so this suite is checking almost nothing"
    )


@pytest.mark.parametrize(
    ("template", "raw", "resolved"),
    [
        pytest.param(t, r, s, id=f"{t.parent.parent.name}/{t.name}:{r}")
        for t, r, s in _declared_sizes()
    ],
)
def test_pvc_size_is_a_multiple_of_the_backup_block_size(
    template: Path, raw: str, resolved: str
) -> None:
    match = SIZE_RE.match(resolved)
    if not match:
        pytest.skip(f"{raw} is not a literal quantity (shell placeholder or a path)")
    size = int(match.group(1)) * UNITS[match.group(2)]
    block = block_size_bytes()
    assert size % block == 0, (
        f"{template.relative_to(ANSIBLE)} requests {resolved}, which is not an integer "
        f"multiple of the {block // UNITS['Mi']}Mi backup block size. Longhorn's admission "
        "webhook refuses to create it — the existing volume keeps working, so this only "
        "fails when the volume is recreated, i.e. during a rebuild or a restore."
    )
