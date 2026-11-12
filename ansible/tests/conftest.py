"""Fixtures shared across the guards in this directory."""

from pathlib import Path

import pytest


@pytest.fixture
def widget_role(tmp_path: Path):
    """Build a synthetic k8s role under tmp_path and return its directory.

    The autodeploy guards read a role off disk, so pinning their behavior means writing one.
    Pass `templates` as {filename: body} and `defaults` as the defaults/main.yml body. Both are
    optional: a role with no defaults file at all is itself a case the guards have to handle.
    """

    built = []

    def build(
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
