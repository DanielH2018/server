"""Fixtures shared across the guards in this directory."""

import sys
from pathlib import Path

import pytest


# DECIDED: the per-worker k8s render leaves `--durations`, on purpose. `rendered_docs()` caches
# per process, and under xdist each worker builds it once: ~1.9s here and ~7.5s on a CI runner,
# charged to whichever module asks first on that worker. That module varies from run to run,
# and the serial `--record` run charges a different one again. On the master run in #4873,
# eleven modules recorded near 0s measured 5-7.7s in CI for that reason alone. Charging every
# reader instead would add 1.9s to each of the 70-odd modules that read the render, against
# about 24 builds CI actually pays (six shards of four workers). So the build is a per-worker
# cost, and the shard weights in `scripts/dev/pytest_shard_weights.json` model each module's
# own cost.
def pytest_collection_finish(session: pytest.Session) -> None:
    """Build the k8s render cache once per process, before any test's timed phases run.

    The module is looked up, not imported: a run that collected nothing reading the render,
    such as `scripts/` alone, pays nothing.
    """
    if session.config.option.collectonly:
        return
    if (k8s_render := sys.modules.get("_k8s_render")) is not None:
        k8s_render.prebuild()


@pytest.fixture
def widget_role(tmp_path: Path):
    """Build a synthetic k8s role under tmp_path and return its directory.

    The autodeploy guards read a role off disk, so pinning their behavior means writing one.
    The fixture makes the dir, makes tasks/ and writes tasks/main.yml, so a test starts at the
    case it is about.

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
