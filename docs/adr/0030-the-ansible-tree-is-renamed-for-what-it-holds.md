---
id: "0030"
title: The ansible/ tree is renamed for what it holds, after its installed readers accept both paths
status: Accepted
date: 2026-11-13
governs: []
---

# ADR-0030: The ansible/ tree is renamed for what it holds, after its installed readers accept both paths

## Status

Accepted on 2026-11-13. The operator approved the rename on 2026-11-12, from the
post-migration audit
([#8186](https://github.com/DanielH2018/server/issues/8186)), and named `k8s/roles`,
`k8s/templates`, `inventory/` and `secrets/` as example names. The same day, the operator
ruled that the rename merges only after every reader deployed separately from the merge
accepts both paths and is installed.

The operator confirmed the four names that installed code reads on 2026-11-13, after the PR
that taught those readers both paths had installed. Step 2 settled the other rows of the table
below.

## Context

`ansible/` no longer holds Ansible. No playbook exists, no role has `tasks/`, ansible-core is
absent from the lock, and `ansible/roles/` holds only `k8s/`
([ADR-0029](0029-each-host-is-configured-by-its-own-package-not-ansible.md)). All five
reviewers of the post-migration audit named the directory name as the root cause of the drift
they found. The name still drives rule globs, agent briefs, test paths and module names.

Most readers of these paths run from a checkout, so they change in the same commit as the
tree. Four readers are installed from a host package instead, and a host package installs
after the merge, not with it:

- The approval list of the agent's landing unit, `/etc/claude-land/approval-paths`, from
  `hosts/components/claude-code/templates/claude-land-approval-paths.j2`. Its entries for the
  secrets and the cluster-base role are literal paths.
- The Renovate agent's lander, `land_renovate_pr.py`. It refuses a PR that changes the
  inventory or a denylisted role, and it matches both by path prefix.
- The host-package builder's `builder_python.py`. It reads the commit's
  `host_python_version` from the inventory.
- The SUC rehearsal helper, `suc_rehearsal.py`. It globs the k8s roles' defaults in a
  checkout.

A rename that merged first would leave each of these reading a path that no longer exists.
The approval list and the Renovate lander would fail open: a change to the secrets or to the
inventory would land without the refusal that guards it.

## Decision

The `ansible/` tree is renamed to these directories. The installed readers name the first
four rows, so a change to one of them costs another install cycle:

| Before | After | Fixed by |
|---|---|---|
| `ansible/roles/k8s/<role>/` | `k8s/roles/<role>/` | installed readers |
| `ansible/inventory/` | `inventory/` | installed readers |
| `ansible/vars/` | `secrets/` | installed readers |
| `ansible/.sops.yaml` | `secrets/.sops.yaml` | installed readers |
| `ansible/roles/conftest.py` | `k8s/conftest.py` | step 2 |
| `ansible/templates/` | `k8s/templates/` | step 2 |
| `ansible/secret_rotation.yml` | `secrets/secret_rotation.yml` | step 2 |
| `ansible/tests/` | `tests/` | step 2 |
| `ansible/README.md` | deleted; this record replaces it | step 2 |
| `scripts/lib/ansible_inventory.py` | `scripts/lib/inventory_lists.py` | step 2 |
| `scripts/lib/ansible_jinja_env.py` | `scripts/lib/guard_jinja_env.py` | step 2 |

No alias or symlink keeps an old path.

The rename lands in two steps:

1. Each installed reader accepts both the old and the new path. That change ships in the
   host packages, and the packages are installed on their hosts.
2. The tree moves, together with every reader that runs from a checkout. The approval list
   and the readers in step 1 drop their `ansible/` paths in a follow-up, once the moved
   tree's packages are installed.

The follow-up ran in two parts. 83aed3de3 ([#8407](https://github.com/DanielH2018/server/issues/8407)) dropped the
`ansible/` paths from the installed readers and their tests. The T2 approval floor kept
`ansible/roles/k8s/homelab-mcp/` while the homelab-mcp pod still mounted the node's `ansible/`
tree. [#8372](https://github.com/DanielH2018/server/issues/8372) dropped that last entry once [#8409](https://github.com/DanielH2018/server/issues/8409) moved the pod to
`k8s/`.

## Consequences

- Between the two steps, the approval list carries entries that match nothing. The list
  already held one such entry before the cluster-base role existed, so a prefix with no
  match costs nothing.
- Step 2 moves every path consumer at once: `.gitignore`'s allow lines, `.gitattributes`'
  `diff=sops` path, the `.sops.yaml` creation rules, `pyproject.toml`'s `testpaths`, rule
  globs, prek regexes, CI workflows, docs generators, skills and agent briefs. The fan-out
  pauses for it, and every open branch that touches `ansible/` rebases afterwards.
- The rotation register holds no secret. Under `secrets/`, the generic `.sops.yaml`
  creation rule and any check that expects every YAML file there to be encrypted would also
  match it. Step 2 checks `test_sops_creation_rules.py` and the protected-edits hook before it
  places the register.
- Copies of `ansible/` paths outside the repository do not move with it. The dotfiles
  repo's auto-mode classifier text names the secrets, inventory and cluster-base paths, and it
  deploys through `chezmoi apply`, so it names both paths before step 2 merges
  ([dotfiles#1261](https://github.com/DanielH2018/dotfiles/issues/1261)). claude-guard
  matches the basenames of the encrypted files, which the rename keeps. The operator's memory
  index and the dotfiles repo's other briefs are updated after step 2 lands.

## What step 2 found

- The roles' conftest sits at `k8s/conftest.py`, not inside `k8s/roles/`. Every walk over the
  roles directory reads each entry as a role, and a `conftest.py` there is not one.
- `lib.repo_paths` drops `ANSIBLE`, `ROLES` and `ANSIBLE_TESTS`. `K8S`, `K8S_ROLES`,
  `SHARED_TPL`, `INVENTORY`, `SECRETS` and `TESTS` name the new trees.
- The SOPS creation rules match at the start of the path or after `secrets/`, such as
  `(^|secrets/)hosts/daniel-pi\.sops\.yaml$`. Each rule then matches a path relative to
  `secrets/`, to the repo root, or absolute, and matches no file outside `secrets/`, so
  `sops --config secrets/.sops.yaml` run on any other file finds no rule.
  `tests/repo/test_sops_creation_rules.py` holds both halves. The generic rule matches a YAML
  file directly under `secrets/` or `secrets/cluster-manifests/`, and no per-host file. The
  plaintext rotation register matches it, and no command encrypts it.
- The secret-rotation history walk reads `secrets.yml` at both paths. A `git log` on the new
  path alone starts at the rename commit and would date every secret to it.
- The render changes in one place only. Ten verify Jobs and deploy gates name their
  `homelab/rerun-safe` test at its new path, because the renderer refuses a node ID that names
  no test in the tree. The annotation is metadata, and no Job name changes. The render-diff
  gates, `scripts/validate/render_diff_gates.py`, read a base before the rename at its
  `ansible/` layout, so they judge a range that crosses the rename.
- The homelab-mcp pod still mounts the node's `ansible/` through a hostPath, and is the one
  node-local reader step 2 does not move. It runs on daniel-server, whose checkout no timer
  syncs, so a mount of `k8s/` there would hold the pod in ContainerCreating. Its placement is
  a preference, not a requirement. If daniel-server is down, the pod schedules on daniel-box,
  where the synced checkout holds no `ansible/` tree, and it fails to mount or serves an
  ignored-file leftover. Moving it waits for a checkout the pod's node keeps current
  ([#8409](https://github.com/DanielH2018/server/issues/8409)). #8409 resolved it: the pod
  requires `k8s_primary_node`, daniel-box, whose checkout `checkout-sync` keeps current, and
  mounts `k8s/`, `inventory/` and `tests/` in place of `ansible/`.
- The readers that compare two commits, the forward-only pin gate in `cluster_pins.py` and
  `promote_lib/forward_only.py`, read both layouts. A range whose base predates the rename
  would otherwise read no pin and no forward-only role on that side.
- Comments inside shipped `templates/` and `files/` keep their `ansible/` paths. Each one
  reaches a rendered manifest or an image, and rewriting it would change the render.

## Governs

No single line enforces this record. The tests that hold the installed readers to the new paths
are `hosts/tests/test_claude_lander.py`,
`hosts/components/renovate-agent/tests/test_land_renovate_pr.py`,
`hosts/lib/tests/test_host_package_builder_python.py` and
`hosts/components/hypervisor/tests/test_suc_rehearsal.py`.
