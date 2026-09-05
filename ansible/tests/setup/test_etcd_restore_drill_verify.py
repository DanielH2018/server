"""The etcd restore drill's verification thresholds, issue #1017.

`scripts/backup/etcd_restore_drill.sh` restores a snapshot into a throwaway data-dir, brings up a
scratch API server on it, and then asserts the object graph came back: >=3 namespaces, >=1
Deployment, >=1 PVC. Nothing fed it a degraded snapshot and confirmed it refuses — the two
existing tests cover adjacent things (this cron's wiring, and the monitor that reads its result
stamp), neither drives the thresholds themselves. CLAUDE.md's rule bites hardest here: a restore
drill is the check you find out about only when you need it, and three thresholds that fire on
nothing would report a healthy drill against an empty snapshot.

The verification stage is pulled into its own function, `verify_restored_objects()`, specifically
so it can be driven here without a real restore. Sourcing the script for that would otherwise run
the whole drill — argument parsing, the root check, live S3 credentials — so the script itself
carries a `BASH_SOURCE` guard (the same one `verify_staging_gate_key.sh` uses) that returns before
any of that when it is sourced rather than executed. A stub `k3s` function stands in for the real
`kubectl get ... | wc -l` pipeline: it reports the counts the test hands it, the same way
`kuma-push-lib.sh`'s tests stub `curl`.
"""

import subprocess

from _helpers import REPO

_SCRIPT = REPO / "scripts" / "backup" / "etcd_restore_drill.sh"


def _run_verify(ns: int, deploys: int, pvcs: int) -> subprocess.CompletedProcess:
    """Source the drill script and call verify_restored_objects() against a stub kubectl.

    The stub answers `k3s kubectl --kubeconfig ... get <resource> [-A] --no-headers` by printing
    `ns`/`deploys`/`pvcs` lines for namespaces/deployments/pvc respectively (secrets and crds are
    fixed at 1 line — this test is only about the three thresholds that gate the drill). Each
    line stands for one object, since the real check only ever counts `wc -l` output.
    """
    script = f"""
    source "{_SCRIPT}"
    k3s() {{
      # args land as: kubectl --kubeconfig <path> get <resource> [-A] --no-headers
      local resource=""
      for arg in "$@"; do
        case "$arg" in
          namespaces|deployments|pvc|secrets|crd) resource="$arg" ;;
        esac
      done
      local n=1
      case "$resource" in
        namespaces)  n={ns} ;;
        deployments) n={deploys} ;;
        pvc)         n={pvcs} ;;
      esac
      local i=0
      while [ "$i" -lt "$n" ]; do echo x; i=$((i + 1)); done
    }}
    KUBECTL=(k3s kubectl --kubeconfig /tmp/does-not-matter.kubeconfig)
    SNAPSHOT=test-snapshot
    verify_restored_objects
    echo "VERIFY_OK"
    """
    return subprocess.run(
        ["bash", "-c", script, "_"],
        capture_output=True,
        text=True,
    )


def test_a_healthy_restore_passes():
    # ACCEPT: a plausible cluster's worth of objects clears all three thresholds.
    result = _run_verify(ns=5, deploys=12, pvcs=8)
    assert result.returncode == 0, result.stderr
    assert "VERIFY_OK" in result.stdout


def test_zero_namespaces_is_refused():
    # REJECT: an empty snapshot restoring with no namespaces at all — the case the finding named.
    result = _run_verify(ns=0, deploys=12, pvcs=8)
    assert result.returncode == 1
    assert "VERIFY_OK" not in result.stdout
    assert "only 0 namespaces" in result.stderr
    assert "carries no cluster" in result.stderr


def test_zero_deployments_is_refused():
    # REJECT: namespaces came back but no workloads did — the second threshold, on its own.
    result = _run_verify(ns=5, deploys=0, pvcs=8)
    assert result.returncode == 1
    assert "VERIFY_OK" not in result.stdout
    assert "no deployments in the restored set" in result.stderr


def test_zero_pvcs_is_refused():
    # REJECT: the third threshold, on its own — a rebuild would have nothing to reattach.
    result = _run_verify(ns=5, deploys=12, pvcs=0)
    assert result.returncode == 1
    assert "VERIFY_OK" not in result.stdout
    assert "no PVCs in the restored set" in result.stderr


def test_sourcing_the_script_runs_no_live_side_effects():
    """The BASH_SOURCE guard must fire on a plain source — proves the test harness above is
    actually safe to run as a non-root, credential-less user, not merely that it happens to."""
    result = subprocess.run(
        ["bash", "-c", f'source "{_SCRIPT}"; echo "SOURCED_OK"', "_"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "SOURCED_OK" in result.stdout
    assert "must run as root" not in result.stderr
