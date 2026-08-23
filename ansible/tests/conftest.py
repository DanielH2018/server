"""Fixtures shared across the guards in this directory."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def widget_role(tmp_path: Path):
    """Build a synthetic k8s role under tmp_path and return its directory.

    The autodeploy guards read a role off disk, so pinning their behavior means writing one.
    Forty of those tests spelled out the same three lines — make the dir, make tasks/, write
    tasks/main.yml — before getting to the case they were actually about.

    Pass `tasks` as the tasks/main.yml body, `templates` as {filename: body}, and `defaults` as
    the defaults/main.yml body. Everything is optional: a role with no tasks file at all is
    itself a case the guards have to handle.
    """

    built = []

    def build(
        tasks: str | None = None,
        *,
        templates: dict[str, str] | None = None,
        defaults: str | None = None,
        name: str | None = None,
    ) -> Path:
        # Each call gets its own directory. Several tests build two or three roles to compare
        # them, and a shared path would let the last one silently stand in for all of them.
        built.append(None)
        role = tmp_path / (
            name or ("widget" if len(built) == 1 else f"widget-{len(built)}")
        )
        role.mkdir(parents=True, exist_ok=True)
        if tasks is not None:
            (role / "tasks").mkdir(exist_ok=True)
            (role / "tasks" / "main.yml").write_text(tasks)
        if templates is not None:
            # An EMPTY templates dir is its own case: a role that renders no manifest at all
            # must read as ungated, not as gated-with-nothing-to-gate. Pass `templates={}`.
            (role / "templates").mkdir(exist_ok=True)
            for filename, body in templates.items():
                (role / "templates" / filename).write_text(body)
        if defaults is not None:
            (role / "defaults").mkdir(exist_ok=True)
            (role / "defaults" / "main.yml").write_text(defaults)
        return role

    return build
