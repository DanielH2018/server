"""Render uptime-kuma's static monitor tiles, for the setup-plane guards that assert on one.

A host cron's guard lives under `ansible/tests/setup/` and its Kuma tile is declared across the
plane boundary, in `roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2`. Four such guards
read that template, and each used to read its SOURCE: the tile's JSON with `{{ ... }}` still in
it, substituted to `0` before the parse, plus a substring assertion that the `{% if %}` gate
spells the right token variable. Both claims decay the way #3178 measured — a tile field that
moves into a role default leaves the parse reading a `0` that means nothing, and a gate spelled
any other way leaves the substring matching nothing, which passes (#3202).

Every push tile is gated on its own `*_push_token` being non-empty, and no token resolves to a
value in a render, so a tile read at inventory values is a tile that is not in the render at
all. That is what `services/_kuma_entities.py` is exempt from the render rule for. This module
takes the other route: it renders the template twice per tile, once with the token set to a
sentinel and once with it empty, which turns the gate from a substring into an observation —
the tile is there with the token and gone without it. The claim is strictly stronger, because
it fails on a gate keyed to the wrong variable as well as on a missing one.

`render_role_template` renders this one template rather than the whole tree, so a guard that
needs three renders does not pay three whole-tree renders.
"""

import json
import re

from _k8s_render import render_role_template

ROLE = "uptime-kuma"
TEMPLATE = "static-monitors.yaml.j2"

# A value no secret holds, so "the tile rendered" cannot be satisfied by an inventory value.
SENTINEL = "kuma-render-sentinel-token"


def monitors_text(overrides: dict | None = None) -> str:
    """The rendered static-monitors Secret, `overrides` laid over uptime-kuma's context."""
    return render_role_template(ROLE, TEMPLATE, overrides or {})


def entity(filename: str, token_var: str) -> dict:
    """The tile declared under `filename`, parsed from a render with `token_var` armed.

    Fails naming the tile when the render does not carry it, so a renamed tile or a gate keyed
    to another variable reads as a failure rather than as a guard over an empty dict.
    """
    text = monitors_text({token_var: SENTINEL})
    match = re.search(rf"^  {re.escape(filename)}: \|\n\s+(\{{.*\}})$", text, re.M)
    assert match, (
        f"{filename} is not in the rendered {TEMPLATE} with {token_var} set — the tile is "
        "gone, renamed, or gated on a different variable"
    )
    return json.loads(match.group(1))


def tile_is_gated_on(filename: str, token_var: str) -> bool:
    """Whether `filename` drops out of the render when `token_var` is empty.

    An ungated tile sits red from creation until the secret exists, so the gate is the claim.
    False means the tile rendered anyway, which is the defect.
    """
    pattern = rf"^  {re.escape(filename)}: \|"
    return not re.search(pattern, monitors_text({token_var: ""}), re.M)
