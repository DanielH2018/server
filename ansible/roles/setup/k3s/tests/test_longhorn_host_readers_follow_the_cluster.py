"""The backup-health reader takes arm state and run times from the cluster, not from the shim (H1.7a).

Each test renders `longhorn-backup-health.sh.j2` once and runs the reader against a stubbed
kubectl, changing only what the stub answers. The environment stays what the rendered shim
exports, so the script is fixed while the cluster flips. That is the stale-cron case the old
exported shape could not see: from H1.7b Flux applies a flipped arm flag without a host apply,
and a baked value would then disagree with the cluster until someone ran `k3s-bringup.yml` by
hand. Before H1.7b the `setup/k3s` tasks write the objects, and the reader reports them.

The stubs and the env builder are in `_longhorn_reader_stubs.py`, whose `_CLUSTER_OBJECTS_PY`
documents the knobs. `scripts/validate/tests/test_backup_health_shim.py` holds the shim's side:
no export of the four arm and schedule vars, and a render that does not move when their keys do.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_host_readers_follow_the_cluster.py
"""

import re
import subprocess
import time

import pytest
from _longhorn_reader_stubs import (
    NOW,
    _green_path_stub_kubectl,
    _grace_pair_stub_kubectl,
    _reader_env,
    _rfc3339,
    reader_argv,
)
from lib.repo_paths import K3S_ROLE
from validate import shell_templates as v
from validate.validate_lib import shell_lint as sl

SHIM = K3S_ROLE / "templates" / "longhorn-backup-health.sh.j2"

# The shim exports that are thresholds. The rest name host paths and binaries, which a test
# points at a temporary directory or a stub instead.
_THRESHOLD = re.compile(
    r"LONGHORN_(?:[A-Z_]*MAX_AGE_HOURS|DAILY_BACKUP_BUDGET|RESTORE_DRILL_(?:MAX_AGE|COVERAGE_SLACK)\w*"
    r"|CRON_EVIDENCE_WINDOW_HOURS)"
)


def _shim_thresholds() -> dict[str, str]:
    """The threshold exports of the rendered shim, as the cron would give the reader."""
    rendered = sl.render_template(SHIM, v.template_context(SHIM))
    exports = dict(re.findall(r'^export (LONGHORN_\w+)="([^"]*)"', rendered, re.M))
    return {k: val for k, val in exports.items() if _THRESHOLD.fullmatch(k)}


def _run(tmp_path, stub, **stub_env):
    """The reader under the shim's exports, against `stub`, with `stub_env` changing the cluster."""
    drill_dir = tmp_path / "drill"
    drill_dir.mkdir(exist_ok=True)
    (drill_dir / "last-success").write_text(str(int(NOW - 3600)))
    env = _reader_env(
        tmp_path,
        LONGHORN_BACKUP_KUBECTL=str(stub),
        LONGHORN_RESTORE_DRILL_STAMP_DIR=str(drill_dir),
        **_shim_thresholds(),
        **stub_env,
    )
    proc = subprocess.run(
        reader_argv(now=NOW), capture_output=True, text=True, env=env, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_the_shim_thresholds_are_found():
    """Non-vacuity: the rendered shim exports the thresholds these tests hand the reader."""
    found = _shim_thresholds()
    assert "LONGHORN_BACKUP_MAX_AGE_HOURS" in found
    assert "LONGHORN_DAILY_BACKUP_BUDGET" in found


def test_a_target_with_a_url_that_is_unavailable_is_flagged(tmp_path):
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(NOW - 60))
    out = _run(tmp_path, stub, STUB_DEFAULT_AVAILABLE="false")
    assert out.startswith("down\t"), out
    assert "backup target default unavailable: STUB_UNAVAILABLE_REASON default" in out


def test_the_same_script_with_the_url_blanked_is_clean_and_names_the_disarm(tmp_path):
    """Only the stub changes. A blank URL is a disarm, so `default`'s tier is not required."""
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(NOW - 60))
    out = _run(tmp_path, stub, STUB_DEFAULT_AVAILABLE="false", STUB_DEFAULT_URL="")
    assert out.startswith("up\t"), out
    assert "DISARMED: default (2 volume(s) not checked)" in out
    assert "backup target(s) r2 available" in out


