"""Properties of `checksum/restart`: where `stamp` inserts it and what `restart_values` hashes.

`stamp` runs on generated multi-document streams in both indents, with comment and blank
lines spliced in, CRLF line ends and `---` separators. `restart_values` runs on one target
Deployment whose refs come from a pool of ConfigMaps and Secrets, some of them repeated in a
second namespace. The property tests run under the profile `tests/hypothesis_profiles.py`
loads.

Run: uv run pytest scripts/renderer/tests/manifests/test_renderer_restart_properties.py
"""

import copy
import difflib
import re
import string
from typing import NamedTuple

import pytest
import yaml
from hypothesis import assume, example, given
from hypothesis import strategies as st

from lib import yaml_fast
from renderer.renderer_lib.environment import RenderError
from renderer.renderer_lib.restart import (
    RESTART_ANNOTATION,
    WORKLOAD_KINDS,
    reloaded_in_place,
    restart_values,
    stamp,
)

NAMES = ("one", "two", "three")
_STRUCTURE = frozenset({"kind", "metadata", "name", "spec", "template", "annotations"})
_WORD = st.text(string.ascii_lowercase + string.digits, min_size=1, max_size=8)
_SCALAR = st.one_of(
    _WORD,
    st.integers(-5, 5),
    st.booleans(),
    st.text(string.ascii_letters + " :#-'\"", max_size=12),
)
_DIGEST = st.text("0123456789abcdef", min_size=64, max_size=64)
_INSERTED = re.compile(
    rf' *(metadata:|annotations:|{RESTART_ANNOTATION}: "[0-9a-f]{{64}}")\n'
)


@st.composite
def _mapping(draw, required: dict, min_extras: int = 0) -> dict:
    """`required` plus a few random keys, in a random order."""
    extras = draw(
        st.dictionaries(
            _WORD.filter(lambda k: k not in _STRUCTURE),
            _SCALAR,
            min_size=min_extras,
            max_size=3,
        )
    )
    return dict(draw(st.permutations([*required.items(), *extras.items()])))


@st.composite
def _document(draw) -> dict:
    """A workload or a ConfigMap, each with a pod template, its metadata optional."""
    template = {"spec": {"containers": [{"name": "app"}]}}
    if draw(st.booleans()):
        metadata = {}
        if draw(st.booleans()):
            metadata["annotations"] = draw(_mapping({}, min_extras=1))
        template["metadata"] = draw(_mapping(metadata, min_extras=0 if metadata else 1))
    doc = {
        "kind": draw(st.sampled_from(sorted(WORKLOAD_KINDS | {"ConfigMap"}))),
        "spec": draw(_mapping({"template": draw(_mapping(template))})),
    }
    if draw(st.integers(0, 3)):
        doc["metadata"] = draw(_mapping({"name": draw(st.sampled_from(NAMES))}))
    return draw(_mapping(doc))


@st.composite
def _stream(draw) -> str:
    """A multi-document YAML stream of `_document`s, laid out as a hand-edited file might be."""
    texts = []
    for doc in draw(st.lists(_document(), min_size=1, max_size=4)):
        indent = draw(st.sampled_from((2, 4)))
        lines = yaml.safe_dump(doc, indent=indent, sort_keys=False, width=1000)
        spliced = []
        for line in lines.splitlines(keepends=True):
            spliced.append(line)
            spliced += draw(
                st.lists(
                    st.sampled_from(("\n", "# note\n", "    # indented\n")), max_size=2
                )
            )
        texts.append("".join(spliced))
    text = draw(st.sampled_from(("", "---\n"))) + "---\n".join(texts)
    return text.replace("\n", "\r\n") if draw(st.booleans()) else text


def _expected(text: str, values: dict[str, str]) -> list:
    """The documents of `text`, each named workload carrying its annotation."""
    docs = copy.deepcopy(list(yaml_fast.safe_load_all(text)))
    for doc in docs:
        name = (doc.get("metadata") or {}).get("name")
        if doc["kind"] in WORKLOAD_KINDS and name in values:
            template = doc["spec"]["template"]
            annotations = template.setdefault("metadata", {}).setdefault(
                "annotations", {}
            )
            annotations[RESTART_ANNOTATION] = values[name]
    return docs


