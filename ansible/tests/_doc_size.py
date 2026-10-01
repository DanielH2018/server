"""What the k8s and setup role-doc size guards share: the unit, the ceiling and the warning
category.

The unit is CHARACTERS, and the ceiling is the payload budget of the hook that injects a role
doc into a Bash session. `.claude/hooks/inject-nested-docs.py:INLINE_MAX_CHARS` inlines a doc
under 7,500 characters and injects a larger one as its HEAD, so a doc over the ceiling is one
a session reads truncated. `MAX_CHARS` is pinned equal to it, and
`ansible/tests/repo/test_role_doc_ceiling_matches_the_hook.py` fails the day the two drift
apart.

`recorded_count_problems` holds an `OVER_CEILING` entry to the character count its reason
records. Both `ansible/tests/k8s/test_k8s_roles_have_claude_md.py` and
`ansible/tests/setup/test_setup_roles_have_claude_md.py` call it, so the two ceilings agree on
what a justified entry must say.

`ansible/tests/k8s/test_k8s_roles_have_claude_md.py` raises `RoleDocNearCeiling` for a role
CLAUDE.md that has entered the warning band below its ceiling, and `pyproject.toml`'s
`filterwarnings` carries an `always::_doc_size.RoleDocNearCeiling` entry beside its blanket
`error` so the notice is SHOWN rather than turned into the failure the band exists to arrive
before.

It is a module of its own for two reasons, both mechanical:

- xdist serializes a warning by its category's module name and the CONTROLLER re-imports that
  module. `pythonpath` puts `ansible/tests` on the path but not its subdirectories, so a
  category defined in `ansible/tests/k8s/<guard>.py` crashes the worker with
  `ModuleNotFoundError` and takes the whole run down with an `INTERNALERROR`. The category has to live at this directory's root.
- `_helpers.py`, the obvious home at that root, is near its 500-line ratchet cap
  (`ansible/tests/repo/test_module_length_ratchet.py`), so adding to it risks failing CI.
"""

import re
from pathlib import Path

from _helpers import REPO

INJECT_HOOK = REPO / ".claude" / "hooks" / "inject-nested-docs.py"

# Pinned equal to the hook's INLINE_MAX_CHARS. The hook's real inline threshold is
# `INLINE_MAX_CHARS - len(_PREAMBLE)`, so a doc just under MAX_CHARS can still be head-
# truncated; the ceiling names the budget rather than the worst case, because the budget is
# the number the hook and this guard have to agree on.
MAX_CHARS = 7500

_RECORDED_CHARS = re.compile(r"^(\d+) chars\b")
_HOOK_BUDGET = re.compile(r"^INLINE_MAX_CHARS = (\d+)$", re.MULTILINE)


class RoleDocNearCeiling(UserWarning):
    """A role CLAUDE.md inside the warning band below its character ceiling."""


def char_count(text: str) -> int:
    """The doc's length in code points — what the hook's budget counts, not `wc -c` bytes.

    This repo's prose carries em-dashes and arrows, so UTF-8 bytes run hundreds past the
    character count on a long doc; recording bytes would build that much slack into every
    OVER_CEILING entry.
    """
    return len(text)


def hook_inline_max_chars(hook: Path = INJECT_HOOK) -> int:
    """The literal `INLINE_MAX_CHARS` the inject hook assigns.

    Read as text rather than imported: the hook's filename is hyphenated and it imports
    `_hook_common` at module level, so an importlib exec from here would need `.claude/hooks`
    on `sys.path` in every xdist worker.
    """
    found = _HOOK_BUDGET.search(hook.read_text())
    if found is None:
        raise AssertionError(
            f"{hook} no longer assigns INLINE_MAX_CHARS at the top level — the role-doc "
            f"ceiling cannot be pinned to a budget it cannot read"
        )
    return int(found.group(1))


def recorded_count_problems(role: str, chars: int, reason: str) -> list[str]:
    """Check an OVER_CEILING reason opens with its character count and the doc still holds it.

    Before this check a listed role passed at any length, and `gitops_deploy`'s entry said
    "404 lines" while the doc grew to 488.
    """
    recorded = _RECORDED_CHARS.match(reason)
    if recorded is None:
        return [f"{role}: OVER_CEILING reason must open with '<N> chars on <date>'"]
    if chars > int(recorded.group(1)):
        return [
            f"{role}: CLAUDE.md is {chars} chars, past the {recorded.group(1)} its "
            f"OVER_CEILING reason records — trim it back, or re-justify the new count"
        ]
    return []
