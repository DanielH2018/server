# `setup/k8s_host_files` — the host-side rows of k8s roles, applied through the setup plane

This role holds the host writes that D5 moves out of the k8s roles before each role flips to
Flux: docs's site directory, pi-peer-backup's two Pi writes, traefik's absent arm, registry's
GC absent arms, the absent arms H2 leaves in crowdsec, configarr, janitorr and qbittorrent,
artifacts' three directory tasks (the star list directory among them), media-volume's media
directories, observability's retired dashboard staging trees, uptime-kuma's stale
autokuma-claims staging directory and crowdsec's edge probe timer on daniel-pi. A Flux-owned
role has no Ansible apply left to carry them, so each lands here in its own conversion PR. The plan is
`docs/target-architecture/deploy/d5-bulk-move.md`, sections *Host-side rows* and *The
`k8s_host_files` role enters the setup plane once (D5.1h)*.

It is not in `containers_list`, so `./scripts/deploy.sh --tags k8s_host_files` exits 2 on an
unmatched tag. Apply it through `initial_setup.yml` on daniel-box:

```bash
uv run ansible-playbook ansible/initial_setup.yml --tags k8s_host_files > /tmp/k8s-host-files.log 2>&1 < /dev/null
```

The redirect is what a shell with non-blocking stdio needs.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "k8s_host_files"` when `inventory_hostname ==
  'daniel-box'`
- **Timer:** `crowdsec-edge-probe.timer` (`OnBootSec=1min`, `OnUnitActiveSec=1min`)
<!-- /generated_from -->

## What it holds

Each conversion PR adds one task file per source role, named after that role, and imports it
from `tasks/main.yml` with `ansible.builtin.import_tasks`. The import is static so the play's
`k8s_host_files` tag reaches every task in the file. docs' conversion PR wrote the first,
`tasks/docs.yml`, which creates the site directory docs' Deployment mounts as a hostPath of
`type: Directory`. artifacts' PR wrote `tasks/artifacts.yml`, which creates the three trees
artifacts' Deployment mounts the same way. media-volume's PR wrote `tasks/media-volume.yml`,
which creates the directory media-volume's static PersistentVolume names and its `media/` and
`torrents/` subdirectories, with no mode. configarr's PR wrote `tasks/configarr.yml`, the
absent arms that remove H2.5b's retired host health reader: its cron, its script, its working
directory and its drift fragment. observability's dashboard conversion wrote
`tasks/observability.yml`, an absent arm over the three trees its old `tasks/dashboards.yml`
staged under `/etc/rancher/k3s/`. janitorr's PR wrote `tasks/janitorr.yml`, which holds the absent arms for the retired health
reader's host cron, its script and code directory, and its drift fragment. pi-peer-backup's flip wrote `tasks/pi-peer-backup.yml`,
which installs the forced-command wrapper and authorizes the pull key on daniel-pi, and moved
both files into `files/`. qbittorrent's PR wrote `tasks/qbittorrent.yml`, which holds
the absent arms for the prefs drift check's retired host cron and script. traefik's PR wrote
`tasks/traefik.yml`, the two absent arms that remove the retired Cloudflare IP drift cron and
its script. uptime-kuma's PR wrote `tasks/uptime-kuma.yml`, the absent arm that removes
the directory k8s/volume-claim staged autokuma-data's claim under. crowdsec's first conversion
PR wrote `tasks/crowdsec.yml`, which installs the edge probe's script, service and timer on
daniel-pi for crowdsec's off-cluster verify. It is the role's first timer and its first
templates. crowdsec's absent arms stay in its own `tasks/main.yml` until a later conversion
PR, because they must run before crowdsec's manifests apply. A row drops its source role's `when: not k8s_dry_run` guard and its `config` tag,
because `initial_setup.yml` has no dry-run mode.

- **The playbook entry is gated to daniel-box by a literal.** `setup_routing.py` places a role
  whose gate admits only another host as a hand apply, so the tick could never run a Pi-gated
  role. A row that writes on daniel-pi keeps `delegate_to: daniel-pi` inside a task here, as
  pi-peer-backup's block does.
- **A task moves with its sources and variables.** Role defaults and role-relative `src:` paths
  do not resolve across roles, so a conversion PR copies the role's host templates and files
  into this role's `templates/` and `files/`. A variable only the host files read moves to this
  role's `defaults/main.yml`. No task here runs `include_vars` on another role's defaults.
- **A variable both planes read stays in the k8s role, with a copy here.** The copy keeps the
  k8s role's own name. A change then edits both, and each half routes to its own plane. A
  shared key in `group_vars/all.yml` routes to the deploy plane only, which never runs
  `initial_setup.yml`.
- **A copy sits in its task's `vars:`, never in this role's defaults.** A task file with
  several tasks wraps them in one `block:` and puts the copies in the block's `vars:`, as
  `tasks/artifacts.yml` does. Role defaults are play-wide in `initial_setup.yml`, so a
  defaults copy of a key another setup role names becomes a cross-role read, which
  `ansible/tests/setup/test_setup_cross_role_reads.py` refuses without a row in the deployer's
  cross-role table (#5472). A copy of `sys_user` or another `group_vars` key would make
  this role's defaults read by nearly every setup role. Task vars reach only the task.

Two tests in `ansible/tests/setup/` hold those rules:

- `ansible/tests/setup/test_k8s_host_files_render.py` renders every template here under
  `StrictUndefined`, with this role's defaults and the inventory. A template reading a
  variable that only a `roles/k8s/` defaults file defines fails it. crowdsec's edge probe
  units are its named members.
- `ansible/tests/setup/test_k8s_host_files_pairs.py` derives every key this role's defaults or
  imported task `vars:` share with a k8s role's defaults or `group_vars/all.yml`, and asserts
  that each copy equals its source. docs' PR added the first pair, `docs_host_site_dir`, and
  artifacts' PR added `artifacts_host_dir`, `artifacts_peer_dir`, `artifacts_peer_sources`
  and `artifacts_stars_dir`. media-volume's PR added `media_volume_host_path` and the
  `group_vars` keys `puid` and `pgid`, copied so that the setup plane re-applies the owner
  when they change, and pi-peer-backup's flip added `pi_peer_backup_src_dir`, the one
  directory the wrapper lets the key read. crowdsec's edge probe copies nothing: its service
  reads the VIP from `group_vars`, its one allowed source, so moving the VIP needs a hand apply
  of this role. It also holds
  `docs-refresh.sh.j2`'s `SITE_DIR` equal to docs' default, the third copy of that path.

A third test, `ansible/tests/setup/test_k8s_host_files_rows.py`, names every moved row by task
file and task name with its whole argument map. It fails when a row is no longer imported or
its path, owner, group or mode changes, and pins a looped row's `loop`. It also holds
pi-peer-backup's two rows and crowdsec's four to `delegate_to: daniel-pi`. Each
conversion PR adds its rows there. A row that moves back to its k8s role fails the host-write
census in `ansible/tests/repo/_host_write_rows.py`, whose `allow` entry the move dropped.

## Autonomous-role contract (it will install a timer and remove crons with no human in the loop)

The GitOps tick applies the role as `initial_setup.yml --tags k8s_host_files` on every merge
that touches it. It holds host directories, configarr's and janitorr's absent arms,
pi-peer-backup's two daniel-pi writes and crowdsec's edge probe timer on daniel-pi.

- **Scope / exclusions:** host files, directories and units that a k8s role's Ansible apply
  owned before its flip, plus the absent arms that remove a retired role's crons. **Never** a
  Kubernetes object, never a `kubectl` write, and never a row outside the D5 list above. A
  daniel-pi write happens only through `delegate_to: daniel-pi`.
- **Mode (explicit + reversible):** each row carries the arm variable its source role used.
  Removing a row is a revert of its conversion PR, which puts the task back in the k8s role.
  Reverting D5.1h removes the playbook entry and parks the tick once more.
- **Authoritative sources:** the repo at the commit being applied, through this role's own
  defaults and `group_vars`. Never another role's defaults.
- **Abort valves:** the tick's CI gate and `hold_sha`. A failed apply writes `hold_sha` and a
  `hold_plane` entry naming `initial_setup.yml`, and the tick applies nothing more until the
  hold clears.
- **Required evidence:** the tick's receipt and `land.sh`'s `VERDICT:` line for every PR that
  changes this role. crowdsec's probe timer appends `<time> http=<code>` once a minute to
  `/var/log/pi-health/crowdsec-edge-probe.status`, which the Pi's Alloy counts by status.
  Read the file's tail over `ssh daniel-pi`, or the counter with `probe.py metric
  'loki_process_custom_crowdsec_edge_probe_total'`. To stop the probe, revert
  `tasks/crowdsec.yml`, then disable `crowdsec-edge-probe.timer` on the Pi by hand
  (operator-only). The file then stops growing and stays bounded.
- **Next-run review:** before moving a new row in, run `--list-tasks` with
  `--tags k8s_host_files` and read the recap of the last apply.
