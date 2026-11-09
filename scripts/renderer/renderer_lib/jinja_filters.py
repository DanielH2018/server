"""The Ansible-dialect filters and the `search` test, with no ansible-core behind them.

`lib.ansible_jinja_env.register_ansible_filters` registers these on every render guard's
environment and on the renderer's, so templates keep the names Ansible gives them.

DECIDED: reimplemented, not copied. ansible-core is GPL-3.0-or-later and this repo is MIT. Each
filter covers what the tree's templates call, and refuses an argument they do not.
`scripts/renderer/tests/test_renderer_filters.py` holds each to the value ansible-core 2.22
returned for the same call, and ADR-0028's literals hold the cluster subset.

Typical usage example:

    env.filters["hash"] = filters.hash_text
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import base64
import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping

import yaml

from lib import yaml_fast

# Ansible's fixed namespace for `to_uuid`. A template's stable id depends on this value.
UUID_NAMESPACE = uuid.UUID("361E6D51-FAEC-444A-9079-341386DA8E2E")
_COMBINE_SKIPS = (None, "None", "null")


class FilterRefused(ValueError):
    """A filter that refused its input or an argument the tree does not use."""


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "surrogateescape")
    return str(value)


def _bytes(value, encoding: str = "utf-8") -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value).encode(encoding, "surrogateescape")


def b64encode(value, encoding: str = "utf-8", urlsafe: bool = False) -> str:
    """`value` as base64 text, in the URL-safe alphabet when `urlsafe` is set."""
    encode = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    return encode(_bytes(value, encoding)).decode()


def basename(path) -> str:
    return os.path.basename(path)


def dirname(path) -> str:
    return os.path.dirname(path)


def combine(*terms, **options) -> Mapping:
    """The mappings in `terms`, a list among them unpacked once, merged with the rightmost winning.

    A key's position is part of the output, which `to_json` writes as it finds it. Two equal
    mappings keep the right one's order.

    Raises:
        FilterRefused: An option was given, since no template in the tree merges recursively,
            or a term is not a mapping.
    """
    if options:
        raise FilterRefused(f"combine: {sorted(options)} is not supported")
    mappings = []
    for term in terms:
        for item in term if isinstance(term, (list, tuple)) else (term,):
            if item in _COMBINE_SKIPS:
                continue
            if not isinstance(item, Mapping):
                raise FilterRefused(f"combine: {item!r} is not a mapping")
            mappings.append(item)
    if not mappings:
        return {}
    merged = mappings[-1]
    for lower in reversed(mappings[:-1]):
        merged = dict(merged) if lower == merged else {**lower, **merged}
    return merged


def comment(text, style: str = "plain") -> str:
    """`text` as `#` comment lines, between a bare `#` line above and below.

    Raises:
        FilterRefused: `style` is not `plain`, the only one the tree uses.
    """
    if style != "plain":
        raise FilterRefused(f"comment: style {style!r} is not supported")
    *lines, last = str(text).split("\n")
    body = [f"# {line}" if line else "#" for line in lines]
    return "\n".join(["#", *body, f"# {last}", "#"])


def from_json(value) -> object:
    return json.loads(value)


def from_yaml(value) -> object:
    """`value` parsed as YAML, and None or a non-string returned as it came.

    Raises:
        FilterRefused: `value` is not valid YAML.
    """
    if not isinstance(value, str):
        return value
    try:
        return yaml_fast.safe_load(value)
    except yaml.YAMLError as exc:
        raise FilterRefused(f"from_yaml: {exc}") from exc


def hash_text(value, hashtype: str = "sha1") -> str:
    """The hex digest of `value`'s UTF-8 bytes under the hashlib algorithm `hashtype`."""
    return hashlib.new(hashtype, _bytes(value)).hexdigest()


def mandatory(value, msg: str | None = None) -> object:
    """`value` unchanged.

    A render guard's tracking Undefined judges an undefined name by itself, and the strict
    Undefined raises where a template uses the value. `msg` is for Ansible's own run.
    """
    return value


def regex_replace(
    value="",
    pattern: str = "",
    replacement: str = "",
    ignorecase: bool = False,
    multiline: bool = False,
    count: int = 0,
    mandatory_count: int = 0,
) -> str:
    """`re.subn` over `value`'s text, refusing a match count other than `mandatory_count`.

    Raises:
        FilterRefused: `mandatory_count` is set and the pattern matched another number of times.
    """
    text = _text(value)
    output, matches = re.subn(
        pattern, replacement, text, count=count, flags=_flags(ignorecase, multiline)
    )
    if mandatory_count and mandatory_count != matches:
        raise FilterRefused(
            f"regex_replace: {pattern!r} matched {matches} times, not {mandatory_count}"
        )
    return output


def search(
    value, pattern: str = "", ignorecase: bool = False, multiline: bool = False
) -> bool:
    """Whether `pattern` matches anywhere in `value`'s text."""
    return (
        re.search(pattern, _text(value), flags=_flags(ignorecase, multiline))
        is not None
    )


def _flags(ignorecase: bool, multiline: bool) -> re.RegexFlag:
    flags = re.RegexFlag(0)
    if ignorecase:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.MULTILINE
    return flags


def to_json(value, **kwargs) -> str:
    """`value` as JSON, holding plain data only, so a value the render lost refuses here.

    Raises:
        FilterRefused: `value` holds something JSON has no form for.
    """
    try:
        return json.dumps(value, **kwargs)
    except TypeError as exc:
        raise FilterRefused(f"to_json: {exc}") from exc


def to_uuid(value, namespace=UUID_NAMESPACE) -> str:
    """The UUIDv5 of `value`'s text, stable for one `value` and `namespace`."""
    if not isinstance(namespace, uuid.UUID):
        namespace = uuid.UUID(namespace)
    return str(uuid.uuid5(namespace, _text(value)))
