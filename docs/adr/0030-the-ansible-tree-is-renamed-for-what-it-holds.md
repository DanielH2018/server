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
([#8186](https://github.com/DanielH2018/server/issues/8186)). The same day, the operator
ruled that the rename merges only after every reader deployed separately from the merge
accepts both paths and is installed.

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

The `ansible/` tree is renamed to these directories:

| Before | After |
|---|---|
| `ansible/roles/k8s/<role>/` | `k8s/roles/<role>/` |
| `ansible/roles/conftest.py` | `k8s/roles/conftest.py` |
| `ansible/templates/` | `k8s/templates/` |
| `ansible/inventory/` | `inventory/` |
| `ansible/vars/` | `secrets/` |
| `ansible/.sops.yaml` | `secrets/.sops.yaml` |
| `ansible/secret_rotation.yml` | `secrets/secret_rotation.yml` |
| `ansible/tests/` | `tests/` |
| `ansible/README.md` | deleted; this record replaces it |

No alias or symlink keeps an old path.

The rename lands in two steps:

1. Each installed reader accepts both the old and the new path. That change ships in the
   host packages, and the packages are installed on their hosts.
2. The tree moves, together with every reader that runs from a checkout. The approval list
   and the readers in step 1 drop their `ansible/` paths in a follow-up, once the moved
   tree's packages are installed.

## Consequences

- Between the two steps, the approval list carries entries that match nothing. The list
  already held one such entry before the cluster-base role existed, so a prefix with no
  match costs nothing.
- Step 2 moves every path consumer at once: `.gitignore`'s allow lines, `.gitattributes`'
  `diff=sops` path, the `.sops.yaml` creation rules, `pyproject.toml`'s `testpaths`, rule
  globs, prek regexes, CI workflows, docs generators, skills and agent briefs. The fan-out
  pauses for it, and every open branch that touches `ansible/` rebases afterwards.
- `secrets/secret_rotation.yml` holds no secret, but it sits under a directory that the
  generic `.sops.yaml` creation rule matches. That rule applies only when someone runs `sops`
  on the file, so step 2 keeps the register plaintext as long as nobody does.
- Copies of `ansible/` paths outside the repository do not move with it. The operator's
  memory index and the dotfiles repo's briefs are updated after step 2 lands.

## Governs

No single line enforces this record. The tests that hold the installed readers to the new paths
are `hosts/tests/test_claude_lander.py`,
`hosts/components/renovate-agent/tests/test_land_renovate_pr.py`,
`hosts/lib/tests/test_host_package_builder_python.py` and
`hosts/components/hypervisor/tests/test_suc_rehearsal.py`.
