#!/usr/bin/env python3
"""The two Longhorn reapers are operator-invoked only; nothing may schedule them.

`longhorn-reap-orphan-backups.sh` and `longhorn-reap-orphan-snapshots.sh` delete stranded
recovery points, and which ones are safe to delete depends on live state — on 2026-08-16 the
answer was "none of them", because the weekly tier had produced nothing and the strays were the
only recovery those volumes had. The k3s role's autonomous-role contract therefore lists them
under **Never a cron**, and until this guard existed nothing checked it: a `cron:` task or a
kuma-check timer naming either script would have landed green.

The census is STRUCTURAL, not textual. The names appear on purpose in `defaults/main.yml`
comments, in the templates' own headers and in the role docs, so "the name appears nowhere under
`roles/setup/`" would fail on prose. What is forbidden is a SCHEDULE: a `cron:` task's fields, a
`kuma_check_timer.yml` import's vars, and a systemd `.service.j2`/`.timer.j2` template.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_reap_orphan_never_scheduled.py
"""

from pathlib import Path

from _helpers import ANSIBLE
from _helpers import load_tasks
from _helpers import walk_tasks
import json

SETUP = ANSIBLE / "roles" / "setup"
REAPERS = frozenset(
    {"longhorn-reap-orphan-backups.sh", "longhorn-reap-orphan-snapshots.sh"}
)
CRON_MODULES = frozenset({"cron", "ansible.builtin.cron"})
INCLUDE_MODULES = frozenset(
    {
        "import_tasks",
        "include_tasks",
        "ansible.builtin.import_tasks",
        "ansible.builtin.include_tasks",
    }
)
UNIT_SUFFIXES = (".service.j2", ".timer.j2")


def scheduled_texts(setup_root: Path) -> dict[str, str]:
    """Every scheduling surface under `setup_root`, keyed by where it was read.

    Three shapes schedule a command here: a `cron:` task (its fields are the schedule), an
    `import_tasks` of `kuma_check_timer.yml` (its `vars` carry `kuma_check_exec`), and a
    systemd unit template (`ExecStart=`). Comment lines in a unit template are dropped, as the
    task walker already drops YAML comments — a comment saying "never schedule X" must not
    read as scheduling X.
    """
    found: dict[str, str] = {}
    for tasks_file in sorted(setup_root.glob("*/tasks/**/*.yml")):
        for i, task in enumerate(walk_tasks(load_tasks(tasks_file))):
            key = f"{tasks_file.relative_to(setup_root)}#{i}"
            for module in CRON_MODULES:
                if module in task:
                    found[key] = json.dumps(task[module])
            for module in INCLUDE_MODULES:
                target = task.get(module)
                if isinstance(target, str) and target.endswith("kuma_check_timer.yml"):
                    found[key] = json.dumps(task.get("vars") or {})
    for unit in sorted(setup_root.glob("*/templates/*.j2")):
        if unit.name.endswith(UNIT_SUFFIXES):
            found[str(unit.relative_to(setup_root))] = "\n".join(
                line
                for line in unit.read_text().splitlines()
                if not line.lstrip().startswith("#")
            )
    return found


def scheduled_reapers(setup_root: Path) -> list[tuple[str, str]]:
    """`(where, reaper)` for every schedule naming a reaper — empty when the contract holds."""
    return [
        (where, reaper)
        for where, text in scheduled_texts(setup_root).items()
        for reaper in sorted(REAPERS)
        if reaper in text
    ]


def test_both_reapers_exist_as_templates() -> None:
    """Non-vacuity: a renamed reaper would make the census below pass on nothing."""
    templates = SETUP / "k3s" / "templates"
    missing = sorted(r for r in REAPERS if not (templates / f"{r}.j2").exists())
    assert not missing, f"reaper template(s) gone or renamed: {missing}"


def test_census_finds_the_scheduled_drill() -> None:
    """Non-vacuity: the scanner must see a real schedule before its silence means anything.

    The restore drill is a `cron:` task in `k3s/tasks/health-crons.yml`, and the kuma-check
    timers reach the census through their `kuma_check_exec` var; both surfaces have to be read
    for a silent scan to mean "nothing schedules a reaper".
    """
    texts = scheduled_texts(SETUP)
    assert any("longhorn-restore-drill.sh" in t for t in texts.values()), (
        "the cron census no longer sees the restore drill — the scanner is reading nothing"
    )
    assert any("kuma_check_exec" in t for t in texts.values()), (
        "the census no longer sees a kuma_check_timer.yml import's vars"
    )
    assert any(k.endswith(".timer.j2") for k in texts), (
        "the census no longer reads systemd timer templates"
    )


def test_no_setup_role_schedules_a_reaper() -> None:
    """The contract itself: no cron, timer import or unit template names either reaper."""
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Autonomous-role contract (the crons that change state with no human in the loop)
    assert scheduled_reapers(SETUP) == [], (
        "a reaper is scheduled; they are operator-invoked only (Never a cron, "
        "roles/setup/k3s/CLAUDE.md)"
    )


def test_a_scratch_cron_naming_a_reaper_is_flagged(tmp_path: Path) -> None:
    """Red proof: a `cron:` task naming the reaper in a scratch role turns the census red."""
    tasks = tmp_path / "scratch" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(
        "- name: Reap stranded backups nightly\n"
        "  ansible.builtin.cron:\n"
        "    name: reap\n"
        "    job: /usr/local/bin/longhorn-reap-orphan-backups.sh --apply\n"
    )
    assert scheduled_reapers(tmp_path) == [
        ("scratch/tasks/main.yml#0", "longhorn-reap-orphan-backups.sh")
    ]


def test_a_scratch_timer_import_naming_a_reaper_is_flagged(tmp_path: Path) -> None:
    """Red proof for the second surface: a kuma-check timer whose exec is the reaper."""
    tasks = tmp_path / "scratch" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(
        "- name: Reap stranded snapshots on a timer\n"
        '  ansible.builtin.import_tasks: "{{ role_path }}/../common/tasks/kuma_check_timer.yml"\n'
        "  vars:\n"
        "    kuma_check_exec: /usr/local/bin/longhorn-reap-orphan-snapshots.sh\n"
    )
    assert scheduled_reapers(tmp_path) == [
        ("scratch/tasks/main.yml#0", "longhorn-reap-orphan-snapshots.sh")
    ]


def test_a_scratch_unit_template_naming_a_reaper_is_flagged(tmp_path: Path) -> None:
    """Red proof for the third surface: a systemd service whose ExecStart is the reaper."""
    templates = tmp_path / "scratch" / "templates"
    templates.mkdir(parents=True)
    (templates / "reap.service.j2").write_text(
        "[Service]\nExecStart=/usr/local/bin/longhorn-reap-orphan-backups.sh --apply\n"
    )
    assert scheduled_reapers(tmp_path) == [
        ("scratch/templates/reap.service.j2", "longhorn-reap-orphan-backups.sh")
    ]


def test_a_comment_naming_a_reaper_is_clean(tmp_path: Path) -> None:
    """A unit template's comment is prose, the same as the defaults file's is."""
    templates = tmp_path / "scratch" / "templates"
    templates.mkdir(parents=True)
    (templates / "other.service.j2").write_text(
        "# never longhorn-reap-orphan-backups.sh here\n[Service]\nExecStart=/bin/true\n"
    )
    assert scheduled_reapers(tmp_path) == []
