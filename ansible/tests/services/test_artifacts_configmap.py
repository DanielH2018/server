"""Guards on the artifacts ConfigMap, which carries an embedded Python program.

validate_k8s_manifests.py asserts every manifest parses as YAML. That is not enough for this
one, and the gap put a crash-looping pod in the cluster: an indented Jinja comment between two
keys pushed the second key inside the first's `|` block scalar, so the rendered ConfigMap held
ONE key whose value was the script with `known_services.json: |` appended to it. That parses as
valid YAML — there is nothing malformed about it — and the pod died on
`SyntaxError: invalid syntax` instead.

So these assert on content: that both keys survive the render, and that the embedded script is
still a parseable Python program. A YAML-only check cannot see either failure.

Run: uv run pytest ansible/tests/services/test_artifacts_configmap.py
"""

from __future__ import annotations

import ast
import json

from _k8s_render import rendered_docs


def _configmap() -> dict:
    for role, tpl, doc in rendered_docs():
        if role == "artifacts" and tpl == "configmap.yaml.j2":
            return doc
    raise AssertionError("the artifacts ConfigMap did not render")


def test_both_keys_survive_the_render() -> None:
    """The absorbed-key failure shows up here and nowhere else in the suite."""
    data = _configmap()["data"]
    assert set(data) == {"artifact_server.py", "known_services.json"}, (
        "a key was absorbed into another key's block scalar — the render still parses as "
        f"YAML, so only this assertion catches it. Got keys: {sorted(data)}"
    )


def test_the_embedded_script_is_parseable_python() -> None:
    """Anything appended to the script (a YAML key, stray prose from a broken comment) makes
    the pod exit on SyntaxError at startup. Parsing it here is the same check, before deploy."""
    source = _configmap()["data"]["artifact_server.py"]
    ast.parse(source)


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
