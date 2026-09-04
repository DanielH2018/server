"""Guards on the artifacts ConfigMap, which carries an embedded Python program.

validate/k8s_manifests.py asserts every manifest parses as YAML. That is not enough for this
one, and the gap put a crash-looping pod in the cluster: an indented Jinja comment between two
keys pushed the second key inside the first's `|` block scalar, so the rendered ConfigMap held
ONE key whose value was the script with `known_services.json: |` appended to it. That parses as
valid YAML — there is nothing malformed about it — and the pod died on
`SyntaxError: invalid syntax` instead.

So these assert on content: that every key survives the render, and that each embedded module
is still a parseable Python program. A YAML-only check cannot see either failure.

The keys come from `artifacts_modules` in the role's defaults rather than one `lookup('file')`
per module written out by hand, which is what caused both crash-loops. That list is compared
against the files on disk here, in both directions.

Run: uv run pytest ansible/tests/services/test_artifacts_configmap.py
"""

from __future__ import annotations

import ast
import json

from _helpers import REPO
from _k8s_render import rendered_docs

FILES = REPO / "ansible" / "roles" / "k8s" / "artifacts" / "files"

# The entry point plus the two modules it imports. Named rather than counted, so a census that
# stopped seeing the tree fails here instead of passing over an empty set.
REQUIRED_MODULES = frozenset({"_gui_html.py", "artifact_meta.py", "artifact_server.py"})


def _configmap() -> dict:
    for role, tpl, doc in rendered_docs():
        if role == "artifacts" and tpl == "configmap.yaml.j2":
            return doc
    raise AssertionError("the artifacts ConfigMap did not render")


def _module_keys() -> set[str]:
    return {key for key in _configmap()["data"] if key.endswith(".py")}


def _module_files() -> set[str]:
    return {path.name for path in FILES.glob("*.py")}


def divergence(shipped: set[str], on_disk: set[str]) -> list[str]:
    """What the ConfigMap ships against what `files/` holds, as readable problems.

    Pure, so the red-proof pair below can hand it a set the tree does not produce. Both
    directions matter and they fail differently, which is why each gets its own message.

    Args:
        shipped: The ConfigMap's `.py` keys, which are `artifacts_modules`.
        on_disk: The `.py` filenames under the role's `files/`.

    Returns:
        One line per divergence, empty when the two sets agree.
    """
    return [
        f"{name} is in files/ but not in artifacts_modules, so it never reaches the pod"
        for name in sorted(on_disk - shipped)
    ] + [
        f"{name} is in artifacts_modules but not in files/"
        for name in sorted(shipped - on_disk)
    ]


def test_every_key_survives_the_render() -> None:
    """The absorbed-key failure shows up here and nowhere else in the suite."""
    data = _configmap()["data"]
    assert set(data) == REQUIRED_MODULES | {"known_services.json"}, (
        "the key set changed: for a new module, update REQUIRED_MODULES above. Otherwise a "
        "key was absorbed into another key's block scalar — the render still parses as YAML, "
        f"so only this assertion catches that. Got keys: {sorted(data)}"
    )


def test_the_shipped_modules_are_the_ones_on_disk() -> None:
    """A module added to files/ but not to artifacts_modules never reaches the pod.

    The pod imports its siblings by plain module name off /app, so the missing one surfaces
    as ModuleNotFoundError at startup rather than as anything Ansible reports.

    The other direction rarely reaches this assertion: `artifacts_modules` naming a file that
    no longer exists makes `lookup('file')` raise while `rendered_docs()` renders the whole
    tree, so the failure arrives as that shared helper blowing up rather than as this test.

    `divergence` sees only `.py` names, because `_module_keys` filters the data keys to those.
    An `artifacts_modules` entry that is not a `.py` file is invisible here; the exact key set
    in test_every_key_survives_the_render is what catches that one.
    """
    on_disk = _module_files()
    assert REQUIRED_MODULES <= on_disk, REQUIRED_MODULES - on_disk
    assert divergence(_module_keys(), on_disk) == []


def test_a_module_missing_from_the_ship_list_is_flagged() -> None:
    """The red half: a key set short one real module must not compare equal."""
    on_disk = _module_files()
    assert divergence(_module_keys() - {"artifact_meta.py"}, on_disk) == [
        "artifact_meta.py is in files/ but not in artifacts_modules, so it never reaches "
        "the pod"
    ]


def test_a_ship_list_entry_with_no_file_is_flagged() -> None:
    """The other direction, which the render usually catches first — see above."""
    flagged = divergence(_module_keys() | {"artifact_gone.py"}, _module_files())
    assert flagged == ["artifact_gone.py is in artifacts_modules but not in files/"]


def test_every_embedded_module_is_parseable_python() -> None:
    """Anything appended to a module (a YAML key, stray prose from a broken comment) makes
    the pod exit on SyntaxError at startup. Parsing it here is the same check, before deploy."""
    data = _configmap()["data"]
    for key in sorted(_module_keys()):
        ast.parse(data[key], filename=key)


def test_the_service_list_is_json_and_holds_the_platform_names() -> None:
    """Longhorn is the most-discussed subject in the corpus and carries no containers_list
    entry — it is installed by roles/setup/k3s — so it only reaches the indexer through
    artifacts_platform_services. Without it the service facet misses its biggest term.

    Only the role's own lists are asserted. The validator stubs containers_list, so the
    workload names it contributes are absent from this render and present on a real deploy —
    asserting them here would pin the stub rather than the manifest.
    """
    names = json.loads(_configmap()["data"]["known_services.json"])
    assert "longhorn" in names
    assert "kopia" in names, (
        "retired names are missing, so older artifacts cannot be tagged"
    )


def test_service_names_are_unique_and_sorted() -> None:
    names = json.loads(_configmap()["data"]["known_services.json"])
    assert names == sorted(set(names))
