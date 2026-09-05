"""Every cron script that sources kuma-push.env must ship under a tag the env task also carries.

WHY. `health-crons.yml` renders `/etc/rancher/k3s/kuma-push.env` (the Kuma push tokens) under one
task, tagged with the tag families of every heartbeat script that sources it. Each of those
scripts is deployed by its own `release_bin.yml` import, tagged with its OWN tag family — so the
two tag lists are two places that must agree, and nothing forced them to.

They stopped agreeing on 2026-09-03: `release-staleness-check.sh.j2` shipped sourcing
`kuma-push.env` (PR #977), tagged `release-staleness`, but the env task's tag list was not
extended to match. `--tags release-staleness` installed the cron and never re-rendered the env
file, so the script logged `RELEASE_STALENESS_PUSH_TOKEN not set — skipping push` and the Kuma
tile stayed dead behind a green play — a tag-scoped run that installs a consumer without
rendering its input reads green.

Run: uv run pytest ansible/tests/setup/test_kuma_env_renders_for_every_cron_tag.py
"""

from pathlib import Path

from _helpers import ANSIBLE
from _helpers import REPO
from _helpers import load_tasks
from lib import release_bin_groups

CRONS = ANSIBLE / "roles" / "setup" / "k3s" / "tasks" / "health-crons.yml"
ENV_TASK_NAME = "Deploy the Kuma push credentials for the heartbeat scripts"
KUMA_ENV_PATH = "/etc/rancher/k3s/kuma-push.env"


def _tasks() -> list[dict]:
    return load_tasks(CRONS)


def _env_task_tags() -> set[str]:
    task = next((t for t in _tasks() if t.get("name") == ENV_TASK_NAME), None)
    assert task is not None, (
        f"{ENV_TASK_NAME!r} is missing from health-crons.yml — nothing renders the Kuma push "
        "tokens the heartbeat scripts source"
    )
    return set(task.get("tags") or [])


def _script_paths_for_task(task: dict, task_file: Path) -> list[Path]:
    """Every `*.sh.j2` this task renders, direct or via a `release_bin.yml` group.

    Both forms are checked because a future cron could render a script straight with
    `ansible.builtin.template:` instead of going through the versioned-release indirection every
    heartbeat here uses today.
    """
    paths = []
    template = task.get("ansible.builtin.template")
    if isinstance(template, dict):
        src = str(template.get("src", ""))
        if src.endswith(".sh.j2"):
            paths.append(task_file.parent.parent / "templates" / src)
    for src in release_bin_groups.task_sources(task, task_file):
        if src.endswith(".sh.j2"):
            paths.append(REPO / src)
    return paths


def _missing_env_tags(
    tasks: list[dict], env_tags: set[str], task_file: Path
) -> dict[str, set[str]]:
    """Map task name -> tags that task carries but the env task does not.

    Only tasks rendering a script that actually sources kuma-push.env are considered — a task
    tagged `live-drift`, say, renders no such script and must not be flagged for lacking a tag
    the env task has no reason to carry.
    """
    violations = {}
    for task in tasks:
        for path in _script_paths_for_task(task, task_file):
            if not path.is_file():
                continue
            if KUMA_ENV_PATH not in path.read_text():
                continue
            gap = set(task.get("tags") or []) - env_tags
            if gap:
                violations.setdefault(task.get("name", "<unnamed>"), set()).update(gap)
    return violations


def test_every_kuma_consuming_cron_tag_renders_the_env_file():
    env_tags = _env_task_tags()
    violations = _missing_env_tags(_tasks(), env_tags, CRONS)
    assert not violations, (
        "these health-crons.yml tasks render a script that sources kuma-push.env under a tag "
        f"the env task ({ENV_TASK_NAME!r}, tags={sorted(env_tags)}) does not carry, so a "
        "tag-scoped run installs the cron without the token it needs: "
        f"{ {name: sorted(tags) for name, tags in violations.items()} }"
    )


def test_census_finds_the_known_cron_families():
    """A census that silently found nothing would pass regardless of what drifted.

    Names two families deliberately: `manifest-prune` was already correctly tagged before this
    guard existed, and `release-staleness` is the family that drifted — both must survive
    whatever the census logic becomes.
    """
    env_tags = _env_task_tags()
    found = set()
    for task in _tasks():
        for path in _script_paths_for_task(task, CRONS):
            if path.is_file() and KUMA_ENV_PATH in path.read_text():
                found.update(task.get("tags") or [])
    assert {"manifest-prune", "release-staleness"} <= found, (
        f"expected the census to find at least manifest-prune and release-staleness; found "
        f"{sorted(found)}. If it found fewer, the discovery walk stopped seeing tasks."
    )
    assert {"manifest-prune", "release-staleness"} <= env_tags, (
        "the env task itself must carry both families' tags"
    )


# ── red proofs ───────────────────────────────────────────────────────────────────────────────


def test_task_missing_the_env_tag_is_flagged():
    """The rejecting half: a task whose script sources kuma-push.env under an untagged family."""
    fixture_task = {
        "name": "Deploy a fixture script as a versioned release",
        "tags": ["totally-untagged-fixture"],
        "ansible.builtin.import_tasks": "release_bin.yml",
        "vars": {
            "release_bin_templates": [
                "ansible/roles/setup/k3s/templates/disk-health.sh.j2",
            ],
        },
    }
    env_tags = {"backup-health", "disk-health", "manifest-prune"}
    violations = _missing_env_tags([fixture_task], env_tags, CRONS)
    assert violations == {
        "Deploy a fixture script as a versioned release": {"totally-untagged-fixture"}
    }


def test_task_carrying_a_tag_the_env_task_has_is_clean():
    """The accepting half, so the rejecting half above proves something."""
    fixture_task = {
        "name": "Deploy a fixture script as a versioned release",
        "tags": ["disk-health"],
        "ansible.builtin.import_tasks": "release_bin.yml",
        "vars": {
            "release_bin_templates": [
                "ansible/roles/setup/k3s/templates/disk-health.sh.j2",
            ],
        },
    }
    env_tags = {"backup-health", "disk-health", "manifest-prune"}
    assert _missing_env_tags([fixture_task], env_tags, CRONS) == {}


def test_task_whose_script_does_not_read_the_env_file_is_ignored():
    """A task tagged with something the env task lacks is fine when its script needs no token."""
    fixture_task = {
        "name": "Deploy a fixture script that reads no Kuma env",
        "tags": ["totally-untagged-fixture"],
        "ansible.builtin.import_tasks": "release_bin.yml",
        "vars": {
            "release_bin_templates": [
                "ansible/roles/setup/k3s/templates/longhorn-restore-drill.sh.j2",
            ],
        },
    }
    assert _missing_env_tags([fixture_task], set(), CRONS) == {}
