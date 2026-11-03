"""The interpreter a host-package render runs on: the built tree's pin, installed on demand.

The builder runs on its unit's `HOMELAB_PYTHON`, but the render of a commit runs on that
commit's own `host_python_version`. The unit carrying a new pin ships in `homelab-daniel-box`,
which only the render builds, so a render on the unit's pin deadlocks an interpreter cutover
(#7023). `host_package_builder.Builder.render` calls `render_python` before every render.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from homelab_tool_fetch import Failure

# Where a tree pins its interpreter, relative to the tree's root.
PYTHON_PIN = "ansible/inventory/group_vars/all.yml"
_PIN_LINE = re.compile(r"""^host_python_version:\s*["']?([^"'\s#]*)["']?\s*(?:#.*)?$""")
_PIN_VALUE = re.compile(r"\d+\.\d+(?:\.\d+)?")


def tree_python(tree: Path) -> str | None:
    """The `host_python_version` the tree at `tree` pins, or None when it pins none.

    The builder runs with no PyYAML, so this reads the one top-level line.

    Raises:
        Failure: reason `build`, for a pin that is not a dotted version, before it reaches an
          argv.
    """
    try:
        text = (tree / PYTHON_PIN).read_text()
    except OSError:
        return None
    for line in text.splitlines():
        match = _PIN_LINE.match(line)
        if match:
            pin = match.group(1)
            if not _PIN_VALUE.fullmatch(pin):
                raise Failure("build", f"{PYTHON_PIN} pins host_python_version {pin!r}")
            return pin
    return None


def render_python(tree: Path, fallback: str, uv: str, run: Callable[..., Any]) -> str:
    """Install the interpreter `tree` pins, or `fallback` when it pins none, and return it.

    Raises:
        Failure: reason `build`, for a malformed pin or an install that exits non-zero.
    """
    python = tree_python(tree) or fallback
    # ExecStart= sets UV_PYTHON_DOWNLOADS=never for the whole run, which refuses an install.
    env = {**os.environ, "UV_PYTHON_DOWNLOADS": "manual"}
    proc = run([uv, "python", "install", "--no-bin", python], env=env, check=False)
    if proc.returncode != 0:
        raise Failure("build", f"uv python install {python} exited {proc.returncode}")
    return python
