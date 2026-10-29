# `setup/k8s_host_files` — the host-side rows of k8s roles, applied through the setup plane

This role holds the host writes that D5 moves out of the k8s roles before each role flips to
Flux: docs's site directory, traefik's absent arm, registry's
ghcr-mirror GC, the absent arms H2 leaves in crowdsec, configarr, janitorr and qbittorrent,
artifacts' three directory tasks (the star list directory among them), media-volume's media
directories, observability's retired dashboard staging trees, uptime-kuma's stale
autokuma-claims staging directory. A Flux-owned
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
- **Crons (1):**
  - `Registry garbage collection` — `20 4 * * 0`
<!-- /generated_from -->

## What it holds

Each conversion PR adds one task file per source role, named after that role, and imports it
from `tasks/main.yml` with `ansible.builtin.import_tasks`. The import is static so the play's
`k8s_host_files` tag reaches every task in the file. docs' conversion PR wrote the first,
`tasks/docs.yml`, which created the site directory docs' Deployment mounts as a hostPath of
`type: Directory`. H7.10a moved that directory into the docs-refresh component's `tmpfiles.d`
file (`hosts/components/docs-refresh/`) and deleted the task file. artifacts' PR wrote `tasks/artifacts.yml`, which creates the three trees
artifacts' Deployment mounts the same way. media-volume's PR wrote `tasks/media-volume.yml`,
which creates the directory media-volume's static PersistentVolume names and its `media/` and
`torrents/` subdirectories, with no mode. configarr's PR wrote `tasks/configarr.yml`, the
absent arms that remove H2.5b's retired host health reader: its cron, its script, its working
directory and its drift fragment. observability's dashboard conversion wrote
`tasks/observability.yml`, an absent arm over the three trees its old `tasks/dashboards.yml`
staged under `/etc/rancher/k3s/`. janitorr's PR wrote `tasks/janitorr.yml`, which holds the absent arms for the retired health
reader's host cron, its script and code directory, and its drift fragment. pi-peer-backup's flip wrote a task file for the forced-command wrapper
and the pull key on daniel-pi. H7.25 moved both into the Pi package
(`hosts/components/pi-peer-backup-target/`) and deleted the file. qbittorrent's PR wrote `tasks/qbittorrent.yml`, which holds
the absent arms for the prefs drift check's retired host cron and script. traefik's PR wrote
`tasks/traefik.yml`, the two absent arms that remove the retired Cloudflare IP drift cron and
its script. uptime-kuma's PR wrote `tasks/uptime-kuma.yml`, the absent arm that removes
the directory k8s/volume-claim staged autokuma-data's claim under. registry's push-store GC
retirement wrote `tasks/registry.yml`, the absent arm for the `gc-job.yaml` manifest nothing
reads any more. registry's conversion PR added the ghcr-mirror GC's host side to it: the Job
manifest, `registry-gc.sh`, `/var/lib/registry-gc` and the `registry-gc` cron. #6536 added the
dockerhub-mirror's Job manifest, `templates/dockerhub-mirror-gc-job.yaml.j2`, which the same
script applies in a second phase. crowdsec's first conversion
PR wrote `tasks/crowdsec.yml`, which installed the edge probe's script, service and timer on
daniel-pi for crowdsec's off-cluster verify. H5.14 moved those into the Pi package
(`hosts/components/crowdsec-probe/`) and deleted the tasks, so no timer is left here.
crowdsec's second conversion PR (#5742) added the absent arms for the four crons
H2.4b and H2.4c retired and their scripts. They run on daniel-box. They moved only after H2.4c deployed, because in crowdsec's own role they ran
ahead of its manifests, and the setup plane cannot order itself against a k8s deploy. A row drops its source role's `when: not k8s_dry_run` guard and its `config` tag,
because `initial_setup.yml` has no dry-run mode.

- **The playbook entry is gated to daniel-box by a literal.** `setup_routing.py` places a role
  whose gate admits only another host as a hand apply, so the tick could never run a Pi-gated
  role. No row writes on daniel-pi any more: crowdsec's probe timer left in H5.14 and
  pi-peer-backup's wrapper and key in H7.25. A Pi write belongs in the Pi package.
- **A task moves with its sources and variables.** Role defaults and role-relative `src:` paths
  do not resolve across roles, so a conversion PR copies the role's host templates and files
  into this role's `templates/` and `files/`. A variable only the host files read moves to this
  role's `defaults/main.yml`. No task here runs `include_vars` on another role's defaults.
- **A variable both planes read stays in the k8s role, with a copy here.** The copy keeps the
  k8s role's own name. A change then edits both, and each half routes to its own plane. A
  shared key in `group_vars/all.yml` routes to the deploy plane only, which never runs
  `initial_setup.yml`.