@given(_stream(), st.dictionaries(st.sampled_from(NAMES), _DIGEST, min_size=1))
def test_stamp_adds_one_annotation_per_named_workload_and_moves_no_other_byte(
    text, values
):
    out = stamp(text, values, "fixture")

    assert list(yaml_fast.safe_load_all(out)) == _expected(text, values)
    before, after = text.splitlines(keepends=True), out.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    inserted = []
    for op, _, _, j1, j2 in matcher.get_opcodes():
        assert op in ("equal", "insert")
        inserted += after[j1:j2] if op == "insert" else []
    assert all(_INSERTED.fullmatch(line) for line in inserted)
    if out != text:
        with pytest.raises(RenderError, match="already carries"):
            stamp(out, values, "fixture")


TARGET = "app"
NAMESPACES = ("home", "other")
REFS = (
    ("ConfigMap", "cfg-a"),
    ("ConfigMap", "cfg-b"),
    ("ConfigMap", "prometheus-rules"),
    ("Secret", "sec-a"),
    ("Secret", "sec-b"),
)
VIA = ("envFrom", "env", "volume")
KEYS = ("alpha_token", "beta_token", "gamma_token")
SOURCE = {key: digit * 64 for key, digit in zip(KEYS, "abc", strict=True)}
ADMITS = (
    frozenset({"ConfigMap"}),
    frozenset({"Secret"}),
    frozenset({"ConfigMap", "Secret"}),
)


class Placed(NamedTuple):
    """One document of a render and the directory and file that hold it."""

    directory: str
    file: str
    doc: dict


class Case(NamedTuple):
    """A render as placed documents, the same documents in another order, and its target.

    Attributes:
        placed: The render's documents, in the order `_directories` lays them out.
        permuted: The same documents in another order.
        reads: Each `(kind, name)` the target's pod template names.
        admitted: The kinds the target admits into its value.
    """

    placed: tuple[Placed, ...]
    permuted: tuple[Placed, ...]
    reads: frozenset[tuple[str, str]]
    admitted: frozenset[str]


def _object(kind: str, name: str, namespace: str, key: str | None, note: str) -> dict:
    data = {"note": note, **({"token": f"${{{key}}}"} if key else {})}
    return {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": namespace},
        ("stringData" if kind == "Secret" else "data"): data,
    }


def _workload(
    name: str, namespace: str, refs: list[tuple[tuple[str, str], str]]
) -> dict:
    env_from, env, volumes = [], [], []
    for (kind, ref), via in refs:
        if via == "envFrom":
            field = "configMapRef" if kind == "ConfigMap" else "secretRef"
            env_from.append({field: {"name": ref}})
        elif via == "env":
            field = "configMapKeyRef" if kind == "ConfigMap" else "secretKeyRef"
            env.append(
                {"name": "X", "valueFrom": {field: {"name": ref, "key": "note"}}}
            )
        elif kind == "ConfigMap":
            volumes.append({"name": ref, "configMap": {"name": ref}})
        else:
            volumes.append({"name": ref, "secret": {"secretName": ref}})
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": "app", "envFrom": env_from, "env": env}],
                    "volumes": volumes,
                }
            }
        },
    }


_REF_LIST = st.lists(
    st.tuples(st.sampled_from(REFS), st.sampled_from(VIA)), min_size=1, max_size=4
)


