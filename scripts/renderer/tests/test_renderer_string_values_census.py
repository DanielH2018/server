"""Every Secret and ConfigMap value in a substituting directory stays a string unquoted (#5574).

`flux diff` refused autofix-bridge's flip on its Secret: `.stringData.INTERVAL: expected string,
got`. Each refused key was a quoted literal that YAML reads as a number or a boolean once its
quotes are gone, such as `"300"` and `"false"`. Something on the Flux side re-serialises a
substituting directory's Secret without the quotes. A Deployment's quoted `env` value survives
the same path, so a setting goes there and a Secret keeps only credentials.

The census reads the placeholder render of every render target, the render the flip publishes.
In each directory that substitutes, every value under a Secret's `stringData` or `data` and a
ConfigMap's `data` must resolve as a YAML string when written as a plain scalar. The empty
string is admitted: homepage's flux-owned `config-secret.yaml` carries `custom.js: ""`, and its
Kustomization read Ready=True with the pod at 0 restarts on 2026-10-18.

Run: uv run pytest scripts/renderer/tests/test_renderer_string_values_census.py
"""

import functools

import pytest
import yaml
from yaml.resolver import Resolver

from deploy_tools.render_targets import render_targets
from lib import yaml_fast
from lib.ansible_inventory import K8S_HOST
from renderer import homelab_render
from renderer.renderer_lib.cluster import RenderedTag
from renderer.renderer_lib.placeholders import (
    PlaceholderRender,
    apply_placeholders,
    render_placeholders,
    sentinel,
)

STRING_TAG = "tag:yaml.org,2002:str"
# The fields Flux re-serialises, by kind.
FIELDS = {"Secret": ("stringData", "data"), "ConfigMap": ("data",)}
# The two Secrets #5574 moved settings out of. The census must read both, or a walk that
# reached neither would pass.
MEMBERS = {
    ("autofix-bridge", "env-secret.yaml"),
    ("monitor-bridge", "env-secret.yaml"),
}
KEY = "probe_key"
_RESOLVER = Resolver()


def reads_as_string(value: object) -> bool:
    """Whether `value` is a string YAML still reads as one when it is written unquoted."""
    if not isinstance(value, str):
        return False
    if value == "":
        return True
    return _RESOLVER.resolve(yaml.ScalarNode, value, (True, False)) == STRING_TAG


def census(render: PlaceholderRender) -> tuple[list[str], set[tuple[str, str]]]:
    """Each offending value as `dir/file Kind/name field.key`, and each Secret file it read."""
    substituting = set(render.substituting())
    offences, read = [], set()
    for tag in render.tags:
        for directory, files in tag.directories().items():
            if directory not in substituting:
                continue
            for name, text in files.items():
                for doc in yaml_fast.safe_load_all(text):
                    if not isinstance(doc, dict) or doc.get("kind") not in FIELDS:
                        continue
                    if doc["kind"] == "Secret":
                        read.add((directory, name))
                    where = (
                        f"{directory}/{name} {doc['kind']}/{doc['metadata']['name']}"
                    )
                    for field in FIELDS[doc["kind"]]:
                        for key, value in (doc.get(field) or {}).items():
                            if not reads_as_string(value):
                                offences.append(f"{where} {field}.{key}")
    return offences, read


def _secret(string_data: str, kind: str = "Secret", field: str = "stringData") -> str:
    """A `kind` document holding `string_data` and a placeholder, so its directory substitutes."""
    return (
        f"apiVersion: v1\nkind: {kind}\nmetadata:\n  name: s\n{field}:\n"
        f'  TOKEN: "{sentinel(KEY)}"\n{string_data}'
    )


def _census(files: dict[str, str]) -> list[str]:
    return census(apply_placeholders([RenderedTag("t", files)], [KEY]))[0]


@pytest.mark.parametrize(
    ("kind", "field", "line", "offence"),
    [
        ("Secret", "stringData", '  INTERVAL: "300"\n', "stringData.INTERVAL"),
        ("Secret", "stringData", '  DRY_RUN: "false"\n', "stringData.DRY_RUN"),
        ("Secret", "stringData", '  RATIO: "0.90"\n', "stringData.RATIO"),
        ("Secret", "data", '  N: "1234"\n', "data.N"),
        ("ConfigMap", "data", '  GRACE: "3"\n', "data.GRACE"),
        ("ConfigMap", "data", '  NOTHING: "null"\n', "data.NOTHING"),
    ],
)
def test_a_numeric_looking_literal_in_a_substituting_directory_is_refused(
    kind, field, line, offence
):
    assert _census({"s.yaml": _secret(line, kind, field)}) == [
        f"t/s.yaml {kind}/s {offence}"
    ]


def test_strings_that_stay_strings_and_unsubstituted_literals_are_clean():
    clean = (
        "  TZ: America/Chicago\n"
        '  WINDOW: "30m"\n'
        '  EMPTY: ""\n'
        "  SCRIPT: |\n    import os\n    print(1)\n"
    )
    deployment = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: d\nspec:\n"
        '  env:\n    - name: INTERVAL\n      value: "300"\n'
    )
    assert _census({"s.yaml": _secret(clean), "d.yaml": deployment}) == []
    # A directory with no placeholder is applied with no substitution, so its quotes survive.
    unsubstituted = 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: s\nstringData:\n  INTERVAL: "300"\n'
    assert _census({"s.yaml": unsubstituted}) == []


@functools.cache
def _live() -> tuple[list[str], set[tuple[str, str]]]:
    tags = sorted(render_targets(K8S_HOST))
    return census(render_placeholders(tags, homelab_render.secret_keys()))


def test_no_substituting_directory_holds_a_value_that_unquotes_to_a_non_string():
    offences, read = _live()
    assert MEMBERS <= read, f"the census never read {sorted(MEMBERS - read)}"
    assert not offences, (
        "move each setting to the Deployment's `env:` as a `value:`; a Secret keeps only "
        "its credentials:\n" + "\n".join(offences)
    )
