# `setup/k8s_host_files` — the host-side rows of k8s roles, applied through the setup plane

This role holds the host writes that D5 moves out of the k8s roles before each role flips to
Flux: docs's site directory, traefik's absent arm, the absent arms H2 leaves in crowdsec, configarr, janitorr and qbittorrent,
observability's retired dashboard staging trees. A Flux-owned
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
- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, no
  `templates/*.timer.j2`)
<!-- /generated_from -->

## What it holds

Each conversion PR adds one task file per source role, named after that role, and imports it
from `tasks/main.yml` with `ansible.builtin.import_tasks`. The import is static so the play's
`k8s_host_files` tag reaches every task in the file. docs' conversion PR wrote the first,
`tasks/docs.yml`, which created the site directory docs' Deployment mounts as a hostPath of
`type: Directory`. H7.10a moved that directory into the docs-refresh component's `tmpfiles.d`
file (`hosts/components/docs-refresh/`) and deleted the task file. artifacts' PR wrote a task
file for the three trees artifacts' Deployment mounts the same way, and H7.40 moved them into
the claude-code component's `tmpfiles.d` file and deleted it. media-volume's PR wrote a task file for the
directory media-volume's static PersistentVolume names and its `media/` and `torrents/`
subdirectories, and H7.35 moved them into daniel-box's `tmpfiles.d` file and deleted it. configarr's PR wrote `tasks/configarr.yml`, the
absent arms that remove H2.5b's retired host health reader: its cron, its script, its working
directory and its drift fragment. observability's dashboard conversion wrote
`tasks/observability.yml`, an absent arm over the three trees its old `tasks/dashboards.yml`
staged under `/etc/rancher/k3s/`. janitorr's PR wrote `tasks/janitorr.yml`, which holds the absent arms for the retired health
reader's host cron, its script and code directory, and its drift fragment. pi-peer-backup's flip wrote a task file for the forced-command wrapper
and the pull key on daniel-pi. H7.25 moved both into the Pi package
(`hosts/components/pi-peer-backup-target/`) and deleted the file. qbittorrent's PR wrote `tasks/qbittorrent.yml`, which holds
the absent arms for the prefs drift check's retired host cron and script. traefik's PR wrote
`tasks/traefik.yml`, the two absent arms that remove the retired Cloudflare IP drift cron and
its script. registry's push-store GC
retirement wrote `tasks/registry.yml`, the absent arm for the `gc-job.yaml` manifest nothing
reads any more. registry's conversion PR added the ghcr-mirror GC's host side to it, and H7.64
moved that into `hosts/components/registry-gc/`. #6987 added an absent arm in
`tasks/registry.yml` that removes the retired apply-time record directory from daniel-box.
crowdsec's first conversion
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
- **A copy sits in its task's `vars:`.** A task file with several tasks wraps them in one
  `block:` and puts the copies in the block's `vars:`. Role defaults are play-wide in
  `initial_setup.yml`, so a defaults copy of a key another setup role names becomes a cross-role
  read, which `ansible/tests/setup/test_setup_cross_role_reads.py` refuses without a row in the
  deployer's cross-role table (#5472). Task vars reach only the task.

`ansible/tests/setup/test_k8s_host_files_pairs.py` holds those rules. It derives every key this
role's imported task `vars:` share with a k8s role's defaults or `group_vars/all.yml`, and
asserts that each copy equals its source. Its census names no key: media-volume's copy moved
to daniel-box's package in H7.35, where `hosts/lib/tests/test_box_k3s_host_half.py` holds it
equal, and pi-peer-backup's
`pi_peer_backup_src_dir` moved into the `pi-peer-backup-target` component's authorized-key line
in H7.25.

`ansible/tests/setup/test_k8s_host_files_rows.py` names every moved row by task
file and task name with its whole argument map. It fails when a row is no longer imported or
its path, owner, group or mode changes, and pins a looped row's `loop`. Each
conversion PR adds its rows there. A row cannot move back to its k8s role, because
`scripts/renderer/tests/test_renderer_runbook_allowlist.py` refuses the role a task file.

## Autonomous-role contract (it will remove crons with no human in the loop)

The GitOps tick applies the role as `initial_setup.yml --tags k8s_host_files` on every merge
that touches it. It holds host directories, the absent arms for configarr's, janitorr's and
crowdsec's retired crons.

- **Scope / exclusions:** host files, directories and units that a k8s role's Ansible apply
  owned before its flip, plus the absent arms that remove a retired role's crons. **Never** a
  Kubernetes object, never a `kubectl` write from a task, and never a row outside the D5 list
  above. No task writes on daniel-pi.
- **Mode (explicit + reversible):** each row carries the arm variable its source role used.
  Removing a row is a revert of its conversion PR, which puts the task back in the k8s role.
  Reverting D5.1h removes the playbook entry and parks the tick once more.
- **Authoritative sources:** the repo at the commit being applied, through this role's own
  defaults and `group_vars`. Never another role's defaults.
- **Abort valves:** the tick's CI gate and `hold_sha`. A failed apply writes `hold_sha` and a
  `hold_plane` entry naming `initial_setup.yml`, and the tick applies nothing more until the
  hold clears.
- **Required evidence:** the tick's receipt and `land.sh`'s `VERDICT:` line for every PR that
  changes this role.
- **Next-run review:** before moving a new row in, run `--list-tasks` with
  `--tags k8s_host_files` and read the recap of the last apply.
