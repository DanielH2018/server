#!/usr/bin/env python3
"""The two Longhorn reapers are operator-invoked only; nothing may schedule them.

`scripts/backup/longhorn_reap.py backups` and `longhorn_reap.py snapshots` delete stranded
recovery points, and which ones are safe to delete depends on live state — when the
weekly tier has produced nothing, the strays are the only recovery a volume has. The k3s-host-checks
component's autonomous-role contract therefore lists them under **Never a cron**, and this guard
fails a systemd unit template naming the script.

The census is STRUCTURAL, not textual. The names appear on purpose in `defaults/main.yml`
comments, in the scripts' own docstrings and in the role docs, so "the name appears nowhere under
`hosts/components/`" would fail on prose. What is forbidden is a SCHEDULE: a systemd
`.service.j2`/`.timer.j2` template under a host component.

A third surface arrives with target slice H2, which moves host crons into CronJobs: a rendered
k8s CronJob whose command, or a ConfigMap it mounts, names the reaper (the CronJob arm below).

Run: uv run pytest tests/longhorn/test_longhorn_reap_never_scheduled.py
"""

from pathlib import Path

from _helpers import REPO
from _k8s_render import pod_spec, rendered_docs
import json

from lib.repo_paths import K3S_HOST_CHECKS

COMPONENTS = REPO / "hosts" / "components"
BACKUP_SCRIPTS = REPO / "scripts" / "backup"
# Both reapers are subcommands of one entry point since #4345, so its name covers either.
REAPERS = frozenset({"longhorn_reap.py"})
UNIT_SUFFIXES = (".service.j2", ".timer.j2")


