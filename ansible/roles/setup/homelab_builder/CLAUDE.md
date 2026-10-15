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

## The dispatcher and the arm

`ansible/roles/setup/homelab_builder/defaults/main.yml:homelab_builder_enabled` is `true`.
`tasks/main.yml` dispatches on it: `install.yml` when true, `teardown.yml` when false. To
disarm the builder, set the flag to `false`; the setup-plane tick then applies the teardown
arm. H3.5a adds the builder timer and this file's autonomous-role contract. Until then the
role installs no timer or cron, so nothing it installs changes state on a schedule.

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

## What the install arm writes (H3.4b)

- The system users `homelab-builder` and `homelab-signer`, nologin, with no home created.
- `/var/lib/homelab-builder/` (0711) with `src/` and `uv-cache/` (0700). Its `staged/` is
  owned by the builder with group `homelab-signer` at 2770, because the builder writes it and
  the signer empties it.
- `/var/lib/homelab-signer/` (0711) with `gnupg/` (0700) and `repo/` (0755), the published
  tree.
- The archive signing key, generated once by `gpg --quick-generate-key` under `runuser -u
  homelab-signer`. A `become_user` task would need an interpreter the signer can execute.
  Its `creates:` guard is `gnupg/openpgp-revocs.d`, the one directory gpg writes only when it
  generates a key. Removing that directory makes the next apply generate a second key.
- `homelab-apt-repo.service`, which runs `python3 -m http.server` on `server_ip` at
  `homelab_builder_repo_port` over `repo/`, with `DynamicUser=yes`. apt trusts the signature
  on `InRelease`, not the transport.
- One ufw rule per `homelab_builder_repo_clients` entry, each a peer's `server_ip`. A client
  removed from that list keeps its rule until the teardown runs or someone deletes it by hand.

`/etc/homelab-builder/` and the builder's environment file arrive in H3.5a with the builder
units, so this arm reads no SOPS key.

## The teardown keeps the signing key

The teardown stops and removes the four units the role installs by H3.5b, the ufw rules and
`/var/lib/homelab-builder/`. It never touches `/var/lib/homelab-signer/` or either user. The
GnuPG home holds the key every host trusts. A re-arm that recreated `homelab-signer` with a new
uid could no longer read that key. The `# DECIDED:` marker at the top of `tasks/teardown.yml`
carries the reasoning, and
`ansible/tests/setup/test_homelab_builder.py::test_the_teardown_never_names_the_signer_tree_is_clean`
holds it.

A failed tick apply of this role writes `hold_sha` and stops every session's landings.
`docs/host-packages.md` has the clear path.
