#!/usr/bin/env python3
"""Every ConfigMap/Secret key written in a template must survive the render as a real key.

A key can be swallowed by the value above it. Indent a Jinja comment (or the key itself) one
level too far and the key line lands *inside* the preceding `|` block scalar, so the rendered
manifest holds one key whose value is the previous value with `known_services.json: |` tacked
onto the end. Nothing about that is malformed YAML, which is why the existing validator cannot
see it: `scripts/validate_k8s_manifests.py` parses the manifest and then parses the embedded
blob (lines 234-258), and both parses stay green either way. The failure only shows up at
runtime — on 2026-08-16 the artifacts pod crash-looped on `SyntaxError: invalid syntax`,
because the ConfigMap key had been appended to the Python program it sits next to.

The check here is deliberately one-directional: every key name written at the `data:` indent in
the SOURCE must appear as a key in the PARSED render. Absorption removes a key from the parsed
result while leaving the source line untouched, so it fails this and nothing else. Extra
rendered keys are fine: `homepage/icons-configmap.yaml.j2` builds all six of its keys in a
`{% for %}` loop, so it writes no literal key at all and is the one template this covers
nothing of.

Two narrowings, both because the source is a template and not YAML:

* Keys nested inside `{% if %}` / `{% for %}` are not checked. `uptime-kuma`'s
  `static-monitors.yaml.j2` gates three monitors on a token being present, and the validator's
  stub context leaves those tokens empty — so the keys are correctly absent from the render and
  a naive check calls it absorption. Losing them costs little: absorption is a whole-file
  indentation slip, and every one of those files still has unconditional keys being checked.
* Counting `<name>: |` block-scalar openers instead was tried first and rejected. It splits on
  the same conditionals, and it misses the plain-string keys that sit next to block scalars in
  the same `data:` map, so the two counts disagree for honest reasons.

`test_artifacts_configmap.py` stays as the specific case for the ConfigMap that actually broke:
it also asserts the embedded script still parses as Python, which is the symptom absorption
produced there and is not generalisable.

Run: uv run pytest ansible/tests/test_configmap_keys_not_absorbed.py
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pytest
from _k8s_render import rendered_docs
from validate_k8s_manifests import K8S_ROLES

ANSIBLE = Path(__file__).resolve().parents[1]

DATA_RE = re.compile(r"^(\s*)(data|stringData|binaryData):\s*$")
KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9][A-Za-z0-9._\-]*):(\s|$)")
BLOCK_OPEN_RE = re.compile(r"^\s*\{%-?\s*(if|for)\b")
BLOCK_CLOSE_RE = re.compile(r"^\s*\{%-?\s*end(if|for)\b")
RAW_OPEN_RE = re.compile(r"^\s*\{%-?\s*raw\b")
RAW_CLOSE_RE = re.compile(r"^\s*\{%-?\s*endraw\b")

KEY_FIELDS = ("data", "stringData", "binaryData")


def unconditional_source_keys(template: Path) -> list[str]:
    """Key names written directly under a `data:` mapping, outside any Jinja block."""
    keys: list[str] = []
    data_indent: int | None = None
    depth = 0
    in_raw = False
    in_comment = False

    for line in template.read_text().splitlines():
        stripped = line.strip()
        # Several roles carry a multi-line `{# ... #}` header inside the data map. Its middle
        # lines are indented prose, so without this they read as dedents and stop the scan.
        if in_comment:
            in_comment = "#}" not in line
            continue
        if "{#" in line and "#}" not in line[line.index("{#") :]:
            in_comment = True
            continue
        if in_raw:
            in_raw = not RAW_CLOSE_RE.match(line)
            continue
        if RAW_OPEN_RE.match(line):
            in_raw = True
            continue
        if BLOCK_OPEN_RE.match(line):
            depth += 1
            continue
        if BLOCK_CLOSE_RE.match(line):
            depth = max(0, depth - 1)
            continue
        if data_indent is None:
            match = DATA_RE.match(line)
            if match and depth == 0:
                data_indent = len(match.group(1))
            continue
        # `{{ lookup('file', ...) | indent(4, true) }}` at column 0 is how a role embeds a file
        # as a key's value. It is not a dedent out of the `data:` map, so it must not reset the
        # scan — before this line was skipped, every key after the first embedded file was
        # dropped, which silently un-covered home-assistant's ten keys and left it checking one.
        if not stripped or stripped.startswith(("{%", "{#", "{{")):
            continue
        match = KEY_RE.match(line)
        if match and len(match.group(1)) == data_indent + 2:
            if depth == 0:
                keys.append(match.group(2))
            continue
        if len(line) - len(line.lstrip()) <= data_indent:
            data_indent = None
    return keys


def rendered_keys_by_template() -> dict[tuple[str, str], set[str]]:
    keys: dict[tuple[str, str], set[str]] = defaultdict(set)
    for role, tpl, doc in rendered_docs():
        if doc.get("kind") not in ("ConfigMap", "Secret"):
            continue
        for field in KEY_FIELDS:
            if isinstance(doc.get(field), dict):
                keys[(role, tpl)].update(doc[field])
    return keys


RENDERED = rendered_keys_by_template()


def cases() -> list[tuple[Path, str, str, list[str]]]:
    """(template, role, template name, source keys) for every template the render covers."""
    rendered = RENDERED
    found = []
    for role_dir in sorted(d for d in K8S_ROLES.iterdir() if d.is_dir()):
        for template in sorted(role_dir.glob("templates/*.j2")):
            key = (role_dir.name, template.name)
            if key not in rendered:
                # The role is skipped by the validator, or the template holds no ConfigMap.
                continue
            source = unconditional_source_keys(template)
            if source:
                found.append((template, role_dir.name, template.name, source))
    return found


CASES = cases()


def test_the_scan_finds_keys_across_most_configmaps() -> None:
    """Guard the guard: a broken scan reports no keys and every assertion below vacuously passes."""
    templates = len(CASES)
    keys = sum(len(k) for _, _, _, k in CASES)
    # Measured 2026-08-21: 51 templates, 321 keys. The floors sit just under that, high enough
    # to catch the two ways this scan has already lost coverage silently: stopping at a
    # multi-line `{# ... #}` header dropped it to 49/312, and stopping at the first column-0
    # `{{ lookup(...) }}` line dropped home-assistant from ten checked keys to one. Both still
    # left the great majority of templates passing, which is why the floor is close to the
    # measured value rather than a round number well below it.
    assert templates >= 50, (
        f"only {templates} templates carry scanned keys — the source scan broke"
    )
    assert keys >= 300, f"only {keys} keys are being checked — the source scan broke"


@pytest.mark.parametrize(
    ("template", "role", "name", "source_keys"),
    [pytest.param(t, r, n, k, id=f"{r}/{n}") for t, r, n, k in CASES],
)
def test_no_source_key_is_absorbed_into_the_value_above_it(
    template: Path, role: str, name: str, source_keys: list[str]
) -> None:
    rendered = RENDERED.get((role, name), set())
    missing = [k for k in source_keys if k not in rendered]
    assert not missing, (
        f"{template.relative_to(ANSIBLE)} writes {missing} at the data indent, but the "
        "rendered ConfigMap has no such key — it was absorbed into the block scalar above it "
        "(check the indentation of the preceding key, its value, and any Jinja comment between "
        f"them). The render still parses as YAML, so no other check sees this. Rendered keys: "
        f"{sorted(rendered)}"
    )
