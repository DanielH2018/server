"""The stale-render check only sees scripts named in `k3s_rendered_setup_scripts`.

manifest-prune-check.sh's third arm compares each template's source checksum against the one
stamped at render time, so a `template:`-rendered script that is NOT in that list is invisible
to it — the same silent-omission shape the arm exists to close (found 2026-08-23, when
/usr/local/bin/longhorn-backup-health.sh had been two commits stale for two days behind a
"deployed scripts match the repo" heartbeat).

Adding a shell template to the k3s role without adding it to the list is therefore a coverage
regression that nothing else would report, so it fails here instead.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_ROLE = _REPO / "ansible/roles/setup/k3s"
_TEMPLATES = _ROLE / "templates"

# Shell templates that are NOT rendered onto this host as standalone scripts, each with the
# reason. Anything here is exempt from the manifest; everything else must be in it.
_NOT_STANDALONE_SCRIPTS: set[str] = {
    # Sourced by the heartbeats rather than run — deployed to /usr/local/lib, not /usr/local/bin.
    "kuma-push-lib.sh.j2",
}


def _declared() -> list[str]:
    defaults = yaml.safe_load((_ROLE / "defaults/main.yml").read_text())
    return defaults["k3s_rendered_setup_scripts"]


def test_every_rendered_shell_template_is_in_the_manifest():
    on_disk = {p.name for p in _TEMPLATES.glob("*.sh.j2")} - _NOT_STANDALONE_SCRIPTS
    missing = on_disk - set(_declared())
    assert not missing, (
        "these shell templates are rendered onto the host but are not checksummed into "
        "/var/lib/homelab/setup-render-manifest, so a stale render of them is undetectable: "
        + ", ".join(sorted(missing))
    )


def test_the_manifest_names_no_template_that_stopped_existing():
    declared = set(_declared())
    stale = {t for t in declared if not (_TEMPLATES / t).is_file()}
    assert not stale, (
        "k3s_rendered_setup_scripts names templates that no longer exist, which would make the "
        "check page forever: " + ", ".join(sorted(stale))
    )


def test_the_check_reads_the_manifest_it_is_given():
    """Pins the two halves to the same path — they are written in different files (a task and a
    shell template), which is exactly how a rename silently disarms the arm."""
    task = (_ROLE / "tasks/health-crons.yml").read_text()
    script = (_TEMPLATES / "manifest-prune-check.sh.j2").read_text()
    path = "/var/lib/homelab/setup-render-manifest"
    assert path in task, "health-crons.yml no longer writes the render manifest"
    assert path in script, (
        "manifest-prune-check.sh.j2 no longer reads the render manifest"
    )


def test_an_absent_manifest_is_not_reported_as_drift():
    """A host that predates the mechanism has no manifest; that is unproven coverage, not a
    failure, and paging for it would train the operator to ignore this monitor."""
    script = (_TEMPLATES / "manifest-prune-check.sh.j2").read_text()
    assert "render manifest absent" in script
    # The absence branch must not contribute to STALE, which is what drives status=down.
    absent_branch = script.split("else", 1)[-1]
    assert "MANIFEST_NOTE=" in absent_branch
