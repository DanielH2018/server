"""The k3s heartbeat scripts read no Kuma credential file, and every tag family retires it.

HISTORY. Until M6 batch B1's Cutover-2, `health-crons.yml` rendered `/etc/rancher/k3s/kuma-push.env`
(the Kuma push tokens) under one task tagged with the tag families of every heartbeat script that
sourced it, and this file held those two tag lists in agreement. A script installed under a tag
the env task did not carry read green behind a dead tile.

Cutover-2 removed every reader, so the same task became one that removes the file. The invariant
turned around: the file holds four push tokens at 0640 and Ansible does not remove a file a role
stops declaring, so a tag-scoped run of ANY of the families that used to read it has to remove it,
or the credential outlives the cutover on a host that was applied tag by tag.

Run: uv run pytest ansible/tests/setup/test_kuma_env_renders_for_every_cron_tag.py
"""

from _helpers import load_tasks
from lib.repo_paths import K3S_ROLE

CRONS = K3S_ROLE / "tasks" / "health-crons.yml"
TEMPLATES = K3S_ROLE / "templates"
RETIRE_TASK_NAME = "Retire the Kuma push credentials of the heartbeat scripts"
KUMA_ENV_PATH = "/etc/rancher/k3s/kuma-push.env"
# The families whose scripts sourced the file before Cutover-2. Named, so a retire task that
# lost one of them fails here and not on a host that was applied tag by tag.
FORMER_READER_TAGS = frozenset(
    {
        "backup-health",
        "disk-health",
        "manifest-prune",
        "etcd-snapshot",
        "release-staleness",
    }
)


def _retire_task() -> dict:
    task = next(
        (t for t in load_tasks(CRONS) if t.get("name") == RETIRE_TASK_NAME), None
    )
    assert task is not None, (
        f"{RETIRE_TASK_NAME!r} is missing from health-crons.yml — nothing removes the Kuma "
        "push tokens the retired scripts used to source"
    )
    return task


def test_the_retire_task_removes_the_file_under_every_former_reader_tag():
    task = _retire_task()
    removal = task["ansible.builtin.file"]
    assert removal["path"] == KUMA_ENV_PATH
    assert removal["state"] == "absent"
    assert FORMER_READER_TAGS <= set(task.get("tags") or []), sorted(
        FORMER_READER_TAGS - set(task.get("tags") or [])
    )


def test_no_k3s_template_or_task_reads_or_renders_the_file_any_more():
    # The rejecting half: a script that sources the file again would read an empty token, and a
    # task that renders it again would undo the retirement above.
    readers = sorted(
        path.name for path in TEMPLATES.glob("*") if KUMA_ENV_PATH in path.read_text()
    )
    assert readers == [], readers
    assert not (TEMPLATES / "kuma-push.env.j2").exists()
    rendering = [
        task.get("name")
        for task in load_tasks(CRONS)
        if "kuma-push.env.j2"
        in str(task.get("ansible.builtin.template", {}).get("src", ""))
    ]
    assert rendering == [], rendering


def test_the_live_drift_credential_file_is_retired_under_its_tag():
    task = next(
        t
        for t in load_tasks(CRONS)
        if t.get("name") == "Retire the live-drift check Kuma credentials"
    )
    removal = task["ansible.builtin.file"]
    assert removal["state"] == "absent"
    assert removal["path"].endswith("live-drift-check.env")
    assert set(task["tags"]) == {"live-drift"}