- **A copy sits in its task's `vars:`, not in this role's defaults, unless a template reads it.** A task file with
  several tasks wraps them in one `block:` and puts the copies in the block's `vars:`, as
  `tasks/artifacts.yml` does. Role defaults are play-wide in `initial_setup.yml`, so a
  defaults copy of a key another setup role names becomes a cross-role read, which
  `ansible/tests/setup/test_setup_cross_role_reads.py` refuses without a row in the deployer's
  cross-role table (#5472). A copy of `sys_user` or another `group_vars` key would make
  this role's defaults read by nearly every setup role. Task vars reach only the task.
  The exception is a key that only this role's templates and rows name. The strict render test
  and the shell-template gate resolve a template's names from role defaults, and Renovate
  reaches a setup role's defaults, so registry's image pins and GC settings sit in
  `defaults/main.yml`. No other setup role names a `registry_k8s_*` key.

Two tests in `ansible/tests/setup/` hold those rules:

- `ansible/tests/setup/test_k8s_host_files_render.py` renders every template here under
  `StrictUndefined`, with this role's defaults and the inventory. A template reading a
  variable that only a `roles/k8s/` defaults file defines fails it. Its named members are
  the registry GC templates.
- `ansible/tests/setup/test_k8s_host_files_pairs.py` derives every key this role's defaults or
  imported task `vars:` share with a k8s role's defaults or `group_vars/all.yml`, and asserts
  that each copy equals its source. artifacts' PR added `artifacts_host_dir`, `artifacts_peer_dir`, `artifacts_peer_sources`
  and `artifacts_stars_dir`. media-volume's PR added `media_volume_host_path` and the
  `group_vars` keys `puid` and `pgid`, copied so that the setup plane re-applies the owner
  when they change, and pi-peer-backup's flip added `pi_peer_backup_src_dir`, which
  H7.25 moved into the `pi-peer-backup-target` component's authorized-key line.

A third test, `ansible/tests/setup/test_k8s_host_files_rows.py`, names every moved row by task
file and task name with its whole argument map. It fails when a row is no longer imported or
its path, owner, group or mode changes, and pins a looped row's `loop`. Each
conversion PR adds its rows there. A row that moves back to its k8s role fails the host-write
census in `ansible/tests/repo/_host_write_rows.py`, whose `allow` entry the move dropped.

`ansible/roles/setup/k8s_host_files/tests/test_ghcr_mirror_prune.py` runs the Jobs' prune
script against a store tree built in `tmp_path`. Both mirrors' Jobs run that one script.

## Autonomous-role contract (it will install a timer and remove crons with no human in the loop)

The GitOps tick applies the role as `initial_setup.yml --tags k8s_host_files` on every merge
that touches it. It holds host directories, the absent arms for configarr's, janitorr's and
crowdsec's retired crons.

- **Scope / exclusions:** host files, directories and units that a k8s role's Ansible apply
  owned before its flip, plus the absent arms that remove a retired role's crons. **Never** a
  Kubernetes object, never a `kubectl` write from a task, and never a row outside the D5 list
  above. One cron is the exception: `registry-gc.sh` runs weekly as root. It scales
  `deploy/ghcr-mirror` and then `deploy/dockerhub-mirror`, one at a time, and applies the Job
  manifest this role stages for each. The task installs the script and the cron and runs
  neither. The operator accepted that script as the one writer of the ghcr-mirror's
  `spec.replicas` (#5732), and #6536 extends that to the dockerhub-mirror's. No task writes on
  daniel-pi.
- **Mode (explicit + reversible):** each row carries the arm variable its source role used.
  `registry_k8s_manage_gc: false` skips the whole registry GC block, as it did in the registry
  role. It only skips: a host that already has the cron keeps it, so disarming a host means
  removing `/etc/cron.d/registry-gc` by hand (operator-only).
  Removing a row is a revert of its conversion PR, which puts the task back in the k8s role.
  Reverting D5.1h removes the playbook entry and parks the tick once more.
- **Authoritative sources:** the repo at the commit being applied, through this role's own
  defaults and `group_vars`. Never another role's defaults.
- **Abort valves:** the tick's CI gate and `hold_sha`. A failed apply writes `hold_sha` and a
  `hold_plane` entry naming `initial_setup.yml`, and the tick applies nothing more until the
  hold clears.
- **Required evidence:** the tick's receipt and `land.sh`'s `VERDICT:` line for every PR that
  changes this role.
- **Registry GC evidence:** the weekly `registry-gc` run (Sunday 04:20) writes a `Registry GC`
  Kuma push and a `registry-gc` healthchecks.io ping, and the `registry-gc` journal
  (`journalctl -t registry-gc`) shows both mirror phases. A conversion apply leaves the cron
  file and the script's hash unchanged. After the restore the run warms the mirror: it GETs
  blobs through the mirror's loopback hostPort and writes nothing to the cluster. The push
  message ends with the warm counts, such as `warmed 354 blob(s), 6207 MB in 133s`.
- **Next-run review:** before moving a new row in, run `--list-tasks` with
  `--tags k8s_host_files` and read the recap of the last apply.
