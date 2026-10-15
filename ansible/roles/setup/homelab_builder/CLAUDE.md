# `setup/homelab_builder` — the host-package builder and its apt repository (daniel-box)

The builder half of target-architecture slice H3, whose plan is
`docs/target-architecture/host/h3-pi-pilot.md`. Once armed, it builds the host packages from
master, signs them, and serves the apt repository the hosts' updater reads. This file holds
the rules for the skeleton; the plan's *The builder and the repository (daniel-box)* holds the
design.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "homelab_builder"` when `has_gitops`
- **Timer:** `homelab-builder.timer` (`OnBootSec=2min`, `OnUnitActiveSec=2min`)
<!-- /generated_from -->

## The dispatcher and the arm

`ansible/roles/setup/homelab_builder/defaults/main.yml:homelab_builder_enabled` is `true`.
`tasks/main.yml` dispatches on it: `install.yml` when true, `teardown.yml` when false. To
disarm the builder, set the flag to `false`; the setup-plane tick then applies the teardown
arm. The builder timer is the role's one scheduled actor, and *Autonomous-role contract*
below bounds it.

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
- One ufw rule per `homelab_builder_repo_clients` entry, each a peer's `server_ip`. The rule
  admits the two peer hosts and every pod on daniel-server, because flannel masquerades a
  pod's traffic to an address outside the pod CIDR to that node's LAN IP. That reach is
  accepted: the repository serves only signed, public packages, and nothing at L3 tells the
  host apart from its pods. The `# DECIDED:` marker in `defaults/main.yml` carries the
  reasoning. A client removed from that list keeps its rule. The install arm never deletes a
  rule, and the teardown deletes only the rules for the clients still in the list. To drop
  such a rule, run `sudo ufw delete allow from <ip> to any port 8791 proto tcp` by hand.

## What the builder half writes (H3.5a)

`tasks/builder.yml` installs the builder, which stages packages and publishes nothing until
H3.5b's signer.

- `/etc/homelab-builder/` (root, group `homelab-builder`, 0750). It holds `config.env` (0640),
  which carries the SOPS key `homelab_builder_github_token`, the deployer's CI contexts, its
  walk bound and its repo. It also holds `archive-key.fpr` (0644), which `install.yml` writes
  from the signer's key on every apply, because the builder cannot read the signer's GnuPG home.
  Every task that renders the token carries `no_log: true`.
- `/opt/homelab-builder/`, root-owned: the script `files/host_package_builder.py`, `host_lib.py`,
  `publish-textfile`, a copy of the operator's pinned uv, and nfpm at the version
  `homelab_builder_nfpm_version` pins in `ansible/roles/setup/homelab_builder/defaults/main.yml`.
  The directory sits outside the builder's `ReadWritePaths`, so a builder run cannot rewrite
  the code the next run executes, nor the `publish-textfile` the unit runs as root.
  `renovate.json`'s nfpm manager moves the role's pin and CI's in one PR, and
  `ansible/roles/setup/homelab_builder/tests/test_builder_units.py::test_the_nfpm_pin_is_ci_s`
  holds them equal.
- The pinned `host_python_version` under `/var/lib/homelab-builder/python`, installed by `uv
  python install` under `runuser`, as `common/tasks/agent_user.yml` installs an agent's.
- `homelab-builder.service`, sandboxed as the plan's *Sandboxing* lists, and
  `homelab-builder.timer`, every 2 minutes. No task starts the service:
  a first run that fails pages through `OnFailure=homelab-builder-alert.service` instead of
  failing the apply, which would hold every session's landings.

The builder reads three `gitops_deploy` defaults through the settings task's `vars:`. That
role's `defaults/main.yml` is therefore a row of
`ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py:SETUP_FILES_SHIPPED_BY_OTHER_ROLES`
naming this role, so a change to the CI contexts re-applies the builder's copy.

## The builder's run

