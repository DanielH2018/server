"""The census row that no `cluster/services.yml` entry carries a description comment.

`test_census_rows_roles.py`, which imports it into `ROWS`, is at the cap for a test module.
"""

from _row_table import Census, Subject

_SERVICES = "cluster/services.yml"


def _described(subject: Subject) -> list[str]:
    """Each entry with a comment above `name:`, or a paragraph directly below it."""
    lines = subject.text.split("containers_list:\n", 1)[1].splitlines()
    found = []
    for i, line in enumerate(lines):
        if not line.startswith("  - name: "):
            continue
        start = i
        while start and lines[start - 1].startswith("  #"):
            start -= 1
        above = start < i and not lines[start].startswith("  # DECIDED:")
        # Two lines, because the first key's own trap gets one.
        below = len(lines) > i + 2 and all(
            following.startswith("    #") for following in lines[i + 1 : i + 3]
        )
        if above or below:
            found.append(f"line {i + 1} after `containers_list:` describes {line[10:]}")
    return found


NO_DESCRIBED_SERVICES = Census(
    name="no-services-entry-carries-a-description",
    reason=(
        "A service's description belongs in its role's CLAUDE.md. A comment in "
        "`cluster/services.yml` states one key's trap, beside that key."
    ),
    files=lambda: [_SERVICES],
    offence=_described,
    red=(
        Subject("above.yml", "containers_list:\n  # The RSS reader.\n  - name: a\n"),
        Subject(
            "below.yml",
            "containers_list:\n  - name: a\n    # Flux applies it\n    # (D5).\n    port: 80\n",
        ),
    ),
    green=(
        Subject(
            "a.yml",
            "containers_list:\n  # DECIDED: no daily tier.\n  # It re-derives.\n"
            "  - name: a\n    # Records the listener.\n    port: 80\n",
        ),
    ),
    must_find=frozenset({_SERVICES}),
)
