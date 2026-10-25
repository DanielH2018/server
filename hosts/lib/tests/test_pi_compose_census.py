"""The project and bind-source censuses over the rendered Compose project (H5.2a1).

Each census has a `_is_clean` case and a `_is_flagged` case, so a rule that stopped matching fails
its own test. The live component holds no service until H5.6, so the named members below are
fixture services. The move PRs point the censuses at the live project and name their services.

Run: uv run pytest hosts/lib/tests/test_pi_compose_census.py
"""

import pytest
from _compose_tree import (
    ALLOY,
    AUTOHEAL,
    COMPOSE,
    DIGEST,
    PROXY,
    REAL,
    TMPFILES_LINES,
    ComposeTree,
    bind_problems,
    memory_problems,
    project_of,
    project_problems,
    tmpfiles_dirs,
)

FIXTURE_MEMBERS = {"docker-proxy-lifecycle", "alloy", "autoheal"}
TMPFILES = (REAL / "tmpfiles.d" / "homelab-compose.conf").read_text() + TMPFILES_LINES


@pytest.fixture
def tree(tmp_path):
    return ComposeTree(tmp_path)


def problems(tree, tmpfiles=TMPFILES):
    project = project_of(tree.render())
    return project_problems(project), *bind_problems(project, tmpfiles_dirs(tmpfiles))


def test_the_project_census_is_clean_and_finds_its_named_members(tree):
    project = project_of(tree.render())
    assert project_problems(project) == []
    assert FIXTURE_MEMBERS <= set(project["services"])


def test_a_depends_on_target_the_project_does_not_declare_is_flagged(tree):
    tree.edit(
        "docker-proxy",
        "compose.yaml.j2",
        PROXY.replace("docker-proxy-lifecycle", "other"),
    )
    assert project_problems(project_of(tree.render())) == [
        "autoheal depends on docker-proxy-lifecycle, which the project does not declare"
    ]


def test_a_container_name_that_differs_from_its_key_is_flagged(tree):
    tree.edit(
        "autoheal",
        "compose.yaml.j2",
        AUTOHEAL.replace("container_name: autoheal", "container_name: autoheal-1"),
    )
    assert project_problems(project_of(tree.render())) == [
        "autoheal has container_name 'autoheal-1'"
    ]


def test_an_image_without_a_digest_is_flagged(tree):
    tree.edit("autoheal", "compose.yaml.j2", AUTOHEAL.replace(DIGEST, ""))
    (problem,) = project_problems(project_of(tree.render()))
    assert "autoheal image" in problem


LIMITS = "    cap_drop:\n      - ALL\n    deploy:\n      resources:\n"


def with_memory(limit, reservation):
    return (
        f"{LIMITS}        limits:\n          memory: {limit}\n"
        f"        reservations:\n          memory: {reservation}\n"
    )


def test_a_memory_floor_at_6_mib_is_clean(tree):
    """ACCEPT: the daemon's minimum is 6 MiB, so 6M and a larger limit pass."""
    tree.edit("autoheal", "compose.yaml.j2", AUTOHEAL + with_memory("16M", "6M"))
    assert memory_problems(project_of(tree.render())) == []


@pytest.mark.parametrize(
    ("limit", "reservation", "flagged"),
    [
        ("16M", "2M", "autoheal reservations.memory '2M' is below 6 MiB"),
        ("5M", "6M", "autoheal limits.memory '5M' is below 6 MiB"),
        ("512k", "6M", "autoheal limits.memory '512k' is below 6 MiB"),
    ],
)
def test_a_memory_value_under_6_mib_is_flagged(tree, limit, reservation, flagged):
    """REJECT: the shape of the canary's first run, which the daemon refused at `up`."""
    tree.edit("autoheal", "compose.yaml.j2", AUTOHEAL + with_memory(limit, reservation))
    assert memory_problems(project_of(tree.render())) == [flagged]


def test_a_top_level_mem_limit_and_an_unreadable_size_are_flagged(tree):
    tree.edit(
        "autoheal",
        "compose.yaml.j2",
        AUTOHEAL + "    mem_limit: 4m\n    mem_reservation: lots\n",
    )
    assert memory_problems(project_of(tree.render())) == [
        "autoheal mem_limit '4m' is below 6 MiB",
        "autoheal mem_reservation 'lots' is not a size",
    ]


def test_the_bind_census_is_clean_and_finds_alloys_data_directory(tree):
    clean, binds, found = problems(tree)
    assert (clean, binds) == ([], [])
    assert "/home/ubuntu/server/containers/alloy/data" in found
    assert f"{COMPOSE}/alloy/config.alloy" in found


def test_a_relative_bind_source_with_no_tmpfiles_line_is_flagged(tree):
    _, binds, _ = problems(tree, tmpfiles=TMPFILES.replace(TMPFILES_LINES, "\n"))
    assert binds == [
        "alloy: bind source /home/ubuntu/server/containers/alloy/data is not allowed"
    ]


def test_a_bind_source_under_the_secrets_directory_is_flagged(tree):
    tree.edit(
        "alloy",
        "compose.yaml.j2",
        ALLOY.replace("source: ./data", "source: /run/homelab-secrets/x"),
    )
    _, binds, _ = problems(tree)
    assert binds == ["alloy: bind source /run/homelab-secrets/x is not allowed"]


def test_a_bind_without_create_host_path_false_is_flagged(tree):
    tree.edit(
        "alloy",
        "compose.yaml.j2",
        ALLOY.replace("bind: {create_host_path: false}", "bind: {}", 1),
    )
    _, binds, _ = problems(tree)
    assert binds == [
        f"alloy: {COMPOSE}/alloy/config.alloy lacks create_host_path: false"
    ]


def test_a_short_syntax_volume_is_flagged(tree):
    tree.edit(
        "alloy",
        "compose.yaml.j2",
        ALLOY.replace("    volumes:\n", "    volumes:\n      - ./data:/data\n", 1),
    )
    _, binds, _ = problems(tree)
    assert binds == ["alloy: volume './data:/data' is not long-syntax bind"]
