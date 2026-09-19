"""Every consumer of the deployer's markers ships a fresh copy of `gitops_markers.py`.

`ansible/roles/setup/gitops_deploy/files/gitops_markers.py` is the one source for the state
directory, the marker basenames and the line parsers; `scripts/dev/gen_gitops_markers.py`
copies it into each tree that reads the markers, because none of them can import the
deployer's `files/` (issue #2063). This is the freshness half of that arrangement, the same
shape as `test_every_committed_fragment_matches_what_the_generator_writes_now` for the docs
fragments: a committed copy that differs from what the generator writes now fails here, and
so does a copy the generator knows about that the consumer role does not ship.

Non-vacuity is a named census rather than a count: `EXPECTED_COPIES` is every consumer this
test knows, so a copy dropped from the generator fails by name, and the generator gaining a
consumer nobody added here fails the other way.

Run: uv run pytest ansible/tests/deploy/test_gitops_markers_copies.py
"""

import re

from _helpers import REPO
from lib import yaml_fast


from dev.gen_gitops_markers import COPIES, SOURCE, render

EXPECTED_COPIES = frozenset(
    {
        "scripts/lib/gitops_markers.py",
        "ansible/roles/k8s/monitor-bridge/files/gitops_markers.py",
        "ansible/roles/setup/deploy_ui/files/gitops_markers.py",
        "ansible/roles/setup/renovate_agent/files/gitops_markers.py",
    }
)

# The copy task that installs each setup consumer's files under /opt, by task file and task
# name. The loop is what reaches the host: the stamp pair beside it records provenance only,
# so a substring search over the whole task file would pass on the stamp entry alone while
# the module never lands and the unit dies at import. monitor-bridge is separate below (its
# module list drives the ConfigMap and the mount), and `test_gitops_deploy_ship_list.py`
# covers the deployer's own loop.
_COPY_TASKS = {
    "ansible/roles/setup/deploy_ui/files/gitops_markers.py": (
        "ansible/roles/setup/deploy_ui/tasks/main.yml",
        "Install the deploy-ui files",
    ),
    "ansible/roles/setup/renovate_agent/files/gitops_markers.py": (
        "ansible/roles/setup/renovate_agent/tasks/main.yml",
        "Install agent Python files",
    ),
}

# Literals outside Python that must name the same directory: the tick wrapper reads the
# markers from a shell, and two manifests hand the directory to a unit and a pod. They cannot
# import anything, so they are pinned here rather than generated.
_DIRECTORY_LITERALS = {
    "scripts/deploy_tools/gitops_tick.sh": re.compile(r"^state_dir=(\S+)$", re.M),
    "ansible/roles/setup/deploy_ui/templates/deploy-ui.service.j2": re.compile(
        r"^Environment=DEPLOY_UI_STATE=(\S+)$", re.M
    ),
    "ansible/roles/k8s/monitor-bridge/templates/deployment.yaml.j2": re.compile(
        r"- name: monitor-bridge-gitops-state\n\s+hostPath:\n\s+path: (\S+)", re.M
    ),
}

# The pod's contention threshold is rendered into its env, and `_num()` reads the env before
# the default `config_service.py` derives from `CONTENTION_PAGE_SECONDS`. The SessionStart
# banner parks on that constant directly, so a bump to it that leaves this literal behind
# quiets the banner while the pod still pages at the old threshold, or the reverse — an
# operator sent looking for a page that never came. `GITOPS_BEHIND_MAX_MIN` is NOT pinned:
# the banner's behind threshold is deliberately shorter than the pod's.
_CONTENTION_ENV = (
    "ansible/roles/k8s/monitor-bridge/templates/env-secret.yaml.j2",
    re.compile(r'^  GITOPS_CONTENTION_MAX_MIN: "(\d+)"$', re.M),
)


def test_the_generator_knows_exactly_the_named_consumers():
    assert frozenset(COPIES) == EXPECTED_COPIES


def test_every_committed_copy_matches_what_the_generator_writes_now():
    source_text = (REPO / SOURCE).read_text()
    stale = [
        target
        for target in EXPECTED_COPIES
        if (REPO / target).read_text() != render(target, source_text)
    ]
    assert not stale, (
        f"stale copies {sorted(stale)}: run `uv run python scripts/dev/gen_gitops_markers.py`"
    )


def test_a_copy_carries_the_provenance_banner_and_the_source_does_not():
    """The banner is the second line of every copy, and the source has no banner at all."""
    assert not (REPO / SOURCE).read_text().startswith(f"# {SOURCE}\n# generated_from:")
    for target in EXPECTED_COPIES:
        assert (
            (REPO / target).read_text().startswith(f"# {target}\n# generated_from:")
        ), target


def _tasks_named(task_file: str, name: str) -> list[dict]:
    """Every task called `name` in `task_file`, descending into `block:` lists."""

    def walk(tasks):
        for task in tasks:
            if task.get("name") == name:
                yield task
            yield from walk(task.get("block", []))

    return list(walk(yaml_fast.safe_load((REPO / task_file).read_text())))


def test_every_setup_consumer_copy_loop_installs_the_module():
    for target, (task_file, task_name) in _COPY_TASKS.items():
        tasks = _tasks_named(task_file, task_name)
        assert len(tasks) == 1, f"{task_file}: {len(tasks)} tasks named {task_name!r}"
        assert "gitops_markers.py" in tasks[0]["loop"], (
            f"{task_file}: the {task_name!r} loop does not install {target}"
        )


def test_the_monitor_bridge_module_list_carries_the_copy():
    """The list, not just the file: it drives the ConfigMap and the mount, so a bare grep hit
    in a comment would not prove the pod gets the module."""
    defaults = yaml_fast.safe_load(
        (REPO / "ansible/roles/k8s/monitor-bridge/defaults/main.yml").read_text()
    )
    assert "gitops_markers.py" in defaults["monitor_bridge_modules"]


def test_the_source_is_import_free():
    """A copy runs in a pod, a hook and three /opt directories; only the stdlib is common."""
    for line in (REPO / SOURCE).read_text().splitlines():
        if line.startswith(("import ", "from ")):
            assert line == "from typing import NamedTuple", line


def test_every_non_python_literal_names_the_same_directory():
    from gitops_markers import STATE_DIR

    for path, pattern in _DIRECTORY_LITERALS.items():
        found = pattern.findall((REPO / path).read_text())
        assert found, f"{path}: no state-directory literal matched"
        assert found == [STATE_DIR], f"{path}: {found}"


def test_the_pod_pages_on_contention_at_the_threshold_the_banner_parks_on():
    from gitops_markers import CONTENTION_PAGE_SECONDS

    path, pattern = _CONTENTION_ENV
    found = pattern.findall((REPO / path).read_text())
    assert found == [str(CONTENTION_PAGE_SECONDS // 60)], f"{path}: {found}"
