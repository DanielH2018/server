"""The census row holding every `*_host` key of `group_vars/all.yml` to a reader."""

import re
from functools import cache

from _row_table import Census, Subject, tracked

_ALL_VARS = "inventory/group_vars/all.yml"
_HOST_KEY = re.compile(r"^([a-z0-9_]+_host):", re.MULTILINE)
_READER_ROOTS = ("hosts/*", "k8s/*", "scripts/*", ".github/*", ".claude/*")
_READER_SUFFIXES = (".yml", ".yaml", ".j2", ".py", ".sh")
_TEST_PATH = re.compile(r"(^|/)tests/|(^|/)test_[^/]*\.py$|(^|/)conftest\.py$")
_COMMENT = re.compile(r"(^|\s)#.*$")


@cache
def _read_text() -> str:
    """Shipped code and templates, minus tests and comments: what can read a key at deploy time."""
    return "\n".join(
        _COMMENT.sub("", line)
        for rel in tracked(*_READER_ROOTS)
        if rel.endswith(_READER_SUFFIXES) and not _TEST_PATH.search(rel)
        for line in Subject(rel).text.splitlines()
    )


def _unread_keys(subject: Subject) -> list[str]:
    read = _read_text()
    return [
        f"{key} has no reader outside a comment"
        for key in _HOST_KEY.findall(subject.text)
        if not re.search(rf"\b{key}\b", read)
    ]


HOST_KEYS_IN_GROUP_VARS_HAVE_A_READER = Census(
    name="host-keys-in-group-vars-have-a-reader",
    reason=(
        "A `*_host` key nothing reads looks like the switch that places a component, so an "
        "operator edits it and expects a timer to move. A component lands on a host through "
        "its host package. Delete the key, or read it where it matters."
    ),
    files=lambda: [_ALL_VARS],
    offence=_unread_keys,
    red=(Subject(_ALL_VARS, "renovate_notify_host: daniel-box\n"),),
    green=(Subject(_ALL_VARS, "ups_host: daniel-server\nunrelated: 1\n"),),
    must_find=frozenset({_ALL_VARS}),
)
