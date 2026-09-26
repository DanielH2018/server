"""What the k8s and setup role-doc size guards share: the warning category and the count check.

`recorded_count_problems` holds an `OVER_CEILING` entry to the line count its reason records
(#2679). Both `ansible/tests/k8s/test_k8s_roles_have_claude_md.py` and
`ansible/tests/setup/test_setup_roles_have_claude_md.py` call it, so the two ceilings agree on
what a justified entry must say.

`ansible/tests/k8s/test_k8s_roles_have_claude_md.py` raises `RoleDocNearCeiling` for a role
CLAUDE.md that has entered the warning band below its line ceiling, and `pyproject.toml`'s
`filterwarnings` carries an `always::_doc_size.RoleDocNearCeiling` entry beside its blanket
`error` so the notice is SHOWN rather than turned into the failure the band exists to arrive
before.

It is a module of its own for two reasons, both mechanical:

- xdist serializes a warning by its category's module name and the CONTROLLER re-imports that
  module. `pythonpath` puts `ansible/tests` on the path but not its subdirectories, so a
  category defined in `ansible/tests/k8s/<guard>.py` crashes the worker with
  `ModuleNotFoundError` and takes the whole run down with an `INTERNALERROR` (measured
  2026-09-25). The category has to live at this directory's root.
- `_helpers.py`, the obvious home at that root, sat at exactly its 500-line ratchet cap
  (`ansible/tests/repo/test_module_length_ratchet.py`), so adding to it fails CI.
"""

import re

_RECORDED_LINES = re.compile(r"^(\d+) lines\b")


class RoleDocNearCeiling(UserWarning):
    """A role CLAUDE.md within `WARN_LINES..MAX_LINES` of its line ceiling."""


def recorded_count_problems(role: str, lines: int, reason: str) -> list[str]:
    """Check that an OVER_CEILING reason opens with its line count and the doc still holds it.

    Before this check a listed role passed at any length, and `gitops_deploy`'s entry said
    "404 lines" while the doc grew to 488.
    """
    recorded = _RECORDED_LINES.match(reason)
    if recorded is None:
        return [f"{role}: OVER_CEILING reason must open with '<N> lines on <date>'"]
    if lines > int(recorded.group(1)):
        return [
            f"{role}: CLAUDE.md is {lines} lines (wc -l), past the {recorded.group(1)} its "
            f"OVER_CEILING reason records — trim it back, or re-justify the new count"
        ]
    return []