def test_a_blank_r2_url_is_named_and_leaves_the_default_target_watched(tmp_path):
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(NOW - 60))
    out = _run(tmp_path, stub, STUB_R2_URL="", STUB_R2_AVAILABLE="false")
    assert out.startswith("up\t"), out
    assert "backup target(s) default available" in out
    assert "DISARMED: r2 (0 volume(s) not checked)" in out


def _daily_cron_at(epoch: float) -> str:
    """A daily cron that fires at the local time of `epoch`, which the reader reads in local time."""
    when = time.localtime(epoch)
    return f"{when.tm_min} {when.tm_hour} * * *"


def test_the_daily_job_cron_moves_the_first_run_grace(tmp_path):
    """`pvc-new` was created eight hours before the pinned clock; only the stub's cron differs.

    The grace lasts until six hours after the volume's first scheduled run. A daily job at
    created + 1h ends it at created + 7h, before the clock. A job at created + 5h ends it at
    created + 11h, after the clock.
    """
    created = NOW - 8 * 3600
    stub = _grace_pair_stub_kubectl(
        tmp_path, created_ts=_rfc3339(created), old_backup_ts=_rfc3339(NOW - 3600)
    )
    early = _run(
        tmp_path, stub, STUB_R2_URL="", STUB_DAILY_CRON=_daily_cron_at(created + 3600)
    )
    late = _run(
        tmp_path,
        stub,
        STUB_R2_URL="",
        STUB_DAILY_CRON=_daily_cron_at(created + 5 * 3600),
    )
    assert early.startswith("down\t") and "new-data" in early, early
    assert (
        late.startswith("up\t") and "awaiting their first scheduled backup" in late
    ), late


def test_a_failed_target_list_is_flagged_and_never_read_as_a_disarm(
    tmp_path, logger_calls
):
    """The list fails while the stub's URL is blank: the volumes stay checked, not suppressed.

    `pvc-new` is uncovered (created 8h ago, cron an hour after). It is reported only if the
    reader kept `default` in the watched set after it could not read the list.
    """
    created = NOW - 8 * 3600
    stub = _grace_pair_stub_kubectl(
        tmp_path, created_ts=_rfc3339(created), old_backup_ts=_rfc3339(NOW - 3600)
    )
    out = _run(
        tmp_path,
        stub,
        STUB_DAILY_CRON=_daily_cron_at(created + 3600),
        STUB_DEFAULT_URL="",
        STUB_FAIL_TARGET_LIST="1",
    )
    assert out.startswith("down\t"), out
    logged = logger_calls.read_text()
    assert "backup target list fetch failed" in logged, logged
    assert "DISARMED" not in logged, logged
    assert "no backup from daily-backup" in logged, logged


@pytest.mark.parametrize("missing", ["default", "r2"])
def test_a_list_without_a_target_is_flagged(tmp_path, logger_calls, missing):
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(NOW - 60))
    out = _run(tmp_path, stub, STUB_DROP_TARGET=missing)
    assert out.startswith("down\t"), out
    assert f"backup target {missing} is not in the cluster" in logger_calls.read_text()


def test_a_blank_target_held_by_the_break_glass_annotation_is_flagged_and_named(
    tmp_path, logger_calls
):
    """H1.7b: the annotation means a break-glass is in force, so the blank is not a disarm.

    Only the stub differs from the clean blank-URL case above. `default` carries the annotation
    Flux's apply skips, and the monitor reads DOWN until step 4 of the break-glass removes it.
    """
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(NOW - 60))
    out = _run(
        tmp_path,
        stub,
        STUB_DEFAULT_AVAILABLE="false",
        STUB_DEFAULT_URL="",
        STUB_DEFAULT_ANNOTATION="1",
    )
    assert out.startswith("down\t"), out
    logged = logger_calls.read_text()
    assert (
        "backup target default carries kustomize.toolkit.fluxcd.io/reconcile=disabled"
        in logged
    )
    assert "DISARMED: default" not in logged, logged


def test_the_same_blank_target_without_the_annotation_stays_clean(tmp_path):
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(NOW - 60))
    out = _run(tmp_path, stub, STUB_DEFAULT_AVAILABLE="false", STUB_DEFAULT_URL="")
    assert out.startswith("up\t"), out
    assert "DISARMED: default" in out
