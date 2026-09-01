"""The stdlib manifest reader must agree with PyYAML on every manifest this repo renders.

`manifest_declares.py` runs on the host under `uv run --no-project`, which supplies no
third-party packages, so it reads the staged YAML with a stdlib parser that keys on document
structure rather than importing PyYAML. That trade is only safe if something proves the two
agree — otherwise a manifest shape the stdlib reader cannot see goes quiet on the host, in a
root cron whose failure mode is a false all-clear.

This is that proof. It renders every k8s manifest template in the repo, parses each one both
ways, and asserts the same `kind/name` set comes back. A template shape the reader cannot
handle fails here instead of silently widening the orphan check's blind spot.

The rest of the file is the accept/reject pairs for the reader's own rules: what it must
declare, and what it must NOT — the container, volume and key names whose false "still
declared" is the whole reason it replaced a grep.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from _helpers import REPO

_REPO = REPO
sys.path.insert(0, str(_REPO / "ansible/roles/setup/k3s/files"))

from _k8s_render import rendered_docs, rendered_texts  # noqa: E402
from manifest_declares import declared, declared_in  # noqa: E402


def _pyyaml_names(text: str) -> set[str]:
    """The same set PyYAML would produce, as the reference implementation."""
    names = set()
    for doc in yaml.safe_load_all(text):
        if not isinstance(doc, dict):
            continue
        kind, meta = doc.get("kind"), doc.get("metadata")
        if isinstance(kind, str) and isinstance(meta, dict):
            name = meta.get("name")
            if isinstance(name, str) and name:
                names.add(f"{kind.lower()}/{name}")
    return names


def test_the_reader_agrees_with_pyyaml_on_every_rendered_manifest():
    """The gap-closer: a shape the stdlib reader misses fails here, not on the host.

    Reads the rendered TEXT rather than a re-serialised doc. Round-tripping through
    yaml.safe_dump would normalise indentation, quoting and block scalars — the exact
    formatting a position-based reader could trip over — so it would test nothing.
    """
    mismatches = []
    for role, tpl, text in rendered_texts():
        mine, reference = declared_in(text), _pyyaml_names(text)
        if mine != reference:
            mismatches.append(
                f"{role}/{tpl}: reader saw {sorted(mine)}, PyYAML saw {sorted(reference)}"
            )
    assert not mismatches, "\n".join(mismatches)


def test_the_rendered_tree_is_not_empty():
    """Guards the test above: an empty corpus would pass it while proving nothing."""
    assert sum(1 for _ in rendered_texts()) > 50
    assert sum(1 for _ in rendered_docs()) > 50


# --- what it must declare ------------------------------------------------------------------


def test_a_plain_object_is_declared():
    text = "apiVersion: v1\nkind: Service\nmetadata:\n  name: bazarr\n"
    assert declared_in(text) == {"service/bazarr"}


def test_each_document_in_a_multi_doc_file_is_declared():
    text = (
        "kind: Service\nmetadata:\n  name: a\n"
        "---\n"
        "kind: Deployment\nmetadata:\n  name: b\n"
    )
    assert declared_in(text) == {"service/a", "deployment/b"}


def test_a_quoted_name_is_unquoted():
    assert declared_in('kind: Service\nmetadata:\n  name: "a-b"\n') == {"service/a-b"}


def test_a_trailing_comment_is_stripped():
    assert declared_in("kind: Service\nmetadata:\n  name: a # why\n") == {"service/a"}


def test_metadata_before_kind_still_pairs():
    assert declared_in("metadata:\n  name: a\nkind: Service\n") == {"service/a"}


# --- what it must NOT declare: the false negatives the grep produced ------------------------


def test_a_container_name_is_not_declared():
    """The documented failure: a container sharing a name hid a real orphan."""
    text = (
        "kind: Deployment\nmetadata:\n  name: sonarr\n"
        "spec:\n  template:\n    spec:\n      containers:\n        - name: exportarr\n"
    )
    assert declared_in(text) == {"deployment/sonarr"}


def test_a_volume_name_is_not_declared():
    text = (
        "kind: Deployment\nmetadata:\n  name: d\n"
        "spec:\n  template:\n    spec:\n      volumes:\n        - name: config\n"
    )
    assert declared_in(text) == {"deployment/d"}


def test_a_port_name_is_not_declared():
    text = "kind: Service\nmetadata:\n  name: s\nspec:\n  ports:\n    - name: web\n"
    assert declared_in(text) == {"service/s"}


def test_a_pod_templates_metadata_name_is_not_declared():
    text = (
        "kind: Deployment\nmetadata:\n  name: d\n"
        "spec:\n  template:\n    metadata:\n      name: inner\n"
    )
    assert declared_in(text) == {"deployment/d"}


def test_a_configmap_key_holding_yaml_is_not_declared():
    """An embedded manifest inside a ConfigMap key is data, not a staged object."""
    text = (
        "kind: ConfigMap\nmetadata:\n  name: cm\n"
        "data:\n  thing.yaml: |\n    kind: Service\n    metadata:\n      name: nested\n"
    )
    assert declared_in(text) == {"configmap/cm"}


def test_a_document_without_a_kind_declares_nothing():
    assert declared_in("metadata:\n  name: orphan\n") == set()


def test_a_document_without_a_name_declares_nothing():
    assert declared_in("kind: Service\nmetadata:\n  labels:\n    a: b\n") == set()


def test_a_comment_line_is_ignored():
    assert declared_in("# kind: Service\nkind: Job\nmetadata:\n  name: j\n") == {
        "job/j"
    }


# --- the directory walk ----------------------------------------------------------------------


def test_declared_reads_yaml_and_yml(tmp_path: Path):
    (tmp_path / "a.yaml").write_text("kind: Service\nmetadata:\n  name: a\n")
    (tmp_path / "b.yml").write_text("kind: Job\nmetadata:\n  name: b\n")
    names, errors = declared(tmp_path)
    assert names == {"service/a", "job/b"}
    assert errors == []


def test_declared_ignores_other_extensions(tmp_path: Path):
    (tmp_path / "notes.txt").write_text("kind: Service\nmetadata:\n  name: a\n")
    names, _ = declared(tmp_path)
    assert names == set()


def test_declared_recurses_into_per_service_subdirs(tmp_path: Path):
    sub = tmp_path / "bazarr"
    sub.mkdir()
    (sub / "svc.yaml").write_text("kind: Service\nmetadata:\n  name: bazarr\n")
    names, _ = declared(tmp_path)
    assert names == {"service/bazarr"}
