# Does a looped `include_role` select what `initial_setup.yml`'s static `roles:` selects?

Measured on 2026-10-10 for #4265, ahead of #3734. #3734 proposes one `setup_roles:` list that
`ansible/initial_setup.yml` loops over with `include_role`, the way `ansible/deploy.yml` loops
over `containers_list`. The question was whether that loop keeps the playbook's role tags and
the per-block tags inside each role, which the deployer's narrowed setup apply selects
(`scripts/deploy_tools/narrow_setup.py`, #3138).

## Result

**The loop selects the same tasks, in the same order, for every tag on every host.** #3734 can
loop at runtime; tag selection does not force a generated playbook. A runtime loop still has
costs that a generated playbook avoids. Ansible can no longer list the playbook's tasks or tags,
18 test modules and the 4 production readers behind them parse the `roles:` list and would have to change, and a cross-role
notify into a gated role fails the play instead of being skipped. *What the loop costs* weighs
them against the generator a generated playbook needs.

| Host | Tags compared | Match | Tasks with no `--tags` (static / looped) |
|---|---|---|---|
| daniel-box | 101 | 101 | 638 / 638 |
| daniel-server | 101 | 101 | 548 / 548 |
| daniel-pi | 101 | 101 | 429 / 429 |

The 101 tags are every tag `initial_setup.yml --list-tags` printed on 2026-10-10: the 100 task
tags, which include every role tag, plus a run with no `--tags`. `narrow_setup.py` emits a subset of
those task tags, so the set covers every value it can produce. The `<role>:<block>` spelling is
how `hold_plane` records a narrowed apply, not a tag Ansible selects on.

The issue's third check also holds:

- **A gated-off role is still skipped.** On a host where a role's gate is false, the role's
  tag runs 6 tasks in both shapes: the `initial_setup` role's `always`-tagged tasks in
  `crons.yml` and its two `stamp_deployed.yml` imports. On its own host it runs the
  whole role, for example `renovate_notify` 18 on daniel-box and 6 elsewhere, and
  `optimize_pi` 79 on daniel-pi and 6 elsewhere.
- **The self-dispatching roles still reach their teardown half.** `docker-teardown` (15
  tasks), `hypervisor-teardown` (16) and `gitops-deploy-teardown` (28) run on all three hosts
  in both shapes, because `docker_install`, `hypervisor` and `gitops_deploy` stay unconditional
  entries.

## Why `--list-tasks` could not answer it

The issue's step 2 compares `ansible-playbook initial_setup.yml --list-tasks --tags <tag>`
output. Against a looped `include_role` that output holds one line, the include itself:

```
  play #1 (daniel-box): Probe	TAGS: []
    tasks:
      Setup role {{ setup_role.role }}	TAGS: [always]
```

`include_role` is dynamic, so Ansible resolves the role name only while the play runs, and
`--list-tasks` and `--list-tags` never see the role's tasks. A listing diff therefore reports a
difference for every tag, and that difference says nothing about tag inheritance.

## Method

The comparison ran both playbooks and recorded which tasks actually executed. A scratch harness
copied every directory under `ansible/roles/setup/`, `common` included, because several roles
`import_tasks` from `{{ role_path }}/../common/tasks/`. In the copy, it replaced every leaf
task with an `ansible.builtin.debug` that carries the task's own `tags:` and a unique name. Blocks,
`import_tasks`, `include_tasks` and their `apply:` stayed as they were. The harness removed every
`when:`, loop, `notify:` and `become` inside the roles, so both halves of each dispatch exist
and nothing but `debug` runs. Role `defaults/` and `vars/` were copied unchanged.

Both playbooks ran against the repo inventory with `connection: local`, once per tag and host
(`-e target=<host>`). The static playbook kept the `roles:` list unchanged. The looped playbook was
this shape:

```yaml
tasks:
  - name: "Setup role {{ setup_role.role }}"
    ansible.builtin.include_role:
      name: "{{ setup_role.role }}"
      apply:
        tags: "{{ setup_role.tags }}"
    loop: "{{ setup_roles }}"
    loop_control:
      loop_var: setup_role
    when: not (setup_role.gated | default(false)) or (lookup('vars', '_setup_gate_' ~ setup_role.role) | bool)
    tags: [always]
```

Each gated entry's original `when:` became a play variable, `_setup_gate_<role>`, evaluated in
the host's context. A `when:` written as a string inside a list item is data, so Ansible does not
evaluate it. #3734 has to encode placement some other way, which the next section covers.

Two controls show the harness can report a difference. Both ran on daniel-box:

| Control | `gitops_deploy` | `chezmoi` | `docker-engine` |
|---|---|---|---|
| `apply:` removed | 60 / 6 | 12 / 12 | 12 / 12 |
| `always` removed from the include | 60 / 0 | 12 / 0 | 12 / 0 |

Without `apply:`, a role tag selects only the tasks that carry that tag themselves. Without
`always`, `--tags` skips the include, so the role never loads and none of its tasks run.

The harness removed every `notify:`, so a separate probe measured handlers. It used two
minimal roles: role `a` defines a handler, and role `b`'s task notifies it.

| Shape | `a` included | `a` gated off |
|---|---|---|
| static `roles:` | handler runs | handler silently does not run, exit 0 |
| looped `include_role` | handler runs | play fails, exit 1 |

The looped failure reads `The requested handler 'Handler from a' was not found in either the
main handlers list nor in the listening handlers list`. The probe returned the same result with
`--tags b`, and with `b` placed before `a` in the list. In this tree, `optimize_pi` notifies two
handlers that `initial_setup` defines (`ansible/tests/setup/test_setup_handlers_resolve.py`).
`initial_setup` is unconditional, so that notify resolves in both shapes. A future
cross-role notify into a GATED role would fail the play under a loop, where the static playbook
skips it silently. That test reads the `roles:` list, so it is one of the readers below and can
carry the rule.

The harness and the probe leave two differences unmeasured, and neither applies to this tree:

- **A static entry's `when:` is evaluated per task, and a looped include evaluates its `when:` once.** Static `roles:`
  append the entry's `when:` to every task in the role. The two shapes differ only if a role
  changes its own gate variable while it runs. No setup role sets `has_github_cli`,
  `has_chezmoi`, `has_claude_code`, `ups_host`, `nut_host_secondary_armed` or any
  `*_host` placement variable.
- **`include_role` defaults to `public: false`**, so a role's `defaults/` and `vars/` stop
  being visible to the roles after it. A census of every key in a setup role's `defaults/` or
  `vars/` against the other roles' files found one match: a comment in
  `claude_code/defaults/main.yml` naming `gitops_deploy_fence_ruleset_exclude`. No role reads
  another role's variable.

## What the loop costs

- **Ansible can no longer list the playbook.** `--list-tasks` and `--list-tags` print only the
  include. The repo uses listing as a diagnostic: `deploy_remediation.py` tells the operator that
  "`--list-tasks` shows what one selects", and a skipped task prints nothing, so listing is how a
  tag miss is told apart from a false `when`. The k3s listing tests read `k3s-bringup.yml` and
  are unaffected. The diagnostic for `initial_setup.yml` would need a replacement, such as a
  static `--list-tags` derived from the tasks tree.
- **18 test modules go red under a loop, 51 tests in all.** This count was measured, not
  grepped. The run replaced `initial_setup.yml` with the looped shape from *Method*, keeping
  the play's preamble, and ran `uv run pytest` against it. The same suite on the static playbook
  failed 2 live-service UI tests and nothing else. The modules that fail only under the loop:
    - `ansible/tests/deploy/`: `test_setup_role_playbooks_agree` (2),
      `test_setup_roles_the_tick_host_skips` (1).
    - `ansible/tests/setup/`: `test_gitops_deploy_reaps_on_non_deployer` (1),
      `test_initial_setup_roles_are_visible_to_the_deployer` (1),
      `test_k3s_host_has_no_docker` (1), `test_nut_host_secondary` (1),
      `test_setup_handlers_resolve` (3).
    - `scripts/deploy_tools/tests/`: `test_land_nut_host_reaches_both_hosts` (2),
      `test_land_reach` (6), `test_land_reach_block_gate` (7),
      `test_land_reach_changed_tasks` (3), `test_land_reach_handlers` (2),
      `test_land_reach_repo_shipped_files` (10), `test_land_reach_role_tests` (5),
      `test_land_reach_vars_files` (3), `test_narrow_setup` (1), `test_narrow_setup_edges` (1).
    - `scripts/docs/tests/`: `test_gen_role_glance` (1).

  Behind those tests sit the production readers `narrow_setup_playbook.playbook_roles`,
  `narrow_setup_index.foreign_tags`, `land_reach` and `scripts/docs/catalog_lib/glance_facts.py`. Under a
  loop, `playbook_roles` returns the empty set, so `narrow_setup.role_tags` refuses every
  narrowing until it reads `setup_roles:` instead. `test_nut_host_secondary` asserts the gate's
  text, so it passes again only if the new encoding keeps that text.
- **`nut_host` does not fit the four placements the issue proposes.** Its gate is
  `inventory_hostname == ups_host or nut_host_secondary_armed | bool`, a `host:` and a `flag:`
  joined by `or`. The list needs a fifth placement, or a per-role gate variable as the
  measurement used.

A generated playbook avoids the first two costs. It still writes a static `roles:` list, so
listing works and the `roles:` readers stay unchanged. In exchange it needs a generator and a drift
check. #3734 therefore chooses between two trade-offs, because tag selection is equal in both
shapes:

- **Loop at runtime.** No generator, but the `roles:` readers change (18 test modules, 51 tests) and listing is lost.
- **Generate the playbook.** Readers and listing are kept, but a generator and its drift check
  are added.

## Where the deployer should read the list

From the tree the tick just fetched, through the subprocess the deployer already uses for
YAML. The deployer runs under `uv run --no-project` and cannot import `yaml` (see
`deploy_narrow.py`'s docstring), so `narrow_setup.py` and `narrow_broad.py` already parse the
fetched tree from `scripts/deploy_tools/`. A `setup_roles:` list in the inventory is read the
same way, and the tick that routes a newly added role sees that role. A copy rendered by the
`gitops_deploy` role would be one apply behind, which is the staleness #4265 names. A committed,
generated JSON file avoids that staleness too, but it adds a generator and a drift check that
the subprocess route does not need.
