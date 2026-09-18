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
import sys

from _helpers import REPO
from lib import yaml_fast

sys.path.insert(0, str(REPO / "scripts"))

from dev.gen_gitops_markers import COPIES, SOURCE, render

EXPECTED_COPIES = frozenset(
    {
        "scripts/lib/gitops_markers.py",
        "ansible/roles/k8s/monitor-bridge/files/gitops_markers.py",
        "ansible/roles/setup/deploy_ui/files/gitops_markers.py",
        "ansible/roles/setup/renovate_agent/files/gitops_markers.py",
    }
)

# Where each Ansible consumer names the files it installs. monitor-bridge's list drives its
# staging copy, the ConfigMap and the pod's mount; the two setup roles have a copy loop and a
# stamp pair, and `test_gitops_deploy_ship_list.py` covers the deployer's own.
_SHIP_LISTS = {
    "ansible/roles/k8s/monitor-bridge/files/gitops_markers.py": (
        "ansible/roles/k8s/monitor-bridge/defaults/main.yml"
    ),
    "ansible/roles/setup/deploy_ui/files/gitops_markers.py": (
        "ansible/roles/setup/deploy_ui/tasks/main.yml"
    ),
    "ansible/roles/setup/renovate_agent/files/gitops_markers.py": (
        "ansible/roles/setup/renovate_agent/tasks/main.yml"
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


def test_every_ansible_consumer_ships_its_copy():
    for target, ship_list in _SHIP_LISTS.items():
        assert "gitops_markers.py" in (REPO / ship_list).read_text(), (
            f"{ship_list} does not ship {target}"
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
    sys.path.insert(0, str(REPO / "ansible/roles/setup/gitops_deploy/files"))
    from gitops_markers import STATE_DIR

    for path, pattern in _DIRECTORY_LITERALS.items():
        found = pattern.findall((REPO / path).read_text())
        assert found, f"{path}: no state-directory literal matched"
        assert found == [STATE_DIR], f"{path}: {found}"
