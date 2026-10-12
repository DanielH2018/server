"""The per-template `[ok]` line every template validator prints, kept behind `-v` (#4448).

A clean run of `k8s_manifests.py` printed one `[ok]` line per template, about 150 of them, and
the `[FAIL]` lines a reader needed sat somewhere in that list. A validator now prints its
failures and its one-line summary by default, and lists the clean templates only when asked.
"""

from collections.abc import Callable, Sequence

VERBOSE_FLAGS = ("-v", "--verbose")


def ok_printer(argv: Sequence[str]) -> Callable[[str], None]:
    """Return the function a validator prints each `[ok]` line through.

    Args:
        argv: The validator's command-line arguments, without the program name.

    Returns:
        `print` when argv carries `-v` or `--verbose`, otherwise a function that drops the line.
    """
    if any(arg in VERBOSE_FLAGS for arg in argv):
        return print
    return lambda _line: None
