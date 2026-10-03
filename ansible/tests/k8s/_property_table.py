"""The row type and checker `test_rendered_properties.py` runs over the rendered k8s manifests.

A policy-shaped guard is one selector, one predicate and a floor that stops the census passing
over nothing. Written as its own file, each one restated the render loop, the floor, the
offender report and the red-proof pair. Here they are a `Property` row: the selector and the
predicate are the row's own code, and everything else is this module's once. The verdict
itself is `_row_table.verdict`, which the textual census rows share.

What a row buys over a file:

- **The subject leaving fails the row.** A selector that matches nothing reports
  `subject gone: delete this row` rather than passing over an empty census. That is the
  retirement rule in `.claude/rules/python-layout.md`, enforced instead of remembered.
- **The floor and the named members are data.** `min_matches` catches a census that shrank;
  `must_find` names the members a row exists for, so the failure says which one went.
- **The red proof is mandatory.** A row cannot be built without `red` (a subject the predicate
  must flag) and `green` (one it must pass), and the table test runs both for every row.
- **An exemption carries its reason, and a stale one fails.** `allow` maps a key to why it is
  exempt; a key that no longer matches, or no longer offends, is reported for removal.

Run: uv run pytest ansible/tests/k8s/test_rendered_properties.py
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from _k8s_render import rendered_docs
from _row_table import verdict

# (role, template, doc) -> the (key, subject) pairs this document contributes. A key names the
# subject in reports, `must_find` and `allow`; it need only be unique within the row.
Selector = Callable[[str, str, dict], Iterable[tuple[str, dict]]]

# (role, subject) -> None when the property holds, a reason when it does not.
Predicate = Callable[[str, dict], str | None]


@dataclass(frozen=True)
class Property:
    name: str
    reason: str
    select: Selector
    offence: Predicate
    # Fixtures as (role, subject). `red` must be flagged and `green` must pass.
    red: tuple[str, dict]
    green: tuple[str, dict]
    min_matches: int = 1
    must_find: frozenset[str] = frozenset()
    allow: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


def check(
    prop: Property, docs: Iterable[tuple[str, str, dict]] | None = None
) -> list[str]:
    """Every problem `prop` has, or [] when it holds.

    `docs` defaults to the shared render; the harness's own tests pass a hand-built list.
    """
    found: dict[str, int] = {}
    offenders = []
    for role, tpl, doc in rendered_docs() if docs is None else docs:
        for key, subject in prop.select(role, tpl, doc):
            found[key] = found.get(key, 0) + 1
            reason = prop.offence(role, subject)
            if reason is not None:
                offenders.append((key, f"{role}/{tpl} [{key}]: {reason}"))
    return verdict(
        prop.name,
        prop.reason,
        found,
        offenders,
        min_matches=prop.min_matches,
        must_find=prop.must_find,
        allow=prop.allow,
    )
