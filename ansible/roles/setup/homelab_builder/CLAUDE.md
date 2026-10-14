# `setup/homelab_builder` — the host-package builder and its apt repository (daniel-box)

The builder half of target-architecture slice H3, whose plan is
`docs/target-architecture/host/h3-pi-pilot.md`. Once armed, it builds the host packages from
master, signs them, and serves the apt repository the hosts' updater reads. This file holds
the rules for the skeleton; the plan's *The builder and the repository (daniel-box)* holds the
design.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "homelab_builder"` when `has_gitops`
- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, no
  `templates/*.timer.j2`)
<!-- /generated_from -->

## A disarmed skeleton, and why it is its own PR

`ansible/roles/setup/homelab_builder/defaults/main.yml:homelab_builder_enabled` is `false`.
`tasks/main.yml` dispatches on it: `install.yml` when true, `teardown.yml` when false. Both
arms are empty, so an apply changes nothing on the host. H3.4b fills both arms and flips the
flag; H3.5a adds the builder timer and this file's autonomous-role contract.

The playbook entry carries `when: has_gitops`, so the role reaches daniel-box only.
`scripts/deploy_tools/setup_lib/setup_gates.py` reads that gate for the deployer's routing,
from `group_vars/all.yml` and `host_vars/daniel-box.yml`, with no inventory change.

**Adding or removing the `initial_setup.yml` entry parks the GitOps deployer**, because
`initial_setup.yml` is in `_BROAD_MANUAL_PREFIXES`. The clear is operator-only, since the
play's `load_secrets` pre_task decrypts:

1. In the primary checkout, run `git log --oneline HEAD..origin/master` and
   `git diff --name-only HEAD..origin/master`. Go on only when the range holds the entry's PR
   alone, or list each other change with the apply it owes and run each after step 3.
2. `git merge --ff-only origin/master`.
3. `uv run ansible-playbook ansible/initial_setup.yml --tags homelab_builder`.

After the clear, a change under this role's directory lands through the setup-plane tick.
