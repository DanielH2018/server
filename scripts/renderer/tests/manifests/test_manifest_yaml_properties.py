"""Any string `manifest_yaml` writes plain is one the cluster's readers take for a string.

The oracle is the YAML 1.2 core schema's tag resolution (spec 1.2.2, section 10.3.2), written
out here from the spec, plus the YAML 1.1 booleans go-yaml v2 still resolves. It shares no code
with the pattern the writer uses. The property tests run under the profile
`tests/hypothesis_profiles.py` loads.
"""

import re

import yaml
from hypothesis import given
from hypothesis import strategies as st

from lib import yaml_fast
from renderer.renderer_lib.manifest_yaml import dump, dump_all

_CORE_SCHEMA_NON_STRING = [
    re.compile(r"null|Null|NULL|~|"),
    re.compile(r"true|True|TRUE|false|False|FALSE"),
    re.compile(r"[-+]?[0-9]+"),
    re.compile(r"0o[0-7]+"),
    re.compile(r"0x[0-9a-fA-F]+"),
    re.compile(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?"),
    re.compile(r"[-+]?(\.inf|\.Inf|\.INF)"),
    re.compile(r"\.nan|\.NaN|\.NAN"),
]
_GO_YAML_V2_BOOLEANS = frozenset(
    "y Y yes Yes YES n N no No NO on On ON off Off OFF".split()
)


def _read_as_a_non_string(plain: str) -> bool:
    return plain in _GO_YAML_V2_BOOLEANS or any(
        shape.fullmatch(plain) for shape in _CORE_SCHEMA_NON_STRING
    )


def _plain_scalars_that_are_not_strings(text: str) -> list[str]:
    """Each string value `text` holds unquoted that a cluster-side reader would retype."""
    return [
        node.value
        for document in yaml.compose_all(text)
        for _, node in document.value
        if not node.style
        and node.tag == "tag:yaml.org,2002:str"
        and _read_as_a_non_string(node.value)
    ]


# Mostly the characters numbers and booleans are made of, so short draws land on the shapes
# the two YAML versions disagree about.
_number_like = st.text(alphabet="0123456789abcdefxoXOE+-._~ynYN", max_size=10)
_digest8 = st.text(alphabet="0123456789abcdef", min_size=8, max_size=8)
_strings = st.one_of(_number_like, _digest8, st.text(max_size=20))


@given(_strings)
def test_a_string_is_never_written_in_a_form_the_cluster_reads_as_another_type(value):
    text = dump({"value": value})

    assert yaml_fast.safe_load(text) == {"value": value}
    assert _plain_scalars_that_are_not_strings(text) == []


@given(st.lists(_strings, max_size=4))
def test_every_document_of_a_stream_keeps_its_strings(values):
    text = dump_all({"value": value} for value in values)

    assert [d["value"] for d in yaml_fast.safe_load_all(text)] == values
    assert _plain_scalars_that_are_not_strings(text) == []


def test_the_oracle_flags_what_safe_dump_writes_for_the_digest_that_failed():
    """The red half: PyYAML's own writer leaves `05094468` plain, and the oracle names it."""
    text = yaml.safe_dump({"value": "05094468"})

    assert _plain_scalars_that_are_not_strings(text) == ["05094468"]
    assert _plain_scalars_that_are_not_strings(dump({"value": "0a1b2c3d"})) == []
