"""One line for many names that share a few reasons: a count for a big group, names for a small one.

A broad landing waits on some seventy Kustomizations, nearly all for the same reason, and
naming each one with its reason fills an agent's context (#7894). `grouped` names the reason
once with a count and spells out only the names held for some rarer reason.

Typical usage example:

    grouped([("svc01", "not Ready"), ("svc02", "absent")], noun="Kustomization")
    # 'not Ready: svc01; absent: svc02'
    grouped([(t, state) for t, state in states], named_up_to=0)
    # '62 healthy, 6 skipped'
"""

from collections.abc import Iterable

# A group this size or smaller is listed by name; a bigger one prints as a count.
NAMED_UP_TO = 3


def grouped(
    pairs: Iterable[tuple[str, str]], noun: str = "", named_up_to: int = NAMED_UP_TO
) -> str:
    """Join `(name, reason)` pairs by reason, largest group first.

    Args:
        pairs: each name with the reason it is listed; the order of first appearance breaks
            a tie in group size.
        noun: what a counted name is, such as "Kustomization", pluralised by `s`. Empty
            prints the bare count before the reason, as in `62 healthy`.
        named_up_to: the largest group still listed by name. 0 counts every group.

    Returns:
        `<n> <noun>s <reason>` for a counted group and `<reason>: <a>, <b>` for a named
        one. Groups join with `; ` when any is named, whose names hold commas, else `, `.
    """
    groups: dict[str, list[str]] = {}
    for name, reason in pairs:
        groups.setdefault(reason, []).append(name)
    parts, named = [], False
    for reason, names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(names) > named_up_to:
            what = f"{noun}{'s' if len(names) != 1 else ''} " if noun else ""
            parts.append(f"{len(names)} {what}{reason}")
        else:
            parts.append(f"{reason}: {', '.join(names)}")
            named = True
    return ("; " if named else ", ").join(parts)
