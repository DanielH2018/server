#!/usr/bin/env python3
"""The restore drill's CronJob is suspended, bounded, and carries the script untouched (H2.3d).

The Job's `activeDeadlineSeconds` must outlast the restore's own timeout plus the probe pod's
bound, or the kubelet kills a slow-but-healthy restore and the run reads as a restore failure.
It must also sit far below the daily period, or two nights overlap. The CronJob ships
`suspend: true` because the host cron and the CronJob name their restore the same
(`restore-drill-<pvc>`), so a run of each could tear down the other's.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_restore_drill_deadline.py
"""

import re
import tempfile
from pathlib import Path

from _k8s_render import render_role_template
from _restore_drill import DRILL_DEFAULTS, render
from _helpers import load_yaml
from lib import yaml_fast
from renderer import homelab_render

TAG = "longhorn-restore-drill"
DAY_SECONDS = 86400


def _cronjob() -> dict:
    (doc,) = yaml_fast.safe_load_all(render_role_template(TAG, "cronjob.yaml.j2"))
    return doc


def _probe_bound_seconds() -> int:
    """The probe pod's wait, read from the script rather than restated here."""
    match = re.search(r"PROBE_DEADLINE=\$\(\( \$\(date \+%s\) \+ (\d+) \)\)", render())
    assert match, "the script's probe deadline moved — update this reader"
    return int(match.group(1))


def test_the_deadline_clears_the_restore_timeout_plus_the_probe_bound() -> None:
    defaults = load_yaml(DRILL_DEFAULTS)
    job = _cronjob()["spec"]["jobTemplate"]["spec"]
    deadline = job["activeDeadlineSeconds"]
    assert deadline == defaults["longhorn_restore_drill_deadline_seconds"]
    floor = defaults["k3s_longhorn_restore_drill_timeout_s"] + _probe_bound_seconds()
    assert deadline > floor, (
        f"deadline {deadline}s leaves no margin over the restore timeout plus the probe bound "
        f"({floor}s): the kubelet would kill a restore that is still within its own limits"
    )
    assert deadline < DAY_SECONDS // 4, (
        "the deadline must sit far below the daily period"
    )


def test_the_grace_period_covers_one_poll_and_the_four_cleanup_deletes() -> None:
    pod = _cronjob()["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["terminationGracePeriodSeconds"] == 300


def test_the_cronjob_ships_suspended_and_never_overlaps() -> None:
    spec = _cronjob()["spec"]
    assert spec["suspend"] is True, (
        "H2.3e unsuspends it, after the host cron file is gone"
    )
    assert spec["concurrencyPolicy"] == "Forbid"
    assert spec["timeZone"] == "Etc/UTC"


def test_the_script_reaches_the_pod_with_its_expansions_intact() -> None:
    """Flux substitutes `${VAR}` only where the tag's Kustomization carries a `postBuild`."""
    (doc,) = yaml_fast.safe_load_all(
        render_role_template(TAG, "script-configmap.yaml.j2")
    )
    script = doc["data"]["longhorn-restore-drill.sh"]
    assert 'DRILL="restore-drill-${PVC}"' in script
    out = Path(tempfile.mkdtemp())
    assert homelab_render.cluster(out, [TAG], None) == 0
    published = (out / "flux" / f"{TAG}.yaml").read_text()
    assert "postBuild" not in published, (
        "the tag substitutes, so Flux would blank every ${VAR} in the drill script"
    )
