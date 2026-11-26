# `hosts/components/secret-rotation`

## Autonomous-role contract (the audit only reads and reports; it changes no credential)

- **Scope / exclusions:** the audit reads `secrets/secret_rotation.yml`, the key list of
  `secrets/secrets.yml`, those files' git history, the `gh` token's auth status, and the
  off-site recovery kit's refresh stamp `secrets/recovery_kit.yml`. It writes only its journal lines and its two textfiles, and stages nothing.
- **Mode (explicit and reversible):** the timer's and the boot unit's enable links in this package
  arm them. Dropping a link from a package version, or reverting this component, stops that unit
  on the install that drops it (reconcile's step 1).
  `systemctl stop homelab-secret-rotation-audit.timer` on daniel-box stops the audit until the
  next package install.
- **Authoritative sources:** `secrets/secret_rotation.yml`, the key list of
  `secrets/secrets.yml` and `secrets/recovery_kit.yml` in the primary checkout.
- **Abort valves:** a run that cannot source the check library or enter the checkout exits 1
  before it reads anything. The audit changes no state, so it holds no lock.
- **Required evidence, every run:** a `status=<up|down>` line tagged secret-rotation-audit in the
  journal, and secret-rotation-audit.prom in node_exporter's textfile directory, which
  SecretRotationAuditFailed and SecretRotationAuditNotReporting read.
  The kit check writes recovery-kit.prom, which RecoveryKitStale reads, and logs a
  `status=down` line tagged recovery-kit when the stamp is missing or too old.
- **Next-run review:** a `down` verdict names its cause on the summary line. An overdue secret is
  rotated by the runbook in docs/secret-rotation.md, and the verdict clears within 30 minutes.
  A stale recovery kit's verdict never fails the run, so Restart= does not rerun it. It
  clears at the next 08:30 run after the operator refreshes the kit and commits
  `scripts/secrets_mgmt/recovery_kit.py record`'s stamp.

ENFORCED: hosts/tests/host_checks/test_host_check_textfiles.py (the audit's behaviour
against stubs), hosts/tests/repo_crons/test_cron_scripts_publish_via_pr.py and
hosts/lib/tests/test_secret_rotation_component.py (the units and the legacy entries).
