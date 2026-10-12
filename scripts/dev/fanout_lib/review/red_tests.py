"""The rules a red test is held to: why it failed and what it suppresses.

WHY IT FAILED (#4023). `red_by_absence` sorts each red test's failure on the unchanged code
into an assertion or a name that is not there yet. The red gate accepts any `FAILED`. `red_prompt` tells the red author to reach a
not-yet-existing module inside the test body, so `assert import_module("new").f() is not None`
fails at the gate on the import and passes after any implementation of `f`. That test proves
the symbol was missing, not that its assertion can fail. The gate records which nodes failed
that way and refuses nothing: a refusal discards the whole red phase, and no production run
exists yet to calibrate one against. The reviewer is told which nodes to check for vacuity.

The failure text is the part of pytest's short summary after ` - `. It is cut to the terminal
width unless the run is `-vv`, which is how the red gate runs it.

WHAT IT SUPPRESSES (#4439). The red author commits with a plain `git commit`, so the `ty` hook
checks the red tree, and ty reports an import of a module that does not exist yet as an
`unresolved-import` error. A `# ty: ignore[unresolved-import]` passes that commit. Once the fix
adds the module, ty reports the same comment as an `unused-ignore-comment` warning, the hook
fails on it, and the fix commits nothing: the red file is not the fix's to edit, and in #4403
the auto-mode classifier denied removing the comments as a CI bypass. `stale_suppressions`
names each such comment the red commits add, so the red gate refuses them before any fix
exists. It counts a suppression that names no rule, in either the `ty:` or the `type:`
spelling, and one naming a rule in `ABSENCE_RULES`, the rules a fix resolves by adding a name.
`red_prompt` says how to reach a missing name without one.

What a fix may change in a red test file is `red_lock`'s.
"""

import io
import re
import tokenize
from collections import Counter

_FAILED = re.compile(r"^FAILED (\S+) - (.*)$", re.M)
# A test that fails because the code under test is missing, before any assertion runs.
ABSENCE = ("ImportError", "ModuleNotFoundError", "AttributeError", "NameError")
# A test that fails on what the code does.
ASSERTION = ("assert ", "AssertionError", "Failed: DID NOT RAISE")


def cause(text: str) -> str:
    """`absence`, `assertion` or `other` for one node's failure text."""
    if text.startswith(tuple(f"{name}:" for name in ABSENCE)):
        return "absence"
    if text.startswith(ASSERTION):
        return "assertion"
    return "other"


def red_by_absence(output: str, nodes: list[str]) -> list[str]:
    """The `nodes` that failed on a missing name in a `pytest -vv -rA` run's summary."""
    texts = {m.group(1): m.group(2) for m in _FAILED.finditer(output)}
    return [n for n in nodes if cause(texts.get(n, "")) == "absence"]


# A type-checker suppression inside one comment, with the rules its brackets name. ty honours
# mypy's `type: ignore` spelling as well as its own.
_SUPPRESSION = re.compile(r"\b(?:ty|type):\s*ignore\b(?:\[([^\]]*)\])?")
# ty's rules for a name that is not there yet, as `ty explain rule` lists them in 0.0.86. A fix
# that adds the name leaves a suppression of any of them unused.
ABSENCE_RULES = frozenset(
    {
        "unresolved-import",
        "unresolved-attribute",
        "unresolved-reference",
        "possibly-missing-import",
        "possibly-missing-attribute",
        "possibly-missing-submodule",
        "possibly-unresolved-reference",
    }
)


def stale_suppressions(base_src: str, red_src: str) -> list[str]:
    """The suppressions `red_src` adds that a fix would leave unused, each as `line N: comment`.

    A comment `base_src` already carries, as the same text, is not added, wherever it moved.
    A source that does not tokenize yields nothing: pytest refuses it at collection.
    """
    before = Counter(text for _, text in _suppressions(base_src))
    added = []
    for line, text in _suppressions(red_src):
        if before[text]:
            before[text] -= 1
        else:
            added.append(f"line {line}: `{text}`")
    return added


def _suppressions(src: str) -> list[tuple[int, str]]:
    """Each comment in `src` that suppresses every rule or one of `ABSENCE_RULES`.

    Comments come from `tokenize`, so the same text inside a string literal is not one.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenError, SyntaxError:
        return []
    found = []
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        for match in _SUPPRESSION.finditer(token.string):
            rules = match.group(1)
            if rules is None or ABSENCE_RULES & {r.strip() for r in rules.split(",")}:
                found.append((token.start[0], token.string.strip()))
                break
    return found