Each run fetches master into its own bare clone, chooses the newest commit on the first-parent
chain since the last built one whose CI is green, renders every host package there with `uv
run --frozen`, checks the rendered keyring against `archive-key.fpr`, and builds each package
whose render changed with nfpm. It writes `staged/chosen` and the `.deb` files, all at one
version, `0.<first-parent count>+g<12-hex sha>`. A staged set the signer has not taken yet is
rebuilt at the new commit, because H3.5b's signer refuses a `staged/` whose versions name two
commits.

The anchor is master plus a green CI verdict, and nothing more. The `# DECIDED:` marker in the
script cites ADR-0021:
`ansible/roles/setup/homelab_builder/files/host_package_builder.py:DECIDED: the anchor is a commit`.

Every run past the fetch writes `homelab_builder.prom`, and the unit's `ExecStopPost=-+` line
copies it into the textfile directory. The `reason` label of
`homelab_builder_unbuilt_since_timestamp_seconds` is one of `ok`, `hold`, `ancestry`,
`keyring`, `ci`, `no-green`, `token` and `build`. `token` is a missing token, or a 401 or
403 from the check-runs read, which the anonymous fetch cannot see. `build` is a render or
nfpm failure at the chosen commit, a value the plan's list does not name. A worktree cleanup
that fails after a refusal is logged, and the run keeps the refusal's reason. A failing run exits 1 once per reason and tip,
then 0 on a repeat, so `OnFailure=` pages once for each condition. A failed fetch exits 0 and
writes nothing: the fetch timestamp then ages, which H3.5c's fetch-age arm pages on.

## Autonomous-role contract (the builder stages host packages with no human in the loop)

Every 2 minutes, `homelab-builder.timer` may build the host packages from a master commit and
replace the contents of `staged/`.

- **Scope / exclusions:** it writes `/var/lib/homelab-builder/` alone, which the unit's
  `ReadWritePaths=` enforces, plus its metric through `publish-textfile`. It **never** signs,
  publishes or touches `/var/lib/homelab-signer/`, and never builds a commit off master's
  first-parent chain or one whose CI is not green.
- **Mode (explicit + reversible):** `homelab_builder_enabled` arms the role; `false` and the
  setup-plane tick apply the teardown, which removes the timer, the units, the code and the
  settings.
- **Authoritative sources:** `origin/master` as fetched this run, GitHub's check-runs API
  graded over `CI_CONTEXTS`, and `archive-key.fpr`. It reads nothing from its clone to decide
  what to trust.
- **Abort valves:** an empty `CI_CONTEXTS` or a walk bound that is not a positive integer
  (`ci`), a tip that does not descend from the last built commit (`ancestry`), and a keyring
  that differs from `archive-key.fpr` (`keyring`). Each builds nothing. H3.5b adds the
  operator's `/etc/homelab-builder/hold` to the runbook; the script already honours it.
- **Required evidence:** `homelab_builder.prom` on every run past the fetch, and one Discord
  page per new failure through `OnFailure=`.
- **Next-run review:** before widening scope, read `journalctl -u homelab-builder` for the
  refusals since the last change, and the metric's `reason` label.

## The teardown keeps the signing key

The teardown stops and removes the units the role installs by H3.5b, the ufw rules for
the clients in `homelab_builder_repo_clients` at teardown time, `/var/lib/homelab-builder/`,
`/etc/homelab-builder/`, `/opt/homelab-builder/` and the builder's stamp fragments. It never
touches `/var/lib/homelab-signer/` or either user. The
GnuPG home holds the key every host trusts. A re-arm that recreated `homelab-signer` with a new
uid could no longer read that key. The `# DECIDED:` marker at the top of `tasks/teardown.yml`
carries the reasoning, and
`ansible/tests/setup/test_homelab_builder.py::test_the_teardown_never_names_the_signer_tree_is_clean`
holds it.

A failed tick apply of this role writes `hold_sha` and stops every session's landings.
`docs/host-packages.md` has the clear path.
