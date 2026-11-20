# `hosts/components/pi-heartbeats`

## Autonomous-role contract (the container-recovery heartbeat restarts what it finds dead)

The other four heartbeats only read and report. This one acts: pi-recovery-health runs `docker start` on any watched container it finds not running, every 5 minutes, unattended.

- **Scope / exclusions:** exactly the containers this host deploys, which is every
  compose_services entry in inventory/host_vars/daniel-pi.yml plus the
  docker-proxy's sub-proxies. One `docker start` per watched container per cycle and nothing else: never a
  restart, recreate, `docker rm` or pull, and nothing on the other two hosts.
  docs/pi-host-tuning-record.md has why the watch set is the deploy list rather than
  `docker ps -a --filter status=exited`.
- **Compose read:** one read of homelab_compose.prom in node_exporter's textfile
  directory, the file homelab-compose.service's compose-run writes. A result of 0 makes the
  cycle report `down`, naming `homelab-compose` and the result's SHA. The read acts on nothing, so
  it adds no action to this contract. An absent file adds nothing, because it is absent by
  design until the first run. A file that exists but cannot be read, or holds no result line,
  reports `down` and is never treated as absent.
- **Why it acts at all:** `restart: unless-stopped` covers a process exit, not a failed create
  at the OCI runtime, which is what this 512 MB board actually produces.
- **Mode (explicit and reversible):** optimize_pi_recovery_restart_enabled arms the timer and
  ships true. Setting it false in inventory/host_vars/daniel-pi.yml drops the timer's enable
  link from the next package version; reconcile stops the timer on the install that drops it
  and leaves the script installed. docs/pi-host-tuning-record.md has the disarm walkthrough,
  including why disarming doubles as the heartbeat's own failure signal.
- **Authoritative sources:** `docker ps` against the real socket, not docker-proxy, so it reports
  docker-proxy's own death too. `docker inspect` is read BEFORE the restart, which erases the
  exit code, the daemon's Error string and FinishedAt.
- **Abort valves:** a missing homelab-check-lib.sh exits before any `docker start`, and a
  container that will not start costs one failed start per cycle.
- **Required evidence, every cycle:** pi-recovery-health.prom in node_exporter's textfile
  directory, and a /var/log/pi-health/health.log line naming what died and whether
  the restart took.
- **It reports `down` on a cycle it fixed, on purpose:** an `up` after a successful restart would
  hide a container crashing every 5 minutes. The script's `# DECIDED:` marker has the
  declined re-proposal.
- **Next-run review:** a repeat `restarted:` for the same container needs fixing upstream. Read
  the week's status=down lines before widening the watch set.

ENFORCED: hosts/tests/test_pi_recovery_restarts_and_reports.py::test_a_dead_container_is_restarted,
which renders the script and runs it against a stub `docker`.