@st.composite
def _cases(draw) -> Case:
    """The target Deployment in `home`, maybe a peer in `other`, and the pool's objects."""
    refs = draw(_REF_LIST)
    docs = [_workload(TARGET, "home", refs)]
    if draw(st.booleans()):
        docs.append(_workload("peer", "other", draw(_REF_LIST)))
    for kind, name in REFS:
        for namespace in draw(st.lists(st.sampled_from(NAMESPACES), unique=True)):
            key = draw(st.none() | st.sampled_from(KEYS))
            docs.append(_object(kind, name, namespace, key, draw(_WORD)))
    placed = tuple(
        Placed(
            draw(st.sampled_from("de")), draw(st.sampled_from(("a.yaml", "b.yaml"))), d
        )
        for d in draw(st.permutations(docs))
    )
    return Case(
        placed,
        tuple(draw(st.permutations(placed))),
        frozenset(ref for ref, _ in refs),
        draw(st.sampled_from(ADMITS)),
    )


def _directories(placed) -> dict[str, dict[str, str]]:
    """The render `placed` lays out, each file's documents joined in their listed order."""
    texts: dict[str, dict[str, list[str]]] = {}
    for p in placed:
        texts.setdefault(p.directory, {}).setdefault(p.file, []).append(
            yaml.safe_dump(p.doc)
        )
    return {
        d: {f: "---\n".join(t) for f, t in files.items()} for d, files in texts.items()
    }


def _value(placed, admitted, source=SOURCE) -> str | None:
    values = restart_values(
        "fixture", _directories(placed), {TARGET: admitted}, KEYS, source
    )
    return values.get(TARGET)


def _inputs(case: Case) -> list[int]:
    """The index in `case.placed` of each object the target reads and admits."""
    return [
        i
        for i, p in enumerate(case.placed)
        if p.doc["kind"] in case.admitted
        and p.doc["metadata"]["namespace"] == "home"
        and (p.doc["kind"], p.doc["metadata"]["name"]) in case.reads
        and not reloaded_in_place(p.doc["kind"], p.doc["metadata"]["name"])
    ]


def _edited(placed, index: int) -> tuple[Placed, ...]:
    """`placed` with the document at `index` given a different `note`."""
    doc = copy.deepcopy(placed[index].doc)
    for body in ("data", "stringData", "metadata"):
        if body in doc:
            doc[body]["note"] = "Edited!"
            break
    return (*placed[:index], placed[index]._replace(doc=doc), *placed[index + 1 :])


def _held(case: Case, indexes: list[int]) -> set[str]:
    """Each secret key the objects at `indexes` hold as a `${key}` placeholder."""
    return {
        token[2:-1]
        for i in indexes
        for body in ("data", "stringData")
        if (token := case.placed[i].doc.get(body, {}).get("token"))
    }


_DEPLOYMENT, _HOME, _OTHER = (
    Placed(
        "d", "a.yaml", _workload(TARGET, "home", [(("ConfigMap", "cfg-a"), "envFrom")])
    ),
    Placed("d", "b.yaml", _object("ConfigMap", "cfg-a", "home", None, "1")),
    Placed("e", "b.yaml", _object("ConfigMap", "cfg-a", "other", None, "2")),
)
_SHADOWED = Case(
    placed=(_DEPLOYMENT, _HOME, _OTHER),
    permuted=(_OTHER, _DEPLOYMENT, _HOME),
    reads=frozenset({("ConfigMap", "cfg-a")}),
    admitted=ADMITS[2],
)


@given(_cases())
@example(_SHADOWED)
def test_restart_values_does_not_depend_on_the_order_of_directories_files_or_documents(
    case,
):
    assume(_inputs(case))
    assert _value(case.placed, case.admitted) == _value(case.permuted, case.admitted)


@given(_cases())
@example(_SHADOWED)
def test_restart_values_moves_with_each_input_and_held_key_and_nothing_else(case):
    before = _value(case.placed, case.admitted)
    inputs = _inputs(case)
    held = _held(case, inputs)

    for i in range(len(case.placed)):
        moved = _value(_edited(case.placed, i), case.admitted) != before
        assert moved == (i in inputs), case.placed[i].doc["metadata"]
    for key in KEYS:
        moved = _value(case.placed, case.admitted, {**SOURCE, key: "f" * 64}) != before
        assert moved == (key in held), key
