# `hosts/components/k3s-host-checks`

## Autonomous-role contract (timers that read the cluster and report, with no human in the loop)

- **Scope / exclusions:** each check reads and reports. None changes the cluster or a backup store.
  `etcd-token-drift.sh` compares the cluster token with the operator's stamp and writes neither.
  The two reapers delete stranded objects, operator-invoked and dry-run by default. Scheduling
  either is out of contract, and they run from the checkout, so this component ships nothing of
  theirs (tests/longhorn/test_longhorn_reap_never_scheduled.py).
- **Mode (explicit and reversible):** each timer's enable link arms it. Dropping the component stops
  the timers on the install that drops it. `k3s_etcd_restore_drill_armed` arms the weekly drill,
  which runs `--list-only` here. `systemctl stop <timer>` stops one until the next install.
- **Authoritative sources:** the Longhorn objects through the read-only kubeconfig, the journal, the
  installed etcd drop-in, the root filesystem and the cluster token file.
- **Abort valves:** the boot grace in the two */10 checks, and the reader's own per-call deadlines.
- **Required evidence, every run:** a check textfile that a Prometheus rule reads, and a
  Healthchecks.io ping. Checks 9 and 10 read the B2 accounting's journal lines AS WRITTEN;
  docs/k3s-node-plane-crons-and-incidents.md names the regex and the file to edit together.
- **Next-run review:** read the B2 ledger before widening scope.

ENFORCED: hosts/lib/tests/test_box_k3s_host_half.py, and the tests in this component's `tests/`.
