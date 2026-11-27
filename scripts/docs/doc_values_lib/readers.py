"""The row type `doc_values.py` checks, and the readers its rows are built from.

A row module maps a source file to the rows that read it, and `rows` flattens that map.
A reader takes the source's path and returns the text the doc should state. A reader that finds its value more than once in
the source returns it only when every copy agrees, so two copies that drift apart fail the
row instead of passing on whichever came first. A reader that turns a number into words
raises on a number it has no word for.

STATIC PARSING ONLY, as in `fragment_readers.py`: a reader parses its source and never
imports it.
"""

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path as _Path

from docs.fragment_readers import module_constant
from lib import yaml_fast
from lib.render_guard import k8s_services

Reader = Callable[[_Path], str]
Render = Callable[[str], str]


@dataclass(frozen=True)
class Value:
    """One sentence that states a value, and where the tree holds that value.

    Attributes:
        doc: repo path of the doc that states the value.
        pattern: a regex with exactly one capture group, the stated value.
        source: repo path of the file the value comes from, named in a failure.
        read: returns the text the doc should state, given the path of `source`.
    """

    doc: str
    pattern: str
    source: str
    read: Reader


CARDINALS = "zero one two three four five six seven eight nine ten".split()
ORDINALS = (
    "zeroth first second third fourth fifth sixth seventh eighth ninth tenth".split()
)


def one(values: Iterable, where: str) -> str:
    distinct = sorted({str(value) for value in values})
    if len(distinct) != 1:
        raise ValueError(f"{where}: expected one value, found {distinct or 'none'}")
    return distinct[0]


def cardinal(value: str) -> str:
    if not 0 <= int(value) < len(CARDINALS):
        raise ValueError(f"no word for {value}; state it as a numeral")
    return CARDINALS[int(value)]


def phrase(words: dict[str, str]) -> Render:
    def render(value: str) -> str:
        if value not in words:
            raise ValueError(f"no phrase for {value!r}; extend the row's mapping")
        return words[value]

    return render


def per(unit: int) -> Render:
    def render(value: str) -> str:
        whole, rest = divmod(int(value), unit)
        if rest:
            raise ValueError(f"{value} is not a whole multiple of {unit}")
        return str(whole)

    return render


def signed(value: str) -> str:
    return f"{int(value):+d}"


def constant(name: str, render: Render = str) -> Reader:
    return lambda module: render(str(module_constant(module, name)))


def key(key: str, render: Render = str) -> Reader:
    return lambda path: render(str(yaml_fast.safe_load(path.read_text())[key]))


def match(pattern: str, render: Render = str) -> Reader:
    """The one value every match of `pattern`'s capture group holds in the source."""
    return lambda path: render(
        one(re.findall(pattern, path.read_text(), re.MULTILINE), f"/{pattern}/")
    )


def count(pattern: str, render: Render = str) -> Reader:
    return lambda path: render(
        str(len(re.findall(pattern, path.read_text(), re.MULTILINE)))
    )


def _service(path: _Path, name: str) -> dict:
    [entry] = [e for e in k8s_services(path) if e["name"] == name]
    return entry


def service_port(name: str) -> Reader:
    return lambda path: str(_service(path, name)["port"])


def metrics_port(name: str) -> Reader:
    return lambda path: one(
        [scrape["port"] for scrape in _service(path, name)["metrics"]], name
    )


def weekly_shard(name: str) -> Reader:
    return lambda path: (
        "weekly-backup-d"
        + one(list(_service(path, name)["weekly_backup_claims"].values()), name)
    )


def dependents(name: str) -> Reader:
    return lambda path: cardinal(
        str(sum(name in (e.get("depends_on") or []) for e in k8s_services(path)))
    )


def soak_plus_grace(name: str) -> Reader:
    def read(module: _Path) -> str:
        soak = module_constant(module, name)
        days = one(soak.values(), name) if isinstance(soak, dict) else soak
        return str(int(days) + module_constant(module, "PENDING_GRACE_DAYS"))

    return read


def staged_paths(script: str) -> list[str]:
    """The paths the docs-refresh cron's one `git add` command names.

    The command may run over several lines that end in a backslash. A comment that
    mentions `git add` is no command.
    """
    lines = [line.strip() for line in script.splitlines()]
    starts = [i for i, line in enumerate(lines) if line.startswith("git add ")]
    assert len(starts) == 1, f"expected one `git add` command, found {len(starts)}"
    words: list[str] = []
    for line in lines[starts[0] :]:
        words += line.removesuffix("\\").split()
        if not line.endswith("\\"):
            break
    return words[2:]


def staged_count(words: Sequence[str], offset: int = 0) -> Reader:
    return lambda script: words[len(staged_paths(script.read_text())) + offset]


def rows(by_source: dict[str, list[tuple[str, str, Reader]]]) -> tuple[Value, ...]:
    """One `Value` per (doc, pattern, reader) in a map from each source to its rows."""
    return tuple(
        Value(doc, pattern, source, read)
        for source, entries in by_source.items()
        for doc, pattern, read in entries
    )
