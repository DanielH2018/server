#!/usr/bin/env python3
"""Flag bash's `${#var}` length expansion in a Jinja template, before anything renders it.

`{#` opens a Jinja COMMENT, so a shell script inside a `.j2` that writes `${#var}` hands Jinja
a comment opener. Two outcomes, both bad. With no `#}` later in the file the render fails with
`Missing end of comment tag`, a message that names neither bash nor a fix. With a `#}` later on
— a real Jinja comment, or a second `${#...}` whose `}` follows a `#` — Jinja SWALLOWS
everything in between and the rendered script is silently short.

This is a TEXT scan, not a render check, and that is the point: the failing case never renders,
and the swallowing case renders to output with the evidence already deleted. Neither is visible
to the render-then-lint validators beside this one.

It is also the only validator scoped to every `.j2` in the tree. compose_templates.py covers
the Pi's compose files, k8s_manifests.py `roles/k8s/**`, shell_templates.py `*.sh.j2` and
unit_templates.py the systemd units — but a `${#` collision bites any template that carries
shell, including a ConfigMap's embedded script and a compose `healthcheck.test`.

A template that must hold a literal `${#` wraps it in `{% raw %}` / `{% endraw %}`, which this
scan honours. An unterminated `{% raw %}` covers to end of file, matching how Jinja's lexer
treats it, rather than quietly re-arming the rule for the rest of the template.

Run directly or via the ``validate-jinja-bash-collisions`` prek hook. Exits non-zero if any
template carries the collision.
"""

import re
import sys
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.repo_paths import ANSIBLE, REPO

# The collision itself. Deliberately narrow: `${` on its own is ordinary Jinja-safe shell, and
# the compose `$$`-escaping rule is validate/compose_templates.py's job.
COLLISION = "${#"

_RAW_OPEN = re.compile(r"\{%-?\s*raw\s*-?%\}")
_RAW_CLOSE = re.compile(r"\{%-?\s*endraw\s*-?%\}")

FIX = (
    "wrap the script in {% raw %} / {% endraw %}, or write "
    '$(printf %s "$var" | wc -c) instead of ${#var}'
)


def blank_raw_blocks(text: str) -> str:
    """Return `text` with every `{% raw %}` body blanked out, line numbers preserved.

    Blanking rather than deleting keeps the line numbers this scan reports equal to the line
    numbers in the file. An unterminated `{% raw %}` blanks to end of file.
    """
    out = list(text)
    pos = 0
    while True:
        opener = _RAW_OPEN.search(text, pos)
        if opener is None:
            return "".join(out)
        closer = _RAW_CLOSE.search(text, opener.end())
        end = closer.end() if closer else len(text)
        for i in range(opener.start(), end):
            if out[i] != "\n":
                out[i] = " "
        pos = end


def find_collisions(text: str) -> list[tuple[int, str]]:
    """Return (line number, stripped line) for every `${#` outside a `{% raw %}` block."""
    scannable = blank_raw_blocks(text)
    return [
        (n, line.strip())
        for n, line in enumerate(scannable.splitlines(), start=1)
        if COLLISION in line
    ]


def templates(root: Path = ANSIBLE) -> list[Path]:
    """Every Jinja template under `root`, sorted."""
    return sorted(root.rglob("*.j2"))


def main() -> int:
    """Scan every template and report each `${#` collision.

    Returns:
        0 if no template carries the collision, 1 otherwise.
    """
    found = 0
    scanned = templates()
    for tpl in scanned:
        for lineno, line in find_collisions(tpl.read_text(errors="replace")):
            found += 1
            rel = tpl.relative_to(REPO)
            print(
                f"  [FAIL] {rel}:{lineno}: bash `{COLLISION}` opens a Jinja comment — {FIX}\n"
                f"         {line}",
                file=sys.stderr,
            )
    print(f"\n{len(scanned)} Jinja template(s) scanned, {found} collision(s).")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
