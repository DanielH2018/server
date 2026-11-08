# The GitOps pipeline

How the homelab deploys itself, and what to do when it stops.

This is the operator's view first. The rules the deployer follows, and the functions that
hold each one, are in `ansible/roles/setup/gitops_deploy/CLAUDE.md`; the budgets and accepted
trade-offs behind those rules are at the end of this page, under *The deployer's record*.

!!! warning "A merge is not a deploy"
    The tick applies the host plane only: a setup-plane change, as `initial_setup.yml`. `promote`
    and Flux apply every k8s change after master CI, an image-pin bump and a deploy-plane path
    included, and `land.sh` waits on that promotion. A setup role that no tick-run playbook
    applies, such as `k3s`, is recorded for a hand apply, and it sits unapplied behind a green
    master until someone runs it. `k8s_autodeploy` says only whether Renovate may automerge an
    image bump: a role on the denylist, which holds the roles declaring `false`, gets a `manual —`
    PR.

Flux follows the promoted artifact and applies every service's render, so no tick and no Ansible
play deploys a workload.
[The Flux deploy plane](flux-deploy-plane.md) has what it runs and what it reports.

Each k8s role declares its own `k8s_autodeploy` stance
(`ansible/roles/k8s/<role>/defaults/main.yml`), which says whether Renovate may automerge an
image bump. `scripts/lib/k8s_roles.py` collects the ones declaring `false` into the denylist that
`renovate.json`'s no-automerge rule and the Renovate lander read.

<!-- Generated from k8s_autodeploy_counts.py, which reads every role's own declaration; edit
     the role's defaults/main.yml, not this table. -->
--8<-- "assets/generated/fragments/autodeploy-coverage.md"

## What a tick does

A systemd timer runs `gitops-deploy.service` on `daniel-box`
every 10 minutes (`gitops_deploy_tick_interval`). One tick, in order:

1. Fetch `origin`.
2. Read the check runs for the tip of `origin/master` and decide a CI verdict (`ci_verdict()`
   in `deploy_logic.py`).
3. Walk back to the newest green ancestor, when that verdict is pending or red: the tick asks
   GitHub about each earlier commit in the incoming range, newest first, and deploys the first
   one whose own CI is green (`ci_walk_candidates()` in `hosts/lib/ci_verdict.py`, bounded by
   `CI_ANCESTOR_WALK_MAX`). A red commit is skipped, never chosen. Merges arrive faster than the
   CI sweep finishes, so the tip is pending on most ticks that would deploy; gating on it alone
   parked a green commit behind the CI sweep of every later merge.
4. Choose an action from the verdict for the chosen SHA and the changed paths (`next_action()` in
   `deploy_logic.py`).
5. Fast-forward the checkout to the chosen SHA, if the action allows it.
6. Apply the setup roles the range changes.
7. Write a hold and alert, if an apply fails. Nothing rolls back (*Both apply arms are
   forward-only*).

All of it runs while holding `/var/lock/server-git-tree.lock`, which is what stops a tick
rewriting the tree under the secret-rotation cron or under an operator's snapshot. The deploy
steps also take one `/var/lock/server-deploy-<tag>.lock` per service, inside that hold, which
is what stops a tick and an operator's deploy driving the same rollout. See
[ADR-0011](adr/0011-one-lock-serialises-every-deploy-path.md) and
[ADR-0017](adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md).

## Reading the deployer's state