def _uncommented(text: str) -> str:
    """`text` with its comment lines dropped.

    The task walker already drops YAML comments, and a unit template's comments are prose the
    same way: a comment saying "never schedule X" must not read as scheduling X.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def unit_texts(root: Path) -> dict[str, str]:
    """Every systemd unit template under `root`, keyed `<owner>/<templates|units>/<name>`.

    Read from the source: a role keeps its units in `templates/`, a host component in `units/`
    or `templates/`. An `ExecStart=` whose command arrives through a variable stays invisible
    to this scan.
    """
    found: dict[str, str] = {}
    units = [*root.glob("*/templates/*.j2"), *root.glob("*/units/*.j2")]
    for unit in sorted(units):
        if unit.name.endswith(UNIT_SUFFIXES):
            found[str(unit.relative_to(root))] = _uncommented(unit.read_text())
    return found


def scheduled_texts(root: Path) -> dict[str, str]:
    """Every scheduling surface under `root`, keyed by where it was read: its unit templates."""
    return unit_texts(root)


def scheduled_reapers(root: Path) -> list[tuple[str, str]]:
    """`(where, reaper)` for every schedule naming a reaper — empty when the contract holds."""
    return [
        (where, reaper)
        for where, text in scheduled_texts(root).items()
        for reaper in sorted(REAPERS)
        if reaper in text
    ]


def test_both_reapers_exist_in_scripts_backup() -> None:
    """Non-vacuity: a renamed reaper would make the census below pass on nothing."""
    missing = sorted(r for r in REAPERS if not (BACKUP_SCRIPTS / r).exists())
    assert not missing, f"reaper script(s) gone or renamed: {missing}"


def test_census_finds_the_scheduled_heartbeat() -> None:
    """Non-vacuity: the scanner must see a real schedule before its silence means anything.

    The host components' unit templates are the one live surface; a silent scan must still
    have read them.
    """
    texts = scheduled_texts(COMPONENTS)
    assert "k3s-host-checks/units/homelab-etcd-token-drift.timer.j2" in texts, (
        "the census no longer reads the host components' timer templates"
    )


def test_no_host_component_schedules_a_reaper() -> None:
    """The contract itself: no unit template names either reaper."""
    assert scheduled_reapers(COMPONENTS) == [], (
        "a reaper is scheduled; they are operator-invoked only (Never a cron, "
        "hosts/components/k3s-host-checks/component.yml)"
    )


def test_no_host_check_unit_runs_a_reaper() -> None:
    """The k3s-host-checks timers carry the same contract, read from the component itself."""
    units = sorted((K3S_HOST_CHECKS / "units").glob("*.j2"))
    assert any(u.name == "homelab-longhorn-backup-health.service.j2" for u in units)
    assert [u.name for u in units if "longhorn_reap" in u.read_text()] == []


def test_a_scratch_unit_template_naming_a_reaper_is_flagged(tmp_path: Path) -> None:
    """Red proof: a systemd service whose ExecStart is the reaper."""
    templates = tmp_path / "scratch" / "templates"
    templates.mkdir(parents=True)
    (templates / "reap.service.j2").write_text(
        "[Service]\nExecStart=/srv/server/scripts/backup/longhorn_reap.py backups --apply\n"
    )
    assert scheduled_reapers(tmp_path) == [
        ("scratch/templates/reap.service.j2", "longhorn_reap.py")
    ]


def test_a_comment_naming_a_reaper_is_clean(tmp_path: Path) -> None:
    """A unit template's comment is prose, the same as the defaults file's is."""
    templates = tmp_path / "scratch" / "templates"
    templates.mkdir(parents=True)
    (templates / "other.service.j2").write_text(
        "# never longhorn_reap.py here\n[Service]\nExecStart=/bin/true\n"
    )
    assert scheduled_reapers(tmp_path) == []


# ── The CronJob arm: no k8s CronJob runs a reaper ────────────────────────────────────────
#
# Host crons moved into CronJobs (ADR-0029), so a schedule can arrive in a rendered manifest,
# as well as in a unit template. A CronJob that names the reaper in its command is caught directly.
# One that
# runs a script from a mounted ConfigMap names it only in that ConfigMap's data, so the arm
# reads the data of every ConfigMap the CronJob mounts, through a `configMap` volume or a
# `projected` source.

# The CronJob and the mounted script ConfigMap that must be read, so an empty render cannot
# pass. H2.3b adds the drill's script ConfigMap here when its tag renders one.
CRONJOB_MEMBERS = frozenset({("CronJob", "pi-peer-backup")})
MOUNTED_MEMBERS = frozenset({("ConfigMap", "karakeep-archive-pruner")})


def _mounted_configmaps(pod: dict) -> set[str]:
    """The names of every ConfigMap a pod spec mounts, directly or through a projected volume."""
    names = set()
    for volume in pod.get("volumes") or []:
        if "configMap" in volume:
            names.add(volume["configMap"]["name"])
        for source in (volume.get("projected") or {}).get("sources") or []:
            if "configMap" in source:
                names.add(source["configMap"]["name"])
    return names


def cronjob_reapers(
    docs: list[dict],
) -> tuple[list[tuple[str, str]], set[tuple[str, str]]]:
    """(`(cronjob, where)` per reaper call, every `(kind, name)` read) over rendered `docs`.

    `where` is `command` for a call in a container's command or args, or `configmap/<name>` for
    a call in a mounted ConfigMap's data. A ConfigMap is looked up in the CronJob's own
    namespace, and one the render does not hold is a hit too, since its data went unread.
    """
    configmaps = {
        (d["metadata"].get("namespace"), d["metadata"]["name"]): d
        for d in docs
        if d.get("kind") == "ConfigMap"
    }
    hits, read = [], set()
    for doc in docs:
        if doc.get("kind") != "CronJob":
            continue
        name, namespace = doc["metadata"]["name"], doc["metadata"].get("namespace")
        read.add(("CronJob", name))
        pod = pod_spec(doc)
        containers = (pod.get("initContainers") or []) + (pod.get("containers") or [])
        argv = json.dumps([(c.get("command"), c.get("args")) for c in containers])
        hits += [(name, "command") for reaper in sorted(REAPERS) if reaper in argv]
        for mounted in sorted(_mounted_configmaps(pod)):
            configmap = configmaps.get((namespace, mounted))
            if configmap is None:
                # An imperatively applied ConfigMap is not in the render, so nothing read it.
                hits.append(
                    (name, f"configmap/{mounted} (not rendered, so never read)")
                )
                continue
            read.add(("ConfigMap", mounted))
            data = json.dumps(configmap.get("data") or {})
            hits += [
                (name, f"configmap/{mounted}")
                for reaper in sorted(REAPERS)
                if reaper in data
            ]
    return hits, read


def _cronjob(command: list[str], configmap: str | None = None) -> dict:
    volumes = [{"name": "s", "configMap": {"name": configmap}}] if configmap else []
    container = {"name": "job", "image": "x", "command": command}
    return {
        "kind": "CronJob",
        "metadata": {"name": "nightly", "namespace": "homelab"},
        "spec": {
            "jobTemplate": {
                "spec": {
                    "template": {
                        "spec": {"containers": [container], "volumes": volumes}
                    }
                }
            }
        },
    }


def _configmap(name: str, script: str) -> dict:
    return {
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": "homelab"},
        "data": {"run.sh": script},
    }


def test_no_k8s_cronjob_runs_a_reaper() -> None:
    """The tree: no rendered CronJob, nor any ConfigMap one mounts, names a reaper."""
    hits, read = cronjob_reapers([doc for _, _, doc in rendered_docs()])
    assert hits == [], (
        f"a CronJob runs a reaper; they are operator-invoked only: {hits}"
    )
    missing = (CRONJOB_MEMBERS | MOUNTED_MEMBERS) - read
    assert not missing, f"the CronJob arm never read {sorted(missing)}"


def test_a_cronjob_command_naming_a_reaper_is_flagged() -> None:
    job = _cronjob(["python3", "/repo/scripts/backup/longhorn_reap.py", "backups"])
    assert cronjob_reapers([job])[0] == [("nightly", "command")]


def test_a_reaper_inside_a_mounted_script_configmap_is_flagged() -> None:
    """The arm's reason to exist: the command names only the script, the script the reaper."""
    job = _cronjob(["sh", "/scripts/run.sh"], configmap="nightly-script")
    script = _configmap(
        "nightly-script", "python3 longhorn_reap.py snapshots --apply\n"
    )
    assert cronjob_reapers([job, script])[0] == [
        ("nightly", "configmap/nightly-script")
    ]


def test_a_cronjob_naming_neither_is_clean() -> None:
    job = _cronjob(["sh", "/scripts/run.sh"], configmap="nightly-script")
    script = _configmap("nightly-script", "rsync -a /src /dst\n")
    hits, read = cronjob_reapers([job, script])
    assert hits == []
    assert read == {("CronJob", "nightly"), ("ConfigMap", "nightly-script")}


def test_a_mounted_configmap_the_render_does_not_hold_is_flagged() -> None:
    """A script ConfigMap applied by a task, outside the render, would otherwise read as clean."""
    job = _cronjob(["sh", "/scripts/run.sh"], configmap="applied-by-a-task")
    assert cronjob_reapers([job])[0] == [
        ("nightly", "configmap/applied-by-a-task (not rendered, so never read)")
    ]
