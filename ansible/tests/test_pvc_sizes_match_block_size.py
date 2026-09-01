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

A CLAIM BUILT THROUGH `ansible/templates/pvc.yml.j2` HAS NO `storage:` LINE OF ITS OWN, and
until 2026-09-01 this file could not see one. Four roles had already adopted that macro —
authelia, crowdsec, karakeep, loki-homelab — so their sizes had never been checked here, while
the suite read green. `PVC_CALL_RE` closes that by reading the macro's third argument, taking
coverage from 21 declared sizes to 31. The floor assertion below is what surfaced it: five more
roles adopted the macro, resolution fell to 14, and the floor failed instead of the suite
quietly checking less. Keep the floor above the real count for that reason.

Run: uv run pytest ansible/tests/test_pvc_sizes_match_block_size.py
"""

import re
from pathlib import Path

import pytest
import yaml
from _helpers import ANSIBLE

K8S_ROLES = ANSIBLE / "roles" / "k8s"
K3S_DEFAULTS = ANSIBLE / "roles" / "setup" / "k3s" / "defaults" / "main.yml"

UNITS = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}
SIZE_RE = re.compile(r"^(\d+)(Ki|Mi|Gi|Ti)$")
STORAGE_RE = re.compile(r"^\s*storage:\s*(\S.*?)\s*$", re.M)
JINJA_RE = re.compile(r"^\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}$")
# A claim built through ansible/templates/pvc.yml.j2 has no `storage:` line of its own — the
# macro carries it. Its third argument is the size, so that is what this reads. Without it a
# converted role drops out of this suite silently, which is how the check would end up passing
# on almost nothing; `test_the_var_map_resolves_most_templated_sizes` is the floor that caught
# exactly that when the first five roles adopted the macro.
PVC_CALL_RE = re.compile(r"\bpvc\(\s*[^,()]+,\s*[^,()]+,\s*([^,()]+?)\s*\)")
IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


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
        text = template.read_text()
        for raw in STORAGE_RE.findall(text):
            resolved = raw
            match = JINJA_RE.match(raw)
            if match:
                resolved = str(variables.get(match.group(1), raw))
            found.append((template, raw, resolved))
        # The macro's third argument is already a bare expression, not `{{ ... }}`, so it
        # resolves against the same var map without the Jinja braces a `storage:` line carries.
        for raw in PVC_CALL_RE.findall(text):
            resolved = raw
            if IDENT_RE.match(raw):
                resolved = str(variables.get(raw, raw))
            elif len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                resolved = raw[1:-1]
            found.append((template, raw, resolved))
    return found


def test_the_var_map_resolves_most_templated_sizes() -> None:
    """Guard the guard: if resolution broke, every size would skip and the check would pass."""
    sizes = _declared_sizes()
    resolved = [s for _, _, s in sizes if SIZE_RE.match(s)]
    assert len(resolved) >= 22, (
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


# The macro-call extraction is itself a check, so it ships with a proof it can go red: one
# shape it must find, one it must not. A rule that matched nothing would leave every converted
# role unchecked while this file still passed — the exact state that held for four roles until
# 2026-09-01.
@pytest.mark.parametrize(
    "text",
    [
        "{{ pvc(registry_k8s_claim, registry_k8s_storage_class, registry_k8s_size) }}",
        "{% call pvc(mosquitto_k8s_claim, mosquitto_k8s_storage_class, mosquitto_k8s_size) %}",
        "{% call pvc('authelia-config', 'longhorn', authelia_k8s_storage) %}",
    ],
)
def test_the_macro_call_regex_finds_the_size_argument(text: str) -> None:
    """Accept case: every call form in the tree yields exactly one size expression."""
    found = PVC_CALL_RE.findall(text)
    assert len(found) == 1, f"{text!r} yielded {found}"
    assert found[0].endswith(("_size", "_storage", "_pvc_size"))


@pytest.mark.parametrize(
    "text",
    [
        "  storage: 128Mi",
        "{{ some_other_helper(a, b, c) }}",
        "{{ pvc(claim, storage_class) }}",
    ],
)
def test_the_macro_call_regex_ignores_a_non_call(text: str) -> None:
    """Reject case: a plain storage line, a different helper, and a two-argument call.

    The two-argument form matters because a `pvc()` call that lost its size argument is the
    one shape that would silently drop a claim from this suite while still rendering.
    """
    assert PVC_CALL_RE.findall(text) == []


def test_a_bad_size_behind_the_macro_is_still_caught() -> None:
    """The point of the extraction: a non-multiple size must fail even with no `storage:` line.

    100Mi is a multiple of 2Mi and not of 16Mi — the exact value that made pi-peer-backup a
    violator when the block size moved on 2026-08-19.
    """
    raw = PVC_CALL_RE.findall("{{ pvc(c, sc, widget_size) }}")[0]
    assert raw == "widget_size"
    size = 100 * UNITS["Mi"]
    assert size % block_size_bytes() != 0, (
        "100Mi is a multiple of the configured block size, so this reject case proves nothing "
        "— pick a size that is not, or the block size changed and this test needs rewriting."
    )
