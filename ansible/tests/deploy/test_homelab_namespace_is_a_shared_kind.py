"""The shared kind `homelab-namespace` renders the `homelab` Namespace and its LimitRange.

It is their only writer. ADR-0020 records the decision.

Run: uv run pytest ansible/tests/deploy/test_homelab_namespace_is_a_shared_kind.py
"""

from collections.abc import Iterable

from lib import yaml_fast

from renderer.renderer_lib.bundle import FLUX_INDEX, SHARED_KINDS, bundle_files
from renderer.renderer_lib.namespace import HOMELAB_NAMESPACE


def namespace_kind_problems(files: dict[str, str], kinds: Iterable[str]) -> list[str]:
    """One line per way `homelab-namespace` is missing from a bundle or from `kinds`."""
    problems = []
    if HOMELAB_NAMESPACE not in set(kinds):
        problems.append(f"{HOMELAB_NAMESPACE} is not in SHARED_KINDS")
    flux = f"flux/{HOMELAB_NAMESPACE}.yaml"
    if flux not in files:
        problems.append(f"{flux} is not rendered")
    elif (
        f"{HOMELAB_NAMESPACE}.yaml"
        not in yaml_fast.safe_load(files[FLUX_INDEX])["resources"]
    ):
        problems.append(f"{FLUX_INDEX} does not list {HOMELAB_NAMESPACE}.yaml")
    rendered = {
        (doc["kind"], doc["metadata"]["name"])
        for path, text in files.items()
        if path.startswith(f"cluster/{HOMELAB_NAMESPACE}/")
        for doc in yaml_fast.safe_load_all(text)
        if isinstance(doc, dict) and "metadata" in doc
    }
    for kind, name in (
        ("Namespace", "homelab"),
        ("LimitRange", "default-container-limits"),
    ):
        if (kind, name) not in rendered:
            problems.append(f"cluster/{HOMELAB_NAMESPACE}/ renders no {kind} {name}")
    return problems


def _shared_bundle() -> dict[str, str]:
    return bundle_files({}, {}, [], "600s", "x=ENC[y]\n")


def test_homelab_namespace_is_rendered_and_a_shared_kind():
    assert namespace_kind_problems(_shared_bundle(), SHARED_KINDS) == []


def test_a_bundle_without_homelab_namespace_is_flagged():
    files = {
        path: text
        for path, text in _shared_bundle().items()
        if HOMELAB_NAMESPACE not in path
    }
    assert namespace_kind_problems(files, SHARED_KINDS - {HOMELAB_NAMESPACE}) == [
        f"{HOMELAB_NAMESPACE} is not in SHARED_KINDS",
        f"flux/{HOMELAB_NAMESPACE}.yaml is not rendered",
        f"cluster/{HOMELAB_NAMESPACE}/ renders no Namespace homelab",
        f"cluster/{HOMELAB_NAMESPACE}/ renders no LimitRange default-container-limits",
    ]
