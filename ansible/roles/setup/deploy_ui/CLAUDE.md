# deploy_ui — the host half of deploy.local

`deploy-ui.service` on the GitOps host (`has_gitops`), `User={{ sys_user }}`, cwd the primary
checkout. It serves the page and API `roles/k8s/deploy-ui` routes to, and runs `land.sh`,
`deploy.sh`, `probe.py releases` and `gh` exactly as the operator would from a terminal.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates or playbook entry. -->
- **Applied by:** `initial_setup.yml --tags "deploy_ui"`
- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, no
  `templates/*.timer.j2`)
<!-- /generated_from -->

## Trust

The daemon does no authentication. Two gates stand in front of it; removing either is a
visible decision:

- ufw admits `deploy_ui_port` from `deploy_ui_allowed_sources` only (the pod CIDR and every
  node IP, because flannel masquerades pod→LAN traffic to the sending node).
  ENFORCED: `ansible/tests/setup/test_deploy_ui_firewall_sources.py`.
- Authelia `two_factor` gates `deploy.local`: `auth_tier: two_factor` on the `deploy-ui`
  containers_list entry, rendered into the authelia role's access_control rules.
  ENFORCED: `ansible/tests/services/test_authelia_access_tiers.py`.

DECIDED: a pod inside the cluster reaches the daemon without Authelia. Accepted 2026-09-10 —
a hostile pod already has kubectl-adjacent reach. Nothing stops it at the network layer:
netpol-baseline is INGRESS ONLY (`roles/k8s/netpol-baseline/templates/networkpolicy.yaml.j2`),
so pod egress is unfenced on this cluster.
Every POST also needs `X-Deploy-UI: 1` so a cross-site form cannot ride the Authelia cookie.

## Turning it off

- Stop and disable the daemon on daniel-box: `systemctl disable --now deploy-ui.service`.
- Remove the `deploy-ui` entry from `containers_list` in `host_vars/daniel-box.yml` and
  deploy, which drops the route (`roles/k8s/deploy-ui`) and `deploy.local`.
- The ufw task only ADDS rules, one per `deploy_ui_allowed_sources` entry. A source removed
  from that list keeps its rule until someone deletes it by hand:
  `ufw delete allow proto tcp from <src> to any port 8790`.

## In flight

The panel reads the per-service locks, not the tree lock alone. `deploy.sh` holds
`/var/lock/server-git-tree.lock` for the snapshot only (ADR-0017), so a page that read that
one lock said `free` for nearly all of a deploy, and its own deploy button queued on
`server-deploy-<tag>.lock` with nothing on the page saying so (#1844).

Two sources, because neither alone says who holds what. One merged-stream `fuser` call over
the tree lock and every `server-deploy-*.lock` under `DEPLOY_UI_LOCKS` names the processes
with each file OPEN — and a queued deploy has the file open too: `deploy.sh` opens the
descriptor, then blocks on it, so fuser lists the waiter exactly like the holder (measured:
a `flock` holder and a `flock -w` waiter both appear). `/proc/locks` is the kernel's own
table: a granted flock per file, and a `->` line with a live pid for each process blocked
on it. The pid on a granted line can be dead — `deploy.sh` takes its locks with a `flock`
child on an inherited descriptor — which is why the holder comes from fuser and only the
waiter from `/proc/locks`. Measured on daniel-box: fuser over the 31 lock files takes 45 ms.

`ps` with `ppid` folds each process family — a landing with the `deploy.sh` it spawned, a
`deploy.sh` with its playbook — to one row carrying `locks` (held) and `waiting_on`. The run
pattern also matches `deploy_run.py`: the `deploy.sh` shim execs `uv run … deploy_run.py`,
which stays the family root while `deploy_locked.sh` runs under it
(`docs/deploy-sh-python-port.md`), so no process names `deploy.sh`. A
holder that matches no run pattern (the GitOps tick on the tree lock) is a `lock` row rather
than nothing. `deploy.sh --list-services`, which the daemon itself runs on every deploy
POST, is excluded by name. fuser lists only processes whose `/proc/<pid>/fd` this user can
read; every deployer here runs as `{{ sys_user }}`, the daemon's own user.

Only a `land` row is cancellable. SIGTERM to a deploy mid-play leaves whatever applied
before it live (`deploy.sh` exit 20), which is not a cancel.

`test_lock_names_agree_with_deploy_locks_is_clean` pins the lock names to `deploy_locks.py`
and to the tree-lock default in `deploy_locked.sh`, because the daemon runs outside the venv and cannot import them.

## Writes

Each write spawns the command detached, logs to `~/.local/state/deploy-ui/`, and emits one
logfmt line via `logger -t deploy-ui`. Land and deploy refuse under `hold_sha`; the hold
clears only as the `hold_sha` + `hold_plane` pair against a SHA the operator typed.

`hold_plane` holds one entry per failed apply, and Clear drops all of them whatever is still
unapplied. The state panel lists the entries one per line, the confirm prompt names them, and
the reply repeats them — after the Clear nothing records those planes at all (#2453).

`/api/deploy` takes one SERVICE tag. `deploy.sh --list-services` also prints the block tags
(`config`, `deploy`, `cron`) and Ansible's `always`, each of which selects every container
role at once, so `writes.NON_SERVICE_TAGS` subtracts them from the allowlist and the guard
refuses one with a 409. A test asserts that literal equals `BLOCK_TAGS | RESERVED_TAGS` in
`scripts/deploy_tools/deploy_tags.py`, because the daemon runs outside the repo venv and
cannot import it.

Log names carry a `mkstemp` suffix as well as the second-granular timestamp. Two writes in
one second used to name the same file, and the second open truncated a log the first process
still held.

## Deploy

`uv run ansible-playbook ansible/initial_setup.yml --tags deploy_ui` (a setup-plane role;
`deploy.sh` does not carry it). Verify: `curl -s http://10.0.0.215:8790/api/state` from the
box.