To list only a failed deploy's lines, run `journalctl -u gitops-deploy -p err`. A failure that
leaves a hold logs at `err`. Most failed reads and gates the tick worked around
log at `warning`, so `-p warning` lists both, but not every one does. Every other line stays at the unit's
`SyslogLevel=notice`. `deploy_config.log` sets the priority with an sd-daemon `<3>` or `<4>`
prefix, which journald strips (#4446).

Every deployer `log()` line opens with one lowercase subject token and a colon: the phase or marker the
line is about, such as `ci:`, `hold:`, `narrow:`, `denylist:` or
`manual_plane:`. To list every line about one subject, run `journalctl -u gitops-deploy | grep
'manual_plane: '`. A relayed child-process line gets the subject of the call that ran it.
`ansible/roles/setup/gitops_deploy/tests/test_deployer_log_subjects.py` holds every `log()` call
to that shape (#4507).

`uv run python scripts/diagnostics/probe.py gitops-state` prints these values. Read them before concluding
anything.

| Field | Means |
|---|---|
| `last_run` | When a tick last completed. A stale value means the timer is not firing. |
| `hold_sha` | Non-empty: an apply of a previous SHA failed and is being held. Diagnose that before deploying anything else. |
| `behind_since` | Non-empty: the checkout is behind `origin`, naming the tip it has not reached. Its timestamp is when the deployer last FAST-FORWARDED, not when it first fell behind: any tick that moved the tree renews it, so a tick that landed at a green ancestor reads healthy while a tail that never goes green still pages at 6h. |

## Why a tick does nothing

Four reasons, and they look identical from outside — the unit succeeds and exits 0 in every
one of them.

**CI is pending or red on every commit in range.** `next_action()` returns `ci_pending`
*before* the fast-forward. A tick fired seconds after a merge therefore pulls nothing, because
GitHub has not finished creating the run yet. An empty or incomplete check-run list is
pending, never green. It takes the whole range: one green commit below a pending tip is enough
for the tick to fast-forward to that commit and deploy it.

**A held SHA.** `hold_sha` is set, so the deployer does not move forward until it is cleared.

**The tree is dirty.** The primary checkout holds an uncommitted change or an untracked file.
`git status --porcelain` counts untracked files, so one stray output file from a tool parks the
deployer with nothing modified. The tick logs `tick: working tree dirty — skipping` with the paths on
every run and pages Discord at most twice a day (`should_alert_dirty`). Every other signal stays
green.

A `.gitignore` rule does not clear an untracked file that is already there. The merged rule
reaches the primary checkout only through a fast-forward, and the file it would ignore is what
blocks the fast-forward. To clear it without losing the file or the deploys, move the file
aside, tick, then move it back once the rule has arrived:

```bash
mv <file> /var/tmp/<name>.hold
systemctl start --no-block gitops-deploy.service
cc-wait systemd gitops-deploy.service
mv /var/tmp/<name>.hold <file>
```

Do not run `git pull --ff-only` in the primary checkout instead. The deployer derives its work
from `local..origin`, so a hand fast-forward empties the range and cancels the deploys those
commits were due. A tool that writes into the repo gets its output path into `.gitignore` in
the same PR that adds the tool.

**A broad change the deployer must not apply itself.** This is the one that surprises people,
so it has its own section.

## A change under `hosts/` is the builder's, not the tick's

The tick maps no path under `hosts/` to a service or a plane. It fast-forwards such a change
and applies nothing. The host-package builder on daniel-box applies it instead:
`homelab-builder.timer` builds each changed package from the newest green master commit,
`homelab-repo-sign.service` signs and publishes it, and each host's `homelab-updater.timer`
installs it. [Host packages](host-packages.md) is the runbook. One change needs both paths: a
key rotation commits the new keyring under `hosts/keys/`, and only a `homelab_builder` apply
rewrites the `archive-key.fpr` the builder checks it against. The runbook names that apply.

## Broad changes

A change under a **broad prefix** is one that maps to no single service. The deployer splits
them three ways, by which playbook applies them and whether it may run that playbook at all.

<!-- The table is generated from deploy_changes.py's three prefix tuples; edit those. -->
--8<-- "assets/generated/fragments/broad-prefixes.md"

The bring-up playbooks appear in both the setup and the manual class: the manual check runs
first, so a change to one of them is never applied here.

The setup/deploy split exists because `deploy.yml` is a `containers_list` loop and renders
nothing for the setup plane, so pointing an operator at it for a `roles/setup/` change is a
no-op that leaves the change unapplied.

Flux applies an entry with `owner: flux`. A `deploy.yml` or `toposort.py` change is broad, so the
deployer applies none of it.

A setup role the deployer cannot apply is a fourth case. `k3s` lives in `k3s-bringup.yml` and
`common` in no playbook, so `setup_tags_for` derives nothing for either. Such a range
fast-forwards and records the role instead of parking; see *A role only a hand can apply is
recorded, not parked* below.

A setup role can ship another setup role's file by path, as `gitops_deploy` and
`initial_setup` both import `common/tasks/stamp_deployed.yml`.
`deploy_changes.setup_roles_for` maps a change to such a file to its owner AND every role listed
for it in `SETUP_FILES_SHIPPED_BY_OTHER_ROLES`. `ChangeSet.setup_roles` and `setup_tags_for` both
read the table, so the `manual_plane` ledger class, the narrowed apply, the `hold_plane` coverage
and `land.sh`'s host reach all see the consumers. `narrow_setup.role_tags` diffs the shipped files
beside the consumer role's own directory, so the consumer narrows to the block that installs the
file. The table is static because every caller passes paths alone, and
`ansible/tests/setup/test_setup_cross_role_files.py` fails when it differs from the cross-role
`files/` and `tasks/` references in the setup roles' tasks.

The table also records a `defaults/` file whose variable another setup role reads.
`claude_code` puts each agent in `initial_setup_worktree_holders_group`, so a change to
`initial_setup/defaults/main.yml` re-applies `claude_code` as well (#4303). The row is per file,
so any change to that file names `claude_code`, including a change to a key it never reads.
`narrow_setup.role_tags` maps such a row to the readers of the changed keys the consumer names.
It first widens the changed keys to every name built from one of them: another key of the
owner's `defaults/` or `vars/`, an inventory YAML value, or a fact the owner sets. The reader
may name only the wider key. A key `hosts.ini` names refuses, because
the walk cannot follow it as a mapping. A key the consumer never names adds no tag. A range in
which no path reaches the consumer answers that it needs nothing (#4361). The tick then drops
the role from its run and from the hold, and a range in which no role needs a tag runs no
playbook and records `narrowed-to-nothing` as the setup plane's tags. `land.sh`'s note on the
hosts the tick does not reach asks the same question over the PR's own range, so it names no
hand-run of such a role (`land_reach._reads_no_changed_key`). A change to the key
`claude_code` does read still applies the whole `claude_code` tag. Its reader,
`tasks/agent_access.yml`, runs only inside the agent include, whose tasks all carry that one
tag. The same test file derives these rows from the tree, and the table must equal them
(#4357).
A word match that is not a read, such as a bash local in a template, is exempted in that test
with the reason.

- A `common/tasks/` file is in the table too. An `import_tasks` is static, so its tasks run under
  each importer's tags, and `narrow_setup` resolves the basename through the importing task.
  `setup_roles_for` drops `common` itself for a file with consumers, so the importers apply
  rather than the resolv.conf remediation `common` would print.
- A `common/templates/` file a common task file renders inherits that task file's importers. The
  kuma-check pair routes to the roles importing `check_timer.yml`. `narrow_setup` finds no
  reader of the template in `gitops_deploy` or `k3s`, so those apply or are
  recorded under their whole-role tag.
- `resolv.conf.j2` is the exception (`SETUP_FILES_ROUTED_TO_OWNER`). `k3s` renders it and is a hand
  apply, since it runs from `k3s-bringup.yml`, so the file routes `common` to `manual_plane`, whose
  remediation names the commands.

A k8s role can read a `common` file too. `configarr` and `janitorr` stage `host_lib.py` into
their health CronJobs' ConfigMaps. The deployer applies no k8s role since D6.12, so the role's
moved render reaches the cluster through `promote`, and the deployer keeps no table of them.

A k8s role's image can embed a file from outside every role, as homelab-mcp's embeds
`scripts/diagnostics/probe_lib/obs_api.py`. An edit to it moves the image's content tag, so
`promote` rebuilds the image the same way.

The third class is the bring-up playbooks, which run by hand by construction. The deployer's own
role, `roles/setup/gitops_deploy/`, is not one of them: it applies itself as `initial_setup.yml
--tags gitops_deploy`. The role's handler is `state: started`, which Ansible skips for an
`activating` unit, so a self-apply from inside a tick leaves the running unit alone and the new
code runs from the next tick. The `DECIDED:` marker above `_BROAD_MANUAL_PREFIXES` in
`deploy_changes.py` carries the evidence.

### A deploy-plane change applies nothing on the tick

Since D6.12 the tick runs no `ansible/deploy.yml`. A change under `ansible/templates/`,
`ansible/inventory/` or another deploy-plane prefix fast-forwards the way a docs-only change
does. It writes no ledger line and no receipt entry, because no apply is owed on this host: the
renderer reads the change at the merge commit, `promote` publishes the bundle, and Flux applies
it. `land.sh` waits on that promotion, and land-wait's changed set names the tags the landing
gates (`docs/landing.md`).

**HISTORY — until D6.12 the tick narrowed and applied the deploy plane.** The
[archived page](archive/gitops-deploy-plane-narrowing.md) has that path and the play-file rule it
applied. D6.15 removed the `narrow` subcommand. The two rules below remain in
`scripts/deploy_tools/narrow_lib/`.

- **A shared template maps to the roles that import it.** A name inside a Jinja `{# #}` comment is
  not an import. `claim-default.yaml.j2` maps to the roles whose `defaults/main.yml` declares
  `k8s_claims`, not to every k8s role, and the derivation refuses if
  `k8s_claims` is set anywhere but a role's defaults. An edit that changes only a shared
  template's Jinja comments maps to no tags: the rule compares the template's `jinja2` lexer
  token streams without comments, under both `trim_blocks` settings.
  `scripts/deploy_tools/narrow_lib/templates.py` holds the rules.
- **A filter plugin maps to the roles that call its filters.** `ansible/filter_plugins/` is a
  play-level prefix, so without this rule every change under it refuses. The rule reads the names the
  plugin's `FilterModule.filters()` returns, at both refs, and greps the role trees, the shared
  templates and the play's own trees for each one. A call from `deploy.yml` or `pre_tasks/`
  still refuses, as do an inventory value that calls the filter, a
  `filters()` that is not a literal dict, a deleted plugin and a plugin another plugin imports.
  An edit to comments, docstrings or layout alone reaches no caller in either plane (#4412).
  `filters.comment_only` compares the module's parse tree, less its docstrings, at both refs.
  `scripts/deploy_tools/narrow_lib/filters.py` holds the rule.
- **A setup role that calls a filter is outside both runs**, because `deploy.yml` applies no
  setup role. The deployer reads those callers from
  `deploy_cross_role.SETUP_ROLES_CALLING_FILTER_PLUGINS`, a static table that
  `ansible/tests/setup/test_setup_cross_role_reads.py` holds to the tree. A plugin change joins
  the setup plane beside the deploy plane. The tick applies an `initial_setup.yml` caller such as
  `gitops_deploy`, and records a `k3s-bringup.yml` caller such as `k3s` as a `manual_plane` line.
  `narrow_setup.plugin_tags` narrows each caller to the readers of the vars key whose value calls
  one of the plugin's filters, so a `longhorn_groups.py` change names the host crons' tags
  (`backup-health`, `disk-health`, `etcd-snapshot` and the rest of the readers of
  `longhorn_backup_name`) rather than `--tags k3s`. An inventory value that calls a filter refuses, and so does a role
  that names none of them. A refusal falls back to the whole-role tag.

### A tag Flux applies is merged and skipped

`promote` and Flux apply every k8s role, so the tick routes each changed path under
`ansible/roles/k8s/<role>/` to `cs.flux`, with no read of the role's `owner`. The tick never
applies, defers, alerts on or owes such a role. Without that branch of
`deploy_changes.services_from_changed_paths`, a k8s path would map to no set at all.

- **The journal names the role.** `plan_tick` logs `flux: <role>: fast-forwarding <paths>`.

A `containers_list` entry that sets `owner` is not Ansible-owned: with `owner: flux`, Flux
applies the tag through `promote`. The tick skips such a tag, so the deployer never
applies, defers, alerts on or owes it. D2's plan has the design:
[The ownership flag and its three readers](target-architecture/deploy/d2-first-leaf.md#the-ownership-flag-and-its-three-readers).
`scripts/lib/k8s_roles.py:tag_owners` reads the flag and raises on an `owner` value other than
`flux`.

### Both apply arms are forward-only

A failed apply writes `hold_sha` and `hold_plane`, alerts, and leaves the tree
fast-forwarded. Nothing is rolled back, and the alert says so.

A rollback re-run has to fit inside the unit's `TimeoutStartSec`, or it is killed partway —
worse than never starting one. The arm stays forward-only because proving it fits needs a
fresh `deploy.yml` measurement, not because of any particular timeout value:
`deploy_logic.broad_budget_ok` encodes the check and has no production caller. The numbers
and the date they were taken are under *The deployer's record* below; read `TimeoutStartSec`
out of `gitops-deploy.service.j2` rather than from prose, since it moves when a phase budget
changes.

It deliberately does not reset the tree either. Resetting without redeploying would leave the
tree claiming the old commit while live state is half-new — a tree that lies, over which every
repo-side check reads green.

### A role only a hand can apply is recorded, not parked

Parking the tick for a range carrying `roles/setup/k3s/` or `roles/setup/common/` buys nothing.
The role needs `ansible-playbook ansible/k3s-bringup.yml --tags k3s` whether or not the range is
merged, while every other session's landing behind the park exits 4 from `deploy.sh` until a hand
pulls the primary checkout.

The tick fast-forwards instead, and writes the role to the `owed` ledger
(`/var/lib/gitops-deploy/owed.jsonl`) as one `manual_plane` line per role, carrying its
origin SHA, playbook, first-seen stamp and narrow tags. A role already listed is not re-added,
so its first-seen stamp is the age everything else reads. Five consequences:

- the journal says `manual_plane: pending <roles> — apply by hand: <commands>` on **every**
  tick, not only the one that recorded it. The recording tick says
  `manual_plane: recorded <roles>` instead, naming only the roles it added, so no tick prints
  both lines;
- Discord pages once per SHA, with the same commands and the ledger's path;
- **GitOps Deploy — Status** goes down once the oldest pending line is older than
  `GITOPS_BEHIND_MAX_S` (6 h), naming the roles and the clear command;
- the **SessionStart banner** names one line per pending role — the role, its playbook, how
  long it has waited and the clear command — from the moment the tick records it. Not
  age-gated, unlike the monitor: the monitor pages, where the banner is a passive notice, and
  the session reading it is usually not the session that landed the change;
- the receipt records that role under `manual`, not `applied` — a plane in the same range
  that DID apply still records its own, so a mixed push still proves the half it applied.

Applying the role by hand is half the job. The line stays until something clears it, and a
role left in the marker pages six hours later over work that is already live:

```bash
ansible-playbook ansible/k3s-bringup.yml --tags k3s
uv run python scripts/deploy_tools/gitops_state.py clear-owed manual_plane k3s
```

**After a NARROWED apply, pass `--applied` naming the tags you ran.** A bare clear drops the
role's whole line, and the row can grow between the moment a surface printed the command and
the moment you run it — a second PR touching the same role widens it to
`coredns,node-address`, and the bare form takes `coredns` with yours. That change is then
merged, unapplied, and recorded nowhere:

```bash
ansible-playbook ansible/k3s-bringup.yml --tags node-address
uv run python scripts/deploy_tools/gitops_state.py clear-owed manual_plane k3s --applied node-address
```

The clear then keeps the line and prints what is still pending. Every surface that prints a
narrowed apply prints the matching `--applied` beside it, so following the printed pair is enough.
A clear whose `--applied` covers the whole row takes the line. A clear with `--applied` against an
EMPTY or missing row keeps the line, says so on stderr and exits 1: an empty row means the whole role is pending,
because a later range's derivation refused after your command was printed. Apply the whole role,
then clear without `--applied`. `common`'s row is always empty, so a remediation naming several
roles prints one clear per role, and only the narrowed ones carry `--applied`.

The clear writes one line to the journal, `journalctl -t gitops-state`, naming the role, the user,
the origin SHA of the line it dropped and, for a narrowed clear that kept the line, the tags still
pending. A clear with no apply behind it leaves that trace and nothing else.

The deployer clears a line itself when a tick applies that role's own playbook and tag
(`DeployerState.clear_manual_plane_applied`). No role reaches that today, since the tick runs
neither `k3s-bringup.yml` nor a playbook for `common`.

`optimize_pi` is recorded the same way (#3933). `initial_setup.yml` includes it under `when:
inventory_hostname == optimize_pi_host`, so the tick's `--tags optimize_pi` run on daniel-box
matched no task, exited 0 and recorded an apply the Pi never received.
`scripts/deploy_tools/setup_routing.py` reads the role's `when:` against each host's vars and
finds the one host it admits. The deployer writes that host into the role's `manual_plane` line
as its `host` key, and every surface that prints the remediation runs the command against
it through `gitops_markers.on_hosts`: `uv run ansible-playbook ansible/initial_setup.yml --tags
optimize_pi -e target=daniel-pi`. A host `hosts.ini` pins `ansible_connection=local` gets
`ssh <host> "cd /home/ubuntu/server && git pull --ff-only && <command>"` instead, because
`-e target=` from another machine selects its vars and runs the play where it was typed. Each prints `gitops_markers.PLAYBOOK_CMD` as the prefix, because a bare `ansible-playbook`
is the uv-tool shim without the collection deps (#4413).
A second role gated off the tick's host is derived the same way, with no table to update
(#3734).

A role the tick DOES apply can still owe another host (#6510). `hypervisor` routes onto
daniel-box, where only its teardown half runs, so its install half on daniel-server went
unapplied with nothing durable naming it (#6411). After the ff-merge the tick runs
`scripts/deploy_tools/setup_reach_hosts.py` over the range. That script reads the same
derivation as the retired `land.sh` "also reaches" note (`land_reach.remaining_setup_hosts`). The tick
then writes a `manual_plane` line for each role it names, with the other hosts in the line's
`hosts` key. The script drops a host that already holds the changed file, as `land.sh` does
(`docs/landing.md`), so a host that cannot be read inside 10 seconds stays named.
`clear_manual_plane_applied` never clears a line that carries `hosts`, so the
tick's own apply of the role leaves it standing. Apply the role on each host as printed, then
`clear-owed manual_plane <role>`. A later range reaching another host widens the line's `hosts`
and keeps its stamp. A failed derivation records nothing and logs `setup-reach:`.

A changed repo file with a row in `deploy_cross_role.SETUP_FILES_SHIPPED_BY_OTHER_ROLES` takes
the broad arm. It owes this line only when the tick applies the row's role and that role's copy
of the file reaches another host. Three rows show the three outcomes:

- `scripts/backup/etcd_restore_drill.sh` routes to `hypervisor`. The tick applies `hypervisor`
  on daniel-box, and `setup_reach_hosts.py` prints `{"hypervisor": ["daniel-server"]}` for the
  range, so the line names daniel-server. A comment-only edit to the script owes the same hand
  apply, because `comment_only_broad_changes` reads only `.yml` and `.yaml` paths.
- `hosts/lib/ci_verdict.py` routes to `gitops_deploy`. The tick applies the role, and its only
  host is daniel-box, so the file owes no line.

A shipped file with no row takes no broad arm and writes no line, and `land.sh` alone names its
hosts. `ansible/tests/setup/test_setup_cross_role_files.py` fails on a literal `src:` copy that
has no row.

A moved `secrets.yml` or inventory key owes the same lines (#6968). `route_key_readers` makes
the tick apply each role with a file that reads the key, and `setup_reach_hosts.py` adds those
files' reach through `land_lib/key_readers.py`. A key moved in `group_vars/all.yml` or
`secrets.yml` reaches every host the reader's task gate admits. A key moved in
`host_vars/<h>.yml` reaches `<h>` alone. A reader's whole file counts, not only the tasks the
range changed in it, because the task naming the key need not be one the range edited.

#### The tag the remediation prints is derived, not the role tag

`--tags k3s` is the whole `setup/k3s` role: it reapplies the server install and CoreDNS's
marker, and arms two gated control-plane tasks. A change to the marker's task needs only
`--tags coredns`. The printed command
therefore names the narrowest tags the change reaches.

**One derivation, four surfaces.** `deploy_defer.record` asks
`scripts/deploy_tools/narrow_setup.py` which of the role's own tags the changed paths reach,
and writes the answer as the `tags` key of the role's ledger line, where an empty list is a
range it could not narrow. The journal line, the Discord alert and the SessionStart banner
read that key. Only the tick has the changed paths in reach, because the banner runs from an
isolated worktree and cannot ask git about the primary checkout. Quoting one stored answer is
what keeps the surfaces from disagreeing about the same deferral.

`land.sh` reads the tick's RECEIPT instead. `deploy_defer.record` writes the same derivation into
`/var/lib/gitops-deploy/receipts.jsonl`, one JSON line per origin SHA, beside each plane the
tick applied (`deploy_handlers.handle_broad`). After the tick it awaited, `classify.narrow_plane`
takes the oldest receipt whose origin contains the PR's merge commit, which is the tick that
crossed it, and quotes that receipt's narrowing. The ledger's `tags` key cannot be quoted that
way, because it spans every range that made the role pending. No covering receipt, or a role the
receipt could not narrow, prints the role tag. A contended tick that resets its ff-merge drops its
receipt with the rest of what it recorded.

A landing that awaits no tick has no row for its range yet.
It prints the tags derived for that PR alone (`land_tags.own_narrow_tags`), with the narrowed
`--applied` clear beside them, which drops only those tags from whatever row the next tick
records.

The ledger's readers ignore unknown keys, which is why the tags live on the line itself.
**Every task file in a setup role carries its own tags**, so the derivation maps a changed path to
the tags of the tasks that read it:

- `tasks/<f>.yml` maps to the tags on its own tasks. When the role's `tasks/main` imports the file
  statically, an edit maps to the tags of the top-level tasks the range added or changed, read at
  the new ref, and of the tasks it deleted, read at the old ref (#6234). A deleted task's tag must
  still select a task at the new ref, or the derivation refuses. H1.4 deleted the `coredns`
  instance from `setup/k3s`'s `stale-checkout-guard.yml`, and the whole-file answer had named the
  surviving Longhorn instances' tags instead. A file that sets a fact, runs `include_vars` or
  `register:`s a result keeps the tags of every task in it, because an edited task can read a
  variable that an unchanged task sets under another tag.
- `templates/<f>` or `files/<f>` maps to the tags of every task file naming `<f>`, following a
  template another template includes or imports. A host script's template is usually named in a
  `defaults/` data structure rather than a task's `src:`, so a name found
  only there maps to the tags of whatever reads that KEY.
- `defaults/main.yml` or `vars/<f>.yml` maps to the top-level keys whose value changed between the
  two refs, then the tags of every task file and template naming one of those keys.
- A key, task file, template or file the range deletes together with its last reader maps to
  the tags its readers carried at the old ref (#5930). It refuses if any of those tags selects
  no task at the new ref. H1.4 deleted `k3s_cluster_dns_ip` and `templates/coredns-corefile.j2`
  with the `tasks/coredns.yml` tasks that read them. Before this rule, the deployer refused that
  range and recorded `"tags": []` where `--tags coredns` was enough.

A path that reaches no host is SKIPPED, not refused: a `.md` outside `files/` and `templates/`
(one inside them can be copied or rendered onto a host, so it narrows), and a role-local `tests/`
directory. A `defaults/` or `vars/` edit that moves no parsed key's value, which is every
comment-only edit, is skipped the same way (#4384): Ansible reads the file only through its values.
So is a task file whose parsed tasks are unchanged once each static import's `name:` is dropped
(#5930). An import runs no task of its own, so its name reaches no host. Any other task's name
still counts, because `stale-checkout-guard.yml`'s rows match tasks by name.
Refusing on those would leave the narrowing almost never firing, because most ranges
touching `setup/k3s` carry the role's own `CLAUDE.md`. A range of nothing but such paths is an
answer rather than a narrowing: `narrow_setup.py` exits 3 printing `narrowed-to-nothing`, since
an empty `--tags` value runs the whole playbook (#4361). The tick drops the role from its run.
`deploy_defer.record` writes no `manual_plane` line and sends no page for such a role. It
records `narrowed-to-nothing` as the role's row in the tick's receipt instead, because an
empty row there reads as a refusal. `land.sh`'s plane note leaves the role out, from that
receipt row after the tick or from the PR's own derivation on the fast path (#4408).

**Any other doubt refuses**, and a refusal prints the role tag with
`deploy_remediation.maximal_tag_warning` beside it. Refusing is the safe direction, because a
`--tags` value matching nothing makes Ansible exit 0 having applied nothing. The refusals that
fire in practice:

- a task file with no `tags:` of its own (`main.yml`), whose tasks inherit
  from the import site;
- a key nothing in the role reads, a change under `handlers/` or `meta/`, or a `defaults/` file
  either ref does not carry;
- a derived tag the remediation's playbook cannot reach. The role must sit in that playbook's
  `roles:`, and the task file must be statically imported from `tasks/main.yml`, or included
  from a file that is with an `apply.tags` value the `include_tasks` task itself carries. Such
  an include narrows to the shared tags: `setup/k3s`'s weekly shard file, included in a loop,
  narrowed to `longhorn` until H1.6 deleted it (#4384). An include without `apply.tags`, with disjoint
  tags, or with `never` on the include or on a task inside the file still refuses. `tasks/storage_smoke.yml` belongs to `k3s-storage-smoke.yml`, so
  `k3s-bringup.yml --tags storage_smoke` selects only `always` tasks;
- a template cycle that reaches no task file, or a template that is not UTF-8 text.

A narrowed tag that still reaches the role's gated control-plane tasks keeps a warning. Every task
in `setup/k3s/tasks/server.yml` carries `k3s_server`, including the installer restart and
`secrets-encrypt rotate-keys`, so `--tags k3s_server` prints beside
`deploy_remediation._MAXIMAL_ROLE_GATED_WARNING`.

Two ranges can make one role pending, since the first line keeps its first-seen stamp. The
tags then union: both changes are merged and unapplied, so both tags have to run. A refusal on
either side absorbs the pair, because a narrow tag beside work nothing could narrow
would read like the complete answer. A line that was already pending with empty tags is the
same refusal, and so is a legacy line with no sidecar row. What that earlier range needed is
unknown, so the role stays at the role tag until its line is cleared.

### When a tick parks

Two shapes park: a bring-up playbook, and a setup path naming no role at all. The symptom
is unchanged — **a tick that exits 0, logs a park line, and writes `behind_since`** — and the
journal line says which of the two it was. The deferral is evaluated over the whole
`local..origin` range, so one such change anywhere in that range holds back everything behind
it. Diagnose it by diffing the range:

```bash
git diff --name-only <local-HEAD>..origin/master
```

The Discord alert names the playbook to run. **Fast-forward first, then run it** —
`git merge --ff-only origin/master` in the primary checkout, and only then the playbook.
Running the playbook first renders from the pre-merge tree and applies nothing.
Running it from your own worktree first is worse. The rendered crons `cd` into the primary
checkout and run its Python, so until the fast-forward a new cron line runs against the old
scripts (PR #438).

**If the change is another session's, it is theirs to clear.** Say so and stop, rather than
applying a setup-plane change you did not write.

## Triggering a tick by hand

To run a tick without waiting for the timer, on `daniel-box`:

```bash
systemctl start --no-block gitops-deploy.service
```

It runs the identical code path the timer runs. **There is no dry-run mode** — this deploys.

To apply a green master commit that no promotion followed, dispatch `promote` with the commit's
full 40-hex SHA. `promote` refuses a commit without green CI:

```bash
gh workflow run promote.yml -R DanielH2018/server --ref master -f sha=<sha>
```

## Gating on CI correctly

Do not hand-poll CI. `land.sh` owns the merge, the CI wait, the promotion wait and the verify step, and
`scripts/deploy_tools/await_ci.py` is the CI waiter it uses; the `land-after-merge` skill has the
invocation. Both read the check runs of the merge commit, the same endpoint the deployer reads,
so your verdict and the tick's agree by construction.

A bare `gh run list --branch master --limit 1` is the wrong gate. GitHub creates the run for a
freshly pushed merge commit a moment after the push, so that query returns the *previous* master
run, green, and the wait returns instantly having watched the wrong commit.

## Contention is not failure

A tick that cannot take the lock exits **75**, and the systemd unit **succeeds**. That is a
resume point, not an error: the lock was busy and nothing was deployed. The unit sets this
deliberately (`SuccessExitStatus=75` in `gitops-deploy.service.j2`). Treating contention as
failure paged seven times in seven days, because every long operator deploy held the same lock
for its whole run. Since [ADR-0017](adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md),
`scripts/deploy.sh` holds the tree lock only for a snapshot of `HEAD`, so tree-lock contention is
rare. A tick can still queue behind an operator deploying one of the same services, on that
service's own lock.

Contention is not silent starvation: the lock path never writes `last_run`, so contention
outlasting the maximum age pages through the GitOps-Alive monitor.

## The deployer's record

Everything below this heading is the long form behind
`ansible/roles/setup/gitops_deploy/CLAUDE.md`: the budgets each timeout was sized from, the
design of each safety arm, and the trade-offs that were accepted rather than fixed. The role file
carries the rule; this page carries the reasoning. The full incident accounts are in
`git log -p -- docs/gitops-pipeline.md`.


### A failed apply writes a hold

A failed setup-plane apply writes the SHA to `/var/lib/gitops-deploy/hold_sha`, so the next tick
does not re-apply it, and alerts the dedicated Discord webhook. The tree stays fast-forwarded
(*Both apply arms are forward-only*). Reverting the offending PR advances `origin` past the held
SHA, which stops the `skip_hold` short-circuit. The marker and the red **GitOps Deploy — Status**
monitor (which pages on a non-empty `hold_sha` with no origin comparison) stay until an apply
covers the held plane or an operator clears the hold; *Which apply clears a hold* has the rule.
To clear it once diagnosed, run `uv run python scripts/deploy_tools/gitops_state.py clear-hold
<sha>` on daniel-box.

### The safety arms, in full

- **CI gate — the tip must be green before anything is merged or deployed** (`REQUIRE_CI`,
  `deploy_logic.ci_verdict`). Nothing else in the pull path consults a workflow result. The gate
  queries GitHub's check-runs API for `origin/master`, only on a tick that would otherwise deploy.
  It authenticates through `gh auth token` when the CLI is logged in (`host_lib.github_token`;
  `GH_TOKEN`/`GITHUB_TOKEN` in the environment win) and degrades to anonymous when gh is logged
  out. The anonymous limit is 60 requests an hour per source IP, shared with every landing's
  `await_ci.py` poll on the host, and an exhausted quota reads as "CI not finished." The
  authenticated limit is 5000 an hour per token. An anonymous `await_ci.py` spreads its budget over at
  most 15 polls, at least 90s apart, and never walks descendants, so land.sh's CI budget
  costs at most 30 of the 60 requests (`await_ci.ANONYMOUS_MAX_POLLS`, #6148, #6857). A poll costs 2
  requests until a required run registers. On an anonymous host, a merge commit GitHub never
  ran CI for ends the wait on exit 75 and asks for `gh auth login`.
  - **A tip that is not green sends the gate walking, and the tick deploys the newest GREEN
    ancestor instead of deferring the range.** *What a tick does* has the steps.
    `deploy_phases.assess` reads `git rev-list --first-parent <local>..<origin>` newest first,
    asks GitHub about each commit below the tip, and stops at the first `pass`. That SHA becomes
    `target.origin`, and the ff-merge, the changed-path diff, the declarations read and the
    narrowing all read it. A `fail` ancestor is skipped, and a held SHA is dropped from the
    candidates whether it arrives as the tip or below one. The walk runs ONLY on a tick that would
    otherwise defer, and `CI_ANCESTOR_WALK_MAX` (`ci_ancestor_walk_max` in `group_vars/all.yml`, 10) bounds
    it, counting the tip's own request. **An UNAUTHENTICATED host does not walk at all** and says
    so in the journal, because a ten-request burst per tick would exhaust the anonymous limit and
    every reader's next call would read `HTTP Error 403`, which this gate maps to `pending`. The
    rule is `ci_verdict.ci_walk_candidates`, which carries the `DECIDED:` marker. The property the
    gate keeps is that **the tree the host runs always has its own green CI verdict.** One line
    names the choice: `ci: origin <tip8> is <pending|fail>; fast-forwarding to the newest green
    ancestor <sha8> (<n> behind the tip)`.
  - `fail` on the tip with no green ancestor makes `next_action` return **`ci_failed`**: no
    ff-merge, no deploy, and a Discord alert throttled once per SHA (the `ci` slot of the
    `alerted_shas` marker). A red tip the tick fast-forwarded PAST still pages once for that SHA,
    because `main()` calls `deploy_handlers.alert_red_tip` on the deploying path, keyed on
    `target.tip`.
  - `pending` on the tip with no green ancestor makes it **`ci_pending`**: a silent deferral.
    Unfinished CI is the normal state for the first tick after a push, so only *sustained*
    behind-ness is a problem.
  - **An unreachable or malformed API reads as `pending`, never `pass`.** The tick still
    completes and writes `last_run`, so a GitHub outage does NOT trip GitOps-Alive the way
    `RetryableFetchError` would.
  - Both outcomes leave the host parked on `local`, which `behind_marker` records, so a
    persistently red master pages through the **6 h behind-origin watchdog** below. That reuse is
    deliberate; do not add a second timer.
  - **This gates the DEPLOY.** PR CI is scoped to changed files while master runs the full sweep,
    so a whole-tree failure can appear only after the merge, and this gate is what keeps it off
    the host. The repository rules that guard the merge itself are checked by `github-ruleset-drift.sh`
    (*The `has_gitops` gate, the GitHub crons and the marker module*).
  - `cancelled`/`stale` count as **no verdict, not failure**: `ci.yml` sets
    `concurrency: cancel-in-progress` on `github.ref`, so two quick pushes cancel the first run,
    and mapping that to a failure would page on an ordinary back-to-back push.
    The `always()` gate job that fails on those cancelled needs is no verdict either, once the
    commit's check-suites show its own run concluded `cancelled` (#5931, #6007).
  - `CI_CONTEXTS` holds GitHub check-run **names**, which must match `ci.yml`'s `name:` exactly.
    An empty list or repo **disarms** the gate with a log line rather than passing everything.
    Set `gitops_deploy_require_ci: false` to turn it off.
- **The tick does not consult staging.** `hosts/components/hypervisor` holds the monthly etcd
  restore drill's throwaway guest; `docs/archive/staging-phase-c.md` and
  `docs/archive/staging-cluster.md` record the removed staging arm.
- Read-only against the repo (no push), and no apply arm rolls back.
- Refuses to *deploy* from a dirty working tree (operator mid-edit), but the tick still completes
  and writes `last_run` (`next_action(..., dirty=True) -> "dirty"`), so the skip does not trip the
  GitOps-Alive stale-file threshold. `should_alert_dirty` throttles the page to twice per
  America/Chicago day (the first tick at or after 08:00 CT and at or after 20:00 CT), with the
  slot key `YYYY-MM-DD:am|pm` in `/var/lib/gitops-deploy/dirty_alerted_date`.
- **Test-suite paths are skipped before every plane below** (`deploy_logic._is_test_only_path`):
  `ansible/tests/`, any role-local `tests/` directory, and a `test_*.py`/`conftest.py` beside the
  module it covers. Every prefix and regex matches on path alone, so a test file would otherwise
  read as whatever plane it sat under (PR #707: three test files set `broad_manual`). A test-only
  push produces an empty `ChangeSet` and takes the `handle_merge_only` ff-merge, like a
  docs-only push. The `no-role-ships-a-test-file` row of
  `ansible/tests/repo/test_census_rows_roles.py` enforces that no role ships a test file to a host.
- **A new module in `files/` goes in two lists in `tasks/code.yml`**: the copy task's `loop:` that
  installs it under `/opt/gitops-deploy/`, and `stamp_deployed_pairs`, which records its render
  provenance. pytest imports from `files/` on disk, so a module forgotten in the copy loop passes
  CI and kills the deployer at import on its next tick. ENFORCED by
  `ansible/tests/deploy/test_gitops_deploy_ship_list.py`, the sibling of
  `test_monitor_bridge_modules.py`.
- **Broad changes split three ways** (`deploy_changes._BROAD_*_PREFIXES`); *Broad changes* above
  has the table and the tag routing. **A deploy-plane path adds no plan.** `deploy_narrow.plan`
  names the setup roles this host runs, narrowed to their own block tags, `handle_broad` applies
  them, and `BROAD_DEPLOY_TIMEOUT_S` bounds that apply against the unit's ceiling.
  - **`roles/setup/<name>/` is not `initial_setup.yml --tags <name>`.** The playbook may not
    include the role (`k3s` is in `k3s-bringup.yml`, and `common` is in no playbook and is read by
    two roles on two hosts), and the tag may not be the directory name (`chezmoi_setup` is tagged
    `chezmoi`). Either way `--tags` matches no task, `ansible-playbook` exits 0, and the tick
    would record the change as applied. That happened on 2026-09-01 with a `roles/setup/k3s/`
    change that installed daniel-box's host DNS forwarder. `setup_role_playbook` and
    `setup_role_tag` own the routing, `setup_tags_for` returns nothing for a role
    `initial_setup.yml` cannot apply, and `broad_remediation` names the real playbook.
    `scripts/deploy_tools/setup_routing.py` derives the routing from the playbooks (#3734). The
    deployer runs under `uv run --no-project` and cannot import `yaml`, so each tick runs that
    script as a subprocess over origin's tree. Nothing applies under a guessed tag, and nothing
    records the role in `manual_plane` under a guessed tag or playbook. A failed or timed-out
    run is transient, so a range carrying a setup role parks with no fast-forward and the next
    tick derives again. Any other answer is deterministic and never parks (#4326). A role whose
    directory the range deletes is dropped, as a deleted k8s role is. A line an earlier tick
    wrote before a later range deleted its role is dropped at the start of the next tick by
    `deploy_defer.drop_deleted_manual_plane` (#4334). A line is keyed by the role's tag, so it
    goes only when its key is neither a role directory at `HEAD` nor a tag the routing places.
    A failed routing or an unreadable listing drops nothing. A role the routing came
    back without placing is recorded in `manual_plane` with the playbook `unrouted`, keyed by
    its directory name. `clear-owed manual_plane <role>` matches that key, and the tick clears
    the line itself once a later routing places the role and an apply runs its tag.
  - **The ff-merge happens BEFORE the apply**, since applying first renders from the pre-merge
    tree and deploys nothing. An unrelated commit sharing the tick also lands when the apply
    fails.
  - **`_BROAD_MANUAL_PREFIXES` keeps the defer-and-alert with no ff-merge**: `bootstrap.yml`,
    `k3s-bringup.yml` and `initial_setup.yml`, the bring-up playbooks, which run by hand by
    construction. Staying parked keeps `behind_since` set, the only durable signal those have. A
    setup-plane path that resolves to no ROLE joins them, because there is no hand command to
    print and no role to record.
  - **A setup ROLE whose tag cannot be derived fast-forwards and is recorded in `manual_plane`.**
    *A role only a hand can apply is recorded, not parked* above has the ledger line, the
    journal and Discord output, and the clear commands. `deploy_defer.py`'s module docstring
    carries the `DECIDED:` marker and the measurement. A range carrying BOTH a bring-up playbook
    and an unapplyable role parks and writes no marker, because `deploy_defer.parks_the_tick`
    gives `cs.broad_manual` priority. `land.sh` prints no clear command for such a PR for the same
    reason.
  - **A park names its reason in the journal on every tick**
    (`deploy_remediation.broad_park_reason`). The Discord page is throttled once per SHA, but the
    journal line is not, because the journal is what an operator reads when `land.sh` exits 4.
  - **This role applies itself** (*Broad changes* above). A failed self-apply takes the same hold
    path as any setup role.
  - `BROAD_DEPLOY_TIMEOUT_S` (1800, `gitops_deploy_broad_timeout_s`) bounds one apply. Without it
    a wedged run is SIGTERMed at `TimeoutStartSec` with no hold written and no alert sent.
- **Behind-origin watchdog** (`deploy_logic.behind_marker`): every PARK leaves the host on an old
  tree while `last_run` keeps ticking (Alive green) and `is_diverged` stays false (origin is a
  strict descendant, so Status is green too). Each tick therefore records
  `"<origin_sha> <first_seen_ts>"` to `/var/lib/gitops-deploy/behind_since` when it ends behind
  origin, and clears it on convergence. monitor-bridge's **GitOps Deploy — Status** pages once the
  age exceeds `GITOPS_BEHIND_MAX_MIN` (6 h). The marker is written AFTER `main()` so it reflects
  the state the tick finished in. The watchdog is age-gated because being behind is normal in the
  small: a push is behind for one tick, and the dirty path is behind for a whole edit session.
  - **The stamp measures TIME WITHOUT A FAST-FORWARD, not time behind the tip.** Any tick that
    moved the tree renews it: `entrypoint()` compares HEAD before and after and passes
    `fast_forwarded=` to `record_behind`. A tick that moved nothing keeps the stamp. The ancestor
    walk
    makes the readings differ: a deployer landing every merge at the newest green ancestor is
    behind the tip on nearly every tick, so a per-SHA stamp would age past six hours while the
    deployer works normally. `entrypoint()` re-resolves the REAL `origin/<branch>` after `main()`
    returns, so `behind_since` names the tip and a tail that never goes green still pages at 6 h.
    Readers state the meaning in their own prose: `scripts/lib/deployer_park.py` (shared by
    `deploy.sh` exit 4 and the SessionStart banner) and `monitor-bridge/files/checks/gitops.py`.
  - **A wedge cannot game the re-stamp.** Every path that stops the deployer converging (a park,
    a hold at the tip, a dirty tree, a diverged tree, a tail with no green ancestor or a
    contention reset) leaves HEAD where the tick found it, so `fast_forwarded` is false. A
    fast-forward to any commit above a wedged range crosses that range, because the walk chooses a
    SHA on the first-parent chain and the ff-merge goes TO it.
    `tests/test_gitops_deploy_alert_channels.py::test_a_parked_range_keeps_its_stamp_while_green_commits_land_above_it`
    pins the combination.
  - **The `manual_plane` ledger class is the watchdog's second arm.** A recorded role
    fast-forwards, so `behind_since` clears and every marker the watchdog reads goes quiet while
    the role stays unapplied. `checks.gitops.gitops_status` therefore reads `manual_plane` too and
    pages once the OLDEST pending line is older than the same `GITOPS_BEHIND_MAX_S` (6 h), naming
    the roles and the clear command. It is reported LAST of the four arms: the other three name a
    deployer that has stopped, where a pending role blocks nobody. The pod reads it off the same
    `:ro` state mount as `behind_since` and parses it with `gitops_ledger.manual_plane_entries`,
    its own generated copy, because it cannot import this tree. The SessionStart banner reads the
    class too, through `lib.deployer_park`, and names a pending role from the moment it is
    recorded.
- **Secrets-only pushes** (`ansible/vars/secrets.yml` changed with no service template — a
  rotation pushed from another machine) are fast-forwarded, and the tick applies only the
  readers the two bullets below derive. The new value reaches any other consumer only on its
  next deploy, so the deployer alerts (once per SHA, the `secrets` slot of `alerted_shas`) to
  redeploy the consumers. On a broad tick that page fires
  from every exit that leaves the range merged (#2383), because `alert_once` advances its marker
  on detection and a skipped page is never sent. The contention arm sends nothing, because it
  resets the tree (#2459). `secrets.yml` is deliberately NOT in the broad list: the `/add-secret`
  flow ships it WITH the consuming template, and `promote` applies that template
  (`deploy_logic.ChangeSet.secrets`).
- **A setup role reading a changed secret is applied as a setup-plane change** (#5436), on any
  tick, with or without a deploy-plane path. `deploy_defer.route_key_readers` runs
  `scripts/deploy_tools/secret_setup_readers.py`, which greps `roles/setup/` at origin for each
  key whose ciphertext changed. Each hit goes through `setup_roles_for`, so the role joins
  `cs.setup_roles` as its own edit would. A role shipping another role's `defaults/` or
  `vars/` file joins only when it names a key of that file built from the secret
  (`secret_keys.reads_through`): `initial_setup` ships `claude_code`'s defaults for one
  unrelated key, and without the check every `claude_code` rotation applied all of
  `--tags initial_setup`. `handle_broad` applies the ones this host runs, and
  `narrow_setup.role_tags` gives each the whole-role tag, because which of its tasks read the
  key is not derived. A role another playbook applies, such as `k3s` for `wifi_password`, is
  recorded in `manual_plane`. A key a play, a shared template or an inventory value reads
  refuses, and the range then routes no setup role. A timeout or
  a crash is no answer, so the range parks without a fast-forward and the next tick derives
  again: once merged, no later range carries the key. The page fires on every tick that
  merges the range: it names `secret_rotation.py consumers`, which prints the
  apply command of every consumer that no derivation reached.
- **A setup role templating a changed inventory key is applied the same way** (#6962). PR
  #6952 moved `host_python_version`, and the tick sent the key to the deploy plane alone.
  It hit the fleet-coverage ceiling, so the tick deferred the plane and applied nothing.
  Eight setup roles render the key into their units, and none was applied or recorded in
  `manual_plane`. `route_key_readers` therefore also runs on a range touching
  `ansible/inventory/`, and `secret_setup_readers.py` adds the setup-role files naming a
  top-level inventory key whose parsed value moved
  (`scripts/deploy_tools/narrow_lib/inventory_keys.py`). A file a role ships from outside
  its tree, such as `hosts/lib/host_lib.py`, counts only when it is a `defaults/`, `vars/`,
  `templates/` or `tasks/` YAML file, in both halves (`narrow_setup._renders`, #6970,
  #6992). Ansible copies any other shipped file verbatim, so a key its docstring names reads
  nothing. A secret reaches such a file only through an env file a template renders, and
  that template names the key itself. Three rules differ from the secrets half:
  - A file under a role's own `files/` is copied verbatim, so naming a key there reads
    nothing.
  - An inventory value built from the key is followed to its own readers instead of
    refusing.
  - Nothing refuses. A play or a k8s-only shared template reading the key renders no setup
    role. A shared template carries the key to the setup-role files that include it, and a
    `hosts.ini` inline variable carries it to that variable's readers. A refusal here would
    route nothing and record nothing, which is the silence #6962 reports. So the half fails
    only on an unparsable inventory file or a failed `git`, and the tick then parks and
    retries. A refusal from the secrets half keeps the inventory half's readers, and the
    journal names it.

  `narrow_setup.role_tags` narrows each routed role to the tags of its readers of the key,
  where a secret takes the whole-role tag. That keeps `k3s` on its cron tags rather than on
  `--tags k3s`, which arms the control-plane restart.
- **The tick applies no k8s role.** `promote` and Flux apply every k8s change, an image-pin bump
  and a secret's k8s reader included. `k8s_autodeploy` says only whether Renovate may automerge
  an image bump. `deploy_phases.reconcile_denylist` re-renders the denylist line of `config.env`
  when it disagrees with the declarations at `HEAD`, because the Renovate lander reads that line.
  A changed path under `roles/k8s/**` lands in `cs.flux`, and the tick fast-forwards past it.
  A role `defaults/` edit that moves no content line is not a change at
  all (`deploy_changes.comment_only_k8s_defaults`, #4460): the tick drops the path before it
  classifies. `templates/` and `tasks/` are outside that read.

  **HISTORY — until D6.12 the tick deployed an eligible image-pin bump itself, rolled a failed
  one back and deferred every other k8s change.** `git log -p -- docs/gitops-pipeline.md` has that
  arm's rules and accepted trade-offs.

- **A k8s role's structural dirs (`tasks/`, `defaults/`, `vars/`, `handlers/`, `meta/`)**
  join `cs.flux` with the rest of the role, and the tick fast-forwards past them. `*.md`
  (CLAUDE.md/README) is a silent ff-merge too. The separate `tasks` channel served the Pi's
  Compose roles under `roles/containers/`, and went with them (#6980). A state directory that
  still holds a `tasks <sha>` line in its `alerted` marker keeps it inert: `parse_alerted` reads
  any slot, `record_alerted` writes it back unchanged, and nothing looks it up.
- Acts **only when origin is strictly ahead of local** (`is_ancestor(local, origin)` →
  `next_action(..., origin_ahead=…)`). Un-pushed local commits make origin an *ancestor* of
  local; that's a no-op, not a deploy — otherwise the tick would diff `local..origin` (the
  *reverse* of those commits) and mis-fire an apply. Push to clear it.
- **Divergence watchdog** (`deploy_logic.is_diverged`): if local and origin differ yet *neither*
  is an ancestor of the other (for example a cron committed locally, its push failed, then origin
  advanced), the deployer can't fast-forward and every tick noops while origin's new commits — a
  Renovate/security bump — never deploy. Both other GitOps signals stay green (`last_run` keeps
  ticking, no hold), so each tick writes the diverged SHA to `/var/lib/gitops-deploy/diverged_sha`
  (cleared once resolved) and `monitor-bridge`'s **GitOps Deploy — Status** monitor pages on it.
  A merely unpushed local commit (`local_ahead`) is NOT flagged — that's the plain no-op above.
- Each `has_gitops` host runs its own independent instance of this deployer, with its own git
  clone, state dir and lock. Only daniel-box has one.
  - **By design, the Pi's services are NOT deployed by this tick.** The Pi has
    `has_gitops: false`, and its Compose services ship in its `homelab-daniel-pi` package,
    which the Pi installs itself. `docs/target-architecture/host/h5-pi-compose.md` is the
    plan that moved them there, and `land.sh` waits for the Pi to apply a merge that changes
    its package.

### The `owed` ledger

The `owed` ledger, `/var/lib/gitops-deploy/owed.jsonl`, holds one JSON object per line carrying
`class`, `subject`, `origin` and `at`.

**The ledger exists because the line markers break on a new field.** Each line parser accepts an
exact field count and skips anything else, and the monitor-bridge, `deploy_ui` and
`renovate_agent` roles each redeploy their copy on their own schedule. A field a new deployer
appended therefore read as no pending work in a reader that had not redeployed.
`gitops_ledger.parse_owed` ignores keys it does not know, and every writer carries them through a
rewrite.

**Two classes live in the ledger**: `manual_plane` and `hold_plane`. Their line-marker
predecessors are gone, and `tasks/install.yml` reaps their basenames, along with that of the
retired `k8s_deferred` marker. A line of a class no reader asks for is inert. Every reader goes
through one function per class:

- `gitops_ledger.manual_plane_entries` returns the `manual_plane` lines, which carry `playbook`
  and `tags` beside the common keys. A line missing `playbook` or carrying malformed `tags` still
  pages, for the whole role. monitor-bridge carries a generated copy of the module.
- `gitops_ledger.held_planes` returns each `hold_plane` subject once, oldest first. Its readers are
  monitor-bridge's Status check, the `deploy_ui` panel, `renovate_agent`'s skip reason,
  `runbook_gates_lib/k3s_upgrade.held_sha` and the scheduled-jobs page. Each reaches it through
  `gitops_hold.DeployerSnapshot`, which reads `hold_sha` and the ledger together and raises on a
  marker it cannot read, and each keeps its own failure rule. `land_lib.tools.read_state` answers
  None, so `land.sh` fails closed. `deployer_park` answers None, which the SessionStart banner
  reads as no park. The deploy-ui panel lets the error through to its `Unavailable` reply.
  `gitops_state.py` reads only through `DeployerState` under the tree lock, because each of its
  clears rewrites the marker it read. `probe.py gitops-state` (`probe_lib/gitops_view.py`) reads
  each marker raw and reports it as set, absent or unreadable.
- A `hold_plane` line's subject is the whole entry, `<playbook> <tags>`, so two failed applies of
  one playbook stay two entries. `DeployerState.hold_failed_apply`, `clear_broad_hold` and
  `clear_service_hold` record and drop the class. The `deploy_ui` Clear drops the class's lines
  under the git-tree lock, so a cleared hold cannot replay them into the next one.

### The `has_gitops` gate, the GitHub crons and the marker module: history

`tasks/main.yml` dispatches on `has_gitops`: `install.yml` on the deployer, `teardown.yml`
everywhere else. `group_vars/all.yml` defaults the var **false**, the same safe direction
`has_docker` takes: a host nobody has thought about gets the teardown arm rather than a timer
that deploys to production on its own, and daniel-box's `host_vars` carries the one
`has_gitops: true`. The three non-deployers keep an explicit `has_gitops: false` because the code
gate below reads host_vars TEXT and fails open on an absent key.

A host flipped to false needs the teardown arm, because a skipped role keeps whatever an earlier
true run installed. `teardown.yml` removes the six units,
the polkit rule, the two GitHub crons and their scripts, and the three directories `install.yml`
creates (`/opt`, `/var/lib` and `/etc/gitops-deploy`). The `DECIDED:` at that task says why the
state directory goes too. `ansible/tests/setup/test_gitops_deploy_reaps_on_non_deployer.py`
derives both censuses from `install.yml` and fails when the teardown stops covering one.

The code carries the same gate. `deploy_phases.refuse_unless_deployer` reads this host's
`host_vars/<hostname>.yml` at the top of `main()`, ahead of the alert drain and every state
write, and raises `NotTheDeployerHost` on a top-level `has_gitops: false`. `entrypoint()` turns
that into one journal line and exit 0, with no Discord post and no `last_run`. It fails open on
every other shape (no file, the key absent, `true`, an indented or commented-out occurrence),
because a false refusal on daniel-box parks every landing in the fleet.
`tests/test_gitops_deploy_not_the_deployer.py` pins both halves.

The teardown reaches a non-deployer host only by hand. `roles/setup/` is a broad setup path, so
the tick applies it on daniel-box, and daniel-server and daniel-pi need
`uv run ansible-playbook ansible/initial_setup.yml --tags gitops_deploy` (with `-e
target=daniel-pi` for the Pi) run by an operator.

**The two GitHub checks** are daily timers of the `github-checks` component on the deploy host, which keep the contract of
`check_timer.yml` in the `common` role. Each writes its verdict through `write_check_textfile`, which the rules
in `gitops-checks.yaml` read, and pushes to nothing. Each exits 1 on a down verdict, and
`Restart=on-failure` reruns it every 30 minutes until it exits 0. Both
authenticate with the deploy user's `gh auth token`, and without it both are red, because GitHub
shows a ruleset's bypass list only to an admin. The timers are `Persistent=true`, so a slot missed
inside an outage runs at boot. `teardown.yml` imports their Ansible-era names absent, which reaps
the old units on a non-deployer host. One alerts and the other reconciles:

- **`github-ruleset-drift.sh`** reads four ruleset definitions from GitHub and alerts on drift in
  any of them. It never writes: a ruleset changes because a human changed it, and reconciling
  would undo that with no signal.
    - The repo itself must be public. On the free plan GitHub enforces no ruleset on a private
      repo, and each ruleset still reads `enforcement: active`. The repo was private from about
      13:07 to 22:47 UTC on 2026-09-21, and 29 PRs merged before their required check reported
      success. A token read names `.visibility`; an anonymous read of a private repo answers
      `Not Found`. `land.sh --arm-merge` refuses to arm or direct-merge while the repo is not
      public.
    - The master CI gate must require exactly `gitops_deploy_expected_ruleset_contexts`. It must
      also carry a `pull_request` rule requiring `gitops_deploy_ruleset_required_approvals`
      approving reviews, which is zero so that GitHub's auto-merge can merge a PR.
    - The branch-protection ruleset (`gitops_deploy_branch_ruleset_id`) must exclude
      `refs/heads/renovate/**`. Without that exclusion GitHub refuses Renovate's post-merge
      branch delete and its rebase force-push, and a merged branch's stale green automerges the
      next PR empty.
    - The fence ruleset (`gitops_deploy_fence_ruleset_id`) must restrict creating, updating and
      deleting every branch except exactly `gitops_deploy_fence_ruleset_exclude`. Without it, the
      agent's account could push onto a branch an operator session opened, and `land.sh`'s admin
      merge would carry that commit to master.
    - Each bypass list must equal its declaration: none on the CI gate, the admin role and the
      Renovate app on the fence. The agent's GitHub account is in no list. The fence excludes
      master, so the agent's account can merge or auto-merge its own PR.
- **`github-interaction-limit.sh`** re-applies `gitops_deploy_interaction_limit` every day.
  GitHub grants an interaction limit for six months at most and lets it lapse silently, so
  re-applying it restores the declared state rather than overriding a decision. The limit keeps
  the public repo closed to anyone but a collaborator. To change or clear it, edit the default;
  `none` clears it with a DELETE on the next run. A missing token, a failed PUT and a stored value
  that differs from the declared one all record DOWN with `UNVERIFIED` or `NOT applied`, never
  `armed`. `ansible/tests/setup/test_github_interaction_limit.py` drives each branch.

**One marker module, shipped from one source.** `files/gitops_markers.py` holds the state
directory, the `MARKERS` table, the parsers for the `behind_since` and `contention_since` line
formats, and the two clear commands every surface prints. `deploy_state` imports it. Checkout code
(`scripts/lib/deployer_park.py`, `gitops_state.py`) imports it through a `sys.path` insert.
`deploy-ui` and `renovate-agent` install it into their `/opt` directories with a `src:` naming
this role's `files/`, and `deploy_changes.SETUP_FILES_SHIPPED_BY_OTHER_ROLES` routes a change to
it to both roles. Both install `gitops_ledger.py` and `gitops_hold.py` the same way. monitor-bridge
ships its own `files/` into a pod, so `scripts/dev/gen_gitops_markers.py` writes a verbatim copy
of all three there under a `generated_from:` header.
`ansible/tests/deploy/test_gitops_markers_copies.py` fails when that copy differs from what the
generator writes, when a consumer's ship list lacks the module, or when the source grows an
import (it runs in a pod, a hook and three `/opt` directories, so only the stdlib is common
ground). To change a basename or a line format, edit the source, run the generator, and commit
the copy in the same PR. The shell and manifest literals that cannot import anything
(`deploy-ui.service.j2`, monitor-bridge's hostPath) are pinned to `STATE_DIR`
by the same test.

### Which apply clears a hold

**`hold_sha` clears only when the plane the hold names is applied** (`Hold.cover` /
`Hold.cover_services` in `gitops_hold.py`, which `DeployerState.clear_broad_hold` and
`DeployerState.clear_service_hold` call, deciding through `gitops_hold.broad_hold_cleared_by`).
`gitops_hold.Hold` is the rule's one owner: the deploy UI's Clear calls `Hold.clear` rather than
restating which files a clear removes. The rule is coverage, not equality. An untagged run
applies the whole playbook and covers any tag set held against it. A tagged run covers a held
tag set it is a superset of, and covers an untagged hold not at all.

A narrowed setup apply holds each block tag it ran as `<role>:<block>` (`gitops_hold.held_tag`),
for example `ansible/initial_setup.yml gitops_deploy:gitops-config`. That block's tag or the
role's own tag covers it, so the whole-role fallback clears it, while an apply narrowed to a
different block of the same role does not. The Discord alert quotes the tags the apply ran, not
the held form, because `--tags gitops_deploy:gitops-config` is no command Ansible accepts.

**The `hold_plane` class holds one ledger line per failed apply**
(`DeployerState.hold_failed_apply`). A second failure adds its line beside the first, and a
repeat of a held entry keeps its first line. An apply drops only the entries it covers, and
`hold_sha` clears once none is left. A torn line still counts as held, so a plane the readers
cannot parse never lets `hold_sha` clear over it.

**Clearing unconditionally erases the fault.** Every consumer gates on `hold_sha` alone:
`checks.gitops.gitops_status` reads `hold_plane` only to choose a sentence, `land.sh` reports
`deploy-failed` off `hold_sha`, and `renovate_agent.decide` refuses to run while one is set. A
hold that cleared on any successful apply turned **GitOps Deploy — Status** green over a plane
nothing had applied (issue #878).

**The way out is manual, and both surfaces name it.** A hand `ansible-playbook` run is not the
deployer, so it clears nothing. Once every held plane is applied, clear the hold with the deploy
UI's Clear or `gitops_state.py clear-hold <sha>`. The Discord alert and the monitor's message
both name the two, with the full held SHA in the command, and the monitor counts the planes still
owed. An `rm` of `hold_sha` is not the way out: it leaves the class's lines in `owed.jsonl`, and
the next failure's hold then waits on planes nobody owes.

**The Clear button in the deploy UI drops every entry at once.** `deploy_ui_writes.clear_hold`
removes `hold_sha` and the `owed` ledger's `hold_plane` lines together as soon as the typed SHA
matches, whatever is still unapplied. It is the operator's override, not a per-entry clear, so
the page lists the entries one per line and the confirm prompt names them. After the Clear
nothing records those planes. From a shell on daniel-box, the page's own request is the same
Clear:
`curl -X POST -H 'X-Deploy-UI: 1' -d '{"expected_sha": "<full hold_sha>"}' http://10.0.0.215:8790/api/hold/clear`.

**`gitops_state.py clear-hold <full hold_sha>` is the same Clear without deploy-ui.** It calls
`Hold.clear` under the git-tree lock and prints every plane it dropped. A different live hold, or
none, refuses with exit 1. It journals `event=clear-hold` under `-t gitops-state`. It is its own
verb rather than a `clear-owed hold_plane` class, because a `hold_plane` line dropped without
`hold_sha`, or `hold_sha` without its lines, is the orphaning above. `clear-hold --orphaned`
removes `hold_plane` lines an earlier hand `rm` left with no `hold_sha`, and refuses while a hold
is set. `probe.py gitops-state` prints the command with the held SHA filled in.

**The cost: a surviving hold parks the Renovate agent** (`agent_logic.decide` returns `run=False`
for any non-empty `hold_sha`). That is intended, because an unapplied plane is not the moment to
land more bumps, and it makes the manual clear load-bearing. It does not park deploys: `skip_hold`
matches only while `origin_head == hold_sha`, so a stale hold stops nothing once origin advances.

### A failed run's error string

`run()` raises a `RuntimeError` carrying the argv, the exit code, then a bounded tail of
**stdout followed by stderr**. Stdout is the half that matters: `ansible-playbook` writes the
failing `TASK` header, the `fatal:` line with its `msg` and the `PLAY RECAP` there, and reserves
stderr for warnings and deprecation notices. A stderr-only error string once left a deprecation
warning as the only detail of a failed broad apply, and that arm is forward-only, so the alert gave
an operator nothing to fix forward from.

Both halves are capped at `RUN_ERROR_STDOUT_CHARS` / `RUN_ERROR_STDERR_TAIL` (4000 each). The
three Discord failure posts trim further through `_alert_excerpt` (`ALERT_EXCERPT_CHARS`, 700).
That second cap is required: `host_lib.discord_post` cuts a post to `DISCORD_MAX` (1900) keeping
the **head**, so an unbounded error string evicts the remediation prose that follows it.
`broad_failure_alert()` is a function so `tests/test_gitops_deploy_failure_output.py` can assert
that the assembled post stays under 1900 characters with its `**Action:**` line intact.

**The stdout half is not a positional tail.** `_failure_detail` finds the last task section with
an un-ignored `fatal:`/`failed:` line, puts that task's header and failure lines first, drops
`profile_tasks`' `TASKS RECAP` timing table, and spends the rest of the budget on the tail (the
`PLAY RECAP`). `_alert_excerpt` runs the same finder, so the Discord post carries the task rather
than the end of stderr. A plain tail loses the failure when the recap and twenty timing rows fill
the whole window (a broad apply that failed one task in 1950). stderr, and stdout with no
`fatal:` line, still get `_tail`, because ansible prints the diagnostic part last there.

A run killed by `BROAD_DEPLOY_TIMEOUT_S` carries the same detail. `run()` re-raises the
`TimeoutExpired` as `deploy_failtext.py:TimedOutWithOutput`, whose `str()` appends what the
killed process had printed. That output comes from the exception the stdlib already raises. The
pipes are NOT re-read after the kill, because that would block on a descendant that escaped the
process group. `last_task` puts the running task first, since a killed run has no `fatal:` line
(`tests/test_gitops_deploy_failure_output.py`).

### Trap: deploying this role under the shared tree lock self-deadlocks

Do not wrap `initial_setup.yml --tags gitops_deploy` in `flock /var/lock/server-git-tree.lock`.
`Run gitops-deploy once` is a handler (`handlers/main.yml`), notified by tasks in `tasks/code.yml`
and `tasks/service.yml`. It invokes the deployer, whose systemd ExecStart is
`flock -w 180 -E 75 /var/lock/server-git-tree.lock …`. Holding the lock from outside makes the
smoke run wait its full 180 s and deploy nothing.

**The failure is silent.** `-E 75` plus `SuccessExitStatus=75` in `gitops-deploy.service.j2` make
systemd report the unit `Result=success`, so the handler's `ansible.builtin.systemd: state:
started` returns rc 0 and the play recaps green. Nothing deploys, `last_run` does not move, the
deployer never starts so no Discord message goes out, and no `OnFailure` fires. The first alert
is GitOps-Alive, once `last_run` ages past `GITOPS_MAX_AGE_S` (grep that name in
`ansible/roles/k8s/monitor-bridge/files/check.py`). The one immediate signal is the unit's
`ExecStopPost` journal marker, `tick skipped (lock contention)`.

Before `SuccessExitStatus=75`, the same contention failed the play: `Run gitops-deploy once` timed
out at 180.30 s, the flock wait (2026-08-20).

Run the apply unlocked. The deployer takes the lock itself, which serialises it against the
timer. The outer flock converts that serialisation into a silent no-op. The same trap applies to
any role whose tasks re-enter a lock-taking command.

The smoke run is a handler, so it fires only when something changed. A no-op run (`changed=0`)
never invokes the deployer and cannot deadlock however it is wrapped. A missing smoke-run step
therefore shows that nothing needed re-rendering, not that the run was incomplete.

**The ff-merge comes before the playbook.** Ansible renders from the working tree, so a playbook
run on the pre-merge tree copies the OLD files and recaps `changed=0`, which looks like a clean
idempotent run. `broad_remediation()` emits the pair in that order, so the deployer's Discord
alert, `deploy_cli.py tags` and `land_tags.py` all prescribe the working sequence.
`test_broad_remediation_puts_the_ff_merge_before_the_playbook` pins the order.

### Trap: moving a config source changes which remediation the alert prescribes

`/etc/gitops-deploy/config.env` is rendered only by `initial_setup.yml --tags gitops_deploy`.
`deploy.yml` runs no setup role, so a change that must reach that file is unapplied by a
`deploy.yml` run, while a plain ff-merge clears the divergence and every repo-side check reads
green. `broad_remediation()`'s docstring says that naming `deploy.yml` there is a silent no-op
that leaves the change unapplied.

Before moving any value that lands in a host config file, check which plane `role_of` puts its
new path on and what `broad_remediation()` says for that plane.

### The rollback timeout, derived

**HISTORY — D6.12 deleted the tick's k8s rollback, and the budget it was sized for.** The
derivation is in `git log -p -- docs/gitops-pipeline.md`, and what stays below is the broad arm's.

**The broad arm is the one worst case inside a unit activation.** It is forward-only: 180 s of
flock wait plus `BROAD_DEPLOY_TIMEOUT_S` (1800 s), or 1980 s, against `TimeoutStartSec=40min`
(2400 s) in `gitops-deploy.service.j2`. The 420 s between them is for the tick's work outside
the budget: its git calls carry no timeout. No lander deadline reads this ceiling, because
`test_lander_deadlines_cover_the_soak.py` gives the tick no fixed budget.

**This unit can hold the tree lock for up to 2400 s**, because only `TimeoutStartSec` ends a tick
whose git calls hang. A concurrent `./scripts/deploy.sh` waits `LOCK_WAIT=2400`, not the unit's
own `-w 180`, so it outlasts any single tick and then deploys. It returns exit 75 only if the lock
stays busy past 2400 s, which also happens behind a docs-refresh run whose four `timeout` calls
all fire (2700 s) or when a second holder takes the lock first, because flock does not queue
fairly. An operator who sees exit 75 should check whether `gitops-deploy` is mid-apply before
assuming the lock is stuck.

**Three waiters on the tree lock are pinned as a census.** `deploy.sh`, docs-refresh and eval-run
each wait 2400 s, and `_LOCK_WAITERS` and `_longest_tick_hold()` in
`tests/test_gitops_deploy_timeout_budgets.py` hold each wait at or above the unit's ceiling. The
census models the deployer alone. docs-refresh's longest path stays outside it because a run
measures 7-11 minutes and `deploy.sh` can retry after exit 75. A
per-consumer test covers only the consumers someone wrote one for, so the census plus
`test_the_lock_waiter_census_is_non_vacuous` makes a new waiter fail instead of passing silently.
A raise that pushes `TimeoutStartSec` past a waiter's wait fails those tests rather than shortening
an operator's wait.

**The overrun does not produce a second run, and it does not alert.** `OnUnitActiveSec` is
measured from when the timer last started the unit, not from when it finished. A `Type=oneshot`
unit that is still `activating` absorbs the next elapse: systemd leaves it running and spawns no
second instance. `ExecStart` sits behind `flock -w 180` as a backstop. `-E 75` plus
`SuccessExitStatus=75` report lock contention as `Result=success`, so no `OnFailure` fires.

**The contention is one-directional since ADR-0017.** `./scripts/deploy.sh` holds the tree lock
only to copy `HEAD` into a detached worktree, and runs its playbook from that snapshot under one
`/var/lock/server-deploy-<tag>.lock` per service. This unit still holds the tree lock across its
whole run and takes the per-service locks inside that hold, which keeps a tick and an operator
deploy off the same rollout. The lock order is `all` first, then each service in sorted order.
`deploy.sh` runs `deploy_locks.py plan <tag>...`, takes the locks it prints top to bottom, and
exits 79 rather than order them itself when the plan does not arrive. The `# DECIDED:` marker
in `files/deployer_lib/deploy_locks.py` says why the two orders cannot deadlock.

**A busy service lock is contention, not a failed apply.** `deploy_locks` raises
`ServiceLockBusy`, `handle_broad` catches it ahead of its failure arm, and
`deploy_defer.for_contention` logs `service lock <tag> busy for <N>s — deferring to the next
tick`, resets to `local` and returns 0. The tick writes no `hold_sha` and no `hold_plane` and
sends no page, because nothing was applied. The reset undoes the ff-merge, so `local..origin`
carries the range for the next tick. `tests/test_gitops_deploy_lock_contention.py` covers it.

**Every marker the tick wrote about that merge goes back with it**, through
`deploy_defer.unrecord`: the `manual_plane` ledger line this tick appended, the `tags` it widened
on an existing line, and this origin's receipt. `land.sh` reads the receipt to tell a plane the
tick applied from one it merely fast-forwarded past. An earlier tick's receipt is still true, so
it stays. No `secrets` alert slot needs taking back, since the secrets page is sent from the
non-contention exits only.

**A contention streak is recorded in `contention_since`.** `for_contention` writes
`"<origin_sha> <lock> <first_seen> <last_seen> <count>"`, keeping `first_seen` across the streak.
`entrypoint()` clears the marker after any tick whose `last_seen` it did not write. A crash
keeps the streak, because a crash is not evidence that the lock was released. monitor-bridge pages
once `first_seen` is older than `GITOPS_CONTENTION_MAX_MIN` (30), and the SessionStart banner
names the lock past the same threshold (`lib.deployer_park`).
`scripts/deploy_tools/gitops_state.py clear-contention` drops the marker by hand. All three
readers parse it through `gitops_markers.parse_contention`.

**A broad apply takes `all` exclusively, whatever its tags.** `deploy_io.deploy_broad` passes
`exclusive_all=True`, because a setup-plane apply reconfigures the host every workload runs on.

**Waiting for a service lock spends the phase's own budget.** A separate wait budget would add
itself to `_worst_lock_hold()`, and the four jobs that wait on the tree lock would start giving
up and paging for ordinary contention.
`deploy_locks.locked_budget` shares one deadline between the wait and the playbook. The cost is
that a phase queued for most of its budget can have its playbook SIGTERMed early, which reads as
a failed apply and writes a hold.

## The deployer's Python layout

Moved out of the role's `CLAUDE.md` by #2999. Three layers, and which one a function belongs in
is decided by what it touches.

On disk, `files/` holds the entry point `gitops_deploy.py` and the three `gitops_*` modules, and
every `deploy_*` module sits in the `files/deployer_lib/` namespace package (#4355). The
`gitops_*` modules stay flat because `ansible/tests/deploy/test_gitops_markers_copies.py` keeps
them identical to monitor-bridge's copies, and the deploy UI installs them flat too. A
`deploy_*` module is imported as `from deployer_lib import deploy_io`. The entry point puts its
own directory on `sys.path`, which is what resolves `deployer_lib` on the host. The package is
not named `deploy_lib`, because `scripts/deploy_tools/deploy_lib/` sits on the same pytest path
and two same-named PEP 420 portions merge into one package.

| layer | modules | holds |
|---|---|---|
| decisions (pure) | `deploy_changes`, `deploy_cross_role`, `deploy_git`, `deploy_health`, `deploy_inventory`, `deploy_remediation` | every branch the tick takes, as functions over plain values |
| what a phase hands the next | `deploy_tick_types` | `TickTarget`, `TickPlan` and `RetryableFetchError`, no behaviour |
| transport | `deploy_io`, `deploy_alerts` | subprocess, when an alert is sent, and the alert queue's own I/O |
| the message bodies | `deploy_alert_text` | one pure function per alert — what each post SAYS (#2600) |
| transport leaves | `gitops_markers`, `gitops_hold`, `deploy_config`, `deploy_state`, `deploy_failtext` | the marker table, its parsers and line rewrites, the hold's owner and its clear rule, the config file, the state directory with every marker's reader and writer, and the text a failed run's alert quotes |
| the seam | `deploy_toolbox` | `DeployTools`, one frozen object holding every boundary the tick crosses |
| the phases | `deploy_phases`, `deploy_handlers`, `deploy_defer` | `assess` and `plan_tick`; one `handle_*` per terminal branch |
| the tick | `gitops_deploy` | the config constants, `tick_config()`, `main()` and `entrypoint()` |

- **`main()` sequences, it does not decide.** `assess()` returns a frozen `TickTarget`,
  `plan_tick()` a frozen `TickPlan`, and one `handle_*` owns each terminal branch. No leaf imports
  `gitops_deploy` (ENFORCED by `test_no_leaf_imports_the_entry_module`).
- **Every process boundary is injected, not patched.** `main(tools, config, state)` threads
  one frozen `DeployTools` through every phase; a test builds one from `tests/_deploy_fakes.py`.
  `deploy_io.deploy_broad` stays outside it because the suite asserts on the argv it builds, so
  it takes a required `run=tools.run` instead. `tests/conftest.py` patches
  nothing (#3744).
- **`deploy_io`, `deploy_alerts` and `deploy_alert_text` are reached QUALIFIED.** ENFORCED in
  `ansible/tests/deploy/test_gitops_deploy_imports.py`, which also holds every module's sibling
  imports to an explicit `ALLOWED` map and keeps `deploy_logic.py` defining nothing, so a
  `deploy_logic.<name>` citation stays true.
- **Configuration is parsed once, and parsing cannot fail.** `deploy_config.load_config` collects
  a malformed value into `Config.errors`; `CONFIG.validate()` at the top of `main()` turns it into
  one line naming the key plus a Discord post.
- **State is one object.** `deploy_state.DeployerState` wraps the marker files and the hold
  writes; a caller names a marker (`state.path("hold")`), never a path. `read()` returns None for
  a missing AND an empty marker, and PROPAGATES any other `OSError`, because an unreadable state
  directory must not read as no hold. `tests/test_deployer_state.py` pins all three outcomes.
- **Marker files have one source.** *One marker module, shipped from one source* above owns it.
- **The playbooks run as `uv run --frozen ansible-playbook`**, the repo-pinned env, so `uv` has
  to be on the unit's PATH.

Tests: one `tests/test_deploy_<module>.py` per decision module, and the
`tests/test_gitops_deploy_*.py` family for the entry module, of which `_main_branches` drives
whole ticks against the scripted `tick` fixture. Run
`uv run pytest ansible/roles/setup/gitops_deploy/tests`.

## The timeout budget, and the waiters it moves

`BROAD_DEPLOY_TIMEOUT_S` (`gitops_deploy_broad_timeout_s`, 1800) is the one phase budget since
D6.12 deleted the k8s path. It bounds the whole apply loop, and the arm is forward-only, so no
rollback budget stacks on top.

- The unit's worst case is the flock wait plus that budget, which
  `test_the_deployers_hold_survives_max_flock_contention` holds inside `TimeoutStartSec` in
  `gitops-deploy.service.j2`.
- A raise of `TimeoutStartSec` past the three waiters' 2400 s moves them too, in the same change,
  because `test_every_git_tree_lock_waiter_clears_the_deployers_worst_case_hold` fails otherwise.
- **The lock waiters and the service-lock budget** are under *The rollback timeout, derived*:
  `deploy.sh`, docs-refresh and eval-run each wait 2400 s, and
  `deploy_locks.locked_budget` spends the phase's own budget on a service-lock wait. An overrun
  never produces a second concurrent run, because the timer coalesces the new start into the
  activation in flight.
