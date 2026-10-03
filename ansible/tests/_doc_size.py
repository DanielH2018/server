"""What the role-doc size guard and the hook arithmetic share: the unit, the ceiling and the warning
category.

The unit is CHARACTERS, and the ceiling is what the hook that injects a role doc into a Bash
session has left for the doc itself. `.claude/hooks/inject-nested-docs.py:INLINE_MAX_CHARS` is
7,500, and a doc the hook cannot inline is injected as its HEAD, so a doc over the ceiling is
one a session reads truncated.

`MAX_CHARS` is DERIVED rather than pinned to that 7,500, because the hook never weighs a doc
against the whole payload. `_fits_inline` weighs the doc plus its
`===== <doc> (applies to <trigger>) =====` header against `INLINE_MAX_CHARS - len(_PREAMBLE)`.
Pinning the ceiling to 7,500 passed twelve role docs the hook had already started truncating
(#3245): each sat in the 228 characters the preamble costs plus the header's own length.
`effective_ceiling` subtracts both, and
`ansible/tests/repo/test_role_claude_md.py` holds the arithmetic against the
hook's own `_fits_inline`.

The LINE half of `_fits_inline` has no ceiling here. Every role doc measured 54 to 95 lines
under `INLINE_MAX_LINES - _PREAMBLE.count("\n")` on 2026-10-02, so the character budget is the
one that binds, and a second ceiling would be a number nothing fails.

`recorded_count_problems` holds an `OVER_CEILING` entry to the character count its reason
records. `ansible/tests/repo/test_role_claude_md.py` calls it for every plane, so the three
planes agree on what a justified entry must say.

`ansible/tests/repo/test_role_claude_md.py` raises `RoleDocNearCeiling` for a role
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

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

from _helpers import REPO

HOOKS_DIR = REPO / ".claude" / "hooks"
INJECT_HOOK = HOOKS_DIR / "inject-nested-docs.py"

# Reserved for the `===== <doc> (applies to `<trigger>`) =====` header the hook charges to the
# same payload as the doc. The header length depends on the doc's own path and on whichever
# path in the command selected it, so the ceiling reserves the worst realistic case instead of
# becoming a per-role number. The longest header over this tree measured 166 chars on
# 2026-10-02 (`ansible/roles/k8s/observability/CLAUDE.md` with its deepest dashboard JSON as
# the trigger); 200 leaves room for a deeper path without moving every role's ceiling the day
# someone adds one.
# ENFORCED: ansible/tests/repo/test_role_claude_md.py
# ::test_the_header_allowance_covers_every_role_docs_longest_trigger
HEADER_ALLOWANCE = 200

# The two newlines `_fits_inline` adds around the doc: one after the header, one closing the
# block. `text.rstrip()` can be as long as the doc itself (a doc with no trailing newline), so
# charging both is the conservative reading.
_BLOCK_NEWLINES = 2

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


def load_inject_hook() -> ModuleType:
    """The inject hook as a module, so the ceiling reads its real constants.

    The hook's filename is hyphenated and it imports `_hook_common` at module level, so
    `.claude/hooks` has to be on `sys.path` for the exec. The entry is removed again
    afterwards: `ansible/tests` holds modules of its own and must not start resolving a hook
    with the same name.
    """
    sys.path.insert(0, str(HOOKS_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "inject_nested_docs_for_doc_size", INJECT_HOOK
        )
        if spec is None or spec.loader is None:
            raise AssertionError(f"{INJECT_HOOK} could not be loaded as a module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(HOOKS_DIR))


def hook_inline_budget(hook: ModuleType | None = None) -> int:
    """Chars `_fits_inline` leaves for the header AND the doc together.

    `INLINE_MAX_CHARS` is the whole payload; the preamble is charged to it before any doc is
    rendered, which is the subtraction the ceiling used to miss.
    """
    hook = hook or load_inject_hook()
    return hook.INLINE_MAX_CHARS - len(hook._PREAMBLE)


def effective_ceiling(hook: ModuleType | None = None) -> int:
    """The longest doc the hook is guaranteed to inline whole, for any role path.

    The budget minus what the hook spends on a doc of that length: the header
    (`HEADER_ALLOWANCE`) and the two newlines of the block. A doc at this ceiling fits
    `_fits_inline`; one above it can be head-truncated, which is a doc a session reads in part
    while the guard calls it fine.
    """
    return hook_inline_budget(hook) - HEADER_ALLOWANCE - _BLOCK_NEWLINES


# Derived, not pinned: the hook's budget, less what the hook spends around the doc inside it.
MAX_CHARS = effective_ceiling()


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
