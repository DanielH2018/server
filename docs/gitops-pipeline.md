# The GitOps pipeline

How the homelab deploys itself, and what to do when it stops.

This is the operator's view first. The rules the deployer follows, and the functions that
hold each one, are in `ansible/roles/setup/gitops_deploy/CLAUDE.md`; the incidents and
measurements behind those rules are at the end of this page, under *The deployer's record*.

!!! warning "A merge is not a deploy"
    The deployer applies three kinds of change on its own: an image-pin bump to a k8s service that
    is not denylisted, a setup-plane change, and a deploy-plane change (*Broad changes* below).
    Every other k8s change, such as a manifest, a template or a task file, is fast-forwarded onto
    the primary checkout and left unapplied. A setup role that no tick-run playbook applies, such
    as `k3s`, is recorded for a hand apply. Left alone, an unapplied change sits undeployed behind
    a green master until someone notices.

Each k8s role declares its own `k8s_autodeploy` stance
(`ansible/roles/k8s/<role>/defaults/main.yml`); `ansible/filter_plugins/k8s_autodeploy.py`
collects the ones declaring `false` into the denylist above.

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
   one whose own CI is green (`ci_walk_candidates()` in `deploy_logic.py`, bounded by
   `CI_ANCESTOR_WALK_MAX`). A red commit is skipped, never chosen. Master takes about 124
   merges a day against a ~103s CI sweep, so the tip is pending on most ticks that would
   deploy; gating on it parked a green commit behind the CI sweep of every later merge.
4. Choose an action from the verdict for the chosen SHA and the changed paths (`next_action()` in
   `deploy_logic.py`).
5. Fast-forward the checkout to the chosen SHA, if the action allows it.
6. Deploy whatever is eligible.
7. Health-gate the result, and roll back on failure.

All of it runs while holding `/var/lock/server-git-tree.lock`, which is what stops a tick
rewriting the tree under the secret-rotation cron or under an operator's snapshot. The deploy
steps also take one `/var/lock/server-deploy-<tag>.lock` per service, inside that hold, which
is what stops a tick and an operator's deploy driving the same rollout. See
[ADR-0011](adr/0011-one-lock-serialises-every-deploy-path.md) and
[ADR-0017](adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md).

## Reading the deployer's state

`./scripts/deploy_tools/gitops_tick.sh` prints three values after it runs. Read them before concluding
anything.

| Field | Means |
|---|---|
| `last_run` | When a tick last completed. A stale value means the timer is not firing. |
| `hold_sha` | Non-empty: a previous SHA failed its health gate and is being held. Diagnose that before deploying anything else. |
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
deployer with nothing modified. The tick logs `working tree dirty — skipping` with the paths on
every run and pages Discord at most twice a day (`should_alert_dirty`). Every other signal stays
green. On 2026-08-30 one untracked file held the checkout 7 commits behind for about 40 minutes.

A `.gitignore` rule does not clear an untracked file that is already there. The merged rule
reaches the primary checkout only through a fast-forward, and the file it would ignore is what
blocks the fast-forward. To clear it without losing the file or the deploys, move the file
aside, tick, then move it back once the rule has arrived:

```bash
mv <file> /var/tmp/<name>.hold
./scripts/deploy_tools/gitops_tick.sh
mv /var/tmp/<name>.hold <file>
```

Do not run `git pull --ff-only` in the primary checkout instead. The deployer derives its work
from `local..origin`, so a hand fast-forward empties the range and cancels the deploys those
commits were due. A tool that writes into the repo gets its output path into `.gitignore` in
the same PR that adds the tool.

**A broad change the deployer must not apply itself.** This is the one that surprises people,
so it has its own section.

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

A setup role the deployer cannot apply is a fourth case. `k3s` lives in `k3s-bringup.yml` and
`common` in no playbook, so `setup_tags_for` derives nothing for either. Such a range
fast-forwards and records the role instead of parking; see *A role only a hand can apply is
recorded, not parked* below.

A setup role can ship another setup role's file by path. The `deploy_ui` and `renovate_agent`
roles install the deployer's `files/gitops_markers.py` and `files/gitops_ledger.py` that way.
`deploy_changes.setup_roles_for` maps a change to such a file to its owner AND every role listed
for it in `SETUP_FILES_SHIPPED_BY_OTHER_ROLES`. `ChangeSet.setup_roles` and `setup_tags_for` both
read the table, so the `manual_plane` ledger class, the narrowed apply, the `hold_plane` coverage
and `land.sh`'s host reach all see the consumers. `narrow_setup.role_tags` diffs the shipped files
beside the consumer role's own directory, so the consumer narrows to the block that installs the
file. The table is static because every caller passes paths alone, and
`ansible/tests/setup/test_setup_cross_role_files.py` fails when it differs from the cross-role
`files/` and `tasks/` references in the setup roles' tasks.

- A `common/tasks/` file is in the table too. An `import_tasks` is static, so its tasks run under
  each importer's tags, and `narrow_setup` resolves the basename through the importing task.
  `setup_roles_for` drops `common` itself for a file with consumers, so the importers apply
  rather than the resolv.conf remediation `common` would print.
- A `common/templates/` file a common task file renders inherits that task file's importers. The
  kuma-check pair routes to the roles importing `kuma_check_timer.yml`. `narrow_setup` finds no
  reader of the template in `gitops_deploy`, `render_records` or `k3s`, so those apply or are
  recorded under their whole-role tag.
- `resolv.conf.j2` is the exception (`SETUP_FILES_ROUTED_TO_OWNER`). Both roles that render it are
  hand applies, since `k3s` runs from `k3s-bringup.yml` and `optimize_pi` runs on daniel-pi only,
  so it routes `common` to `manual_plane`, whose remediation names both commands.

A k8s role can import a `common` file too. `janitorr` and `configarr` copy `host_lib.py` through
`install_host_lib.yml` and stamp it through `stamp_deployed.yml`, and
`K8S_ROLES_IMPORTING_SETUP_FILES` puts both in `ChangeSet.k8s` when one of those three files
changes. The deployer applies a k8s role only for an image-pin bump, so each one defer-and-alerts
and records a `k8s_unapplied` line. The same test holds that table to the k8s roles' tasks.

The third class is the bring-up playbooks, which run by hand by construction. The deployer's own
role, `roles/setup/gitops_deploy/`, applies itself as `initial_setup.yml --tags gitops_deploy`. It
sat in the manual class until 2026-09-01 on the claim that applying it restarts the unit executing
the tick. It does not: the role's handler is `state: started`, which Ansible skips for an
`activating` unit, so a self-apply from inside a tick is a no-op on the unit and the new code runs
from the next tick. The park was costly, because every other session's landing stopped behind it
until an operator hand-ran the role and ff-merged. The `DECIDED:` marker above
`_BROAD_MANUAL_PREFIXES` in `deploy_changes.py` carries the evidence.

### A deploy-plane change is narrowed before it is applied

The deploy arm used to run `ansible/deploy.yml` unscoped, over every role, for any change under
`ansible/templates/` or `ansible/inventory/`. That cost about twenty minutes under the tree lock,
was 47% of all lock-busy time in the week from 2026-09-04, and twice failed on a gate belonging to
a service the change never touched and held the fleet.

`deploy_handlers.handle_broad` now asks `scripts/deploy_tools/deploy_tags.py narrow <local>
<origin>` which services the range actually reaches, before the fast-forward and at the two refs
the tick pinned. It runs as a subprocess because the derivation parses YAML and the unit runs
under `uv run --no-project`. The journal names the outcome on every tick:

- **tags**: `ansible/deploy.yml --tags <tags>`, recorded in the tick's receipt with those tags.
- **no tags**: the range moves no rendered output (a comment-only inventory edit, a variable
  nothing reads, a macro nothing imports). The fast-forward is the whole apply, and the receipt
  records `narrowed-to-nothing` as the plane's tags.
- **a refusal**: the full `deploy.yml`. Anything the derivation cannot map lands here: a variable
  the play itself reads, `hosts.ini`, a tag list covering most of the fleet, or a crash in the
  derivation. A missed consumer would leave a service silently stale, while a full run is only
  slow.

`narrow` is read-only and can be run by hand against any range. The rules it applies, and what
each one refuses, are in `scripts/deploy_tools/narrow_broad.py`.

- **A shared template maps to the roles that import it.** A name inside a Jinja `{# #}` comment is
  not an import. `claim-default.yaml.j2` maps to the roles whose `defaults/main.yml` declares
  `k8s_claims`, not to every caller of `k8s/manifests`, and the derivation refuses if
  `k8s_claims` is set anywhere but a role's defaults. An edit that changes only a shared
  template's Jinja comments maps to no tags: the rule compares the template's `jinja2` lexer
  token streams without comments, under both `trim_blocks` settings.
  `scripts/deploy_tools/narrow_templates.py` holds the rules.
- **A filter plugin maps to the roles that call its filters.** `ansible/filter_plugins/` is a
  play-level prefix, so every change under it used to refuse. The rule reads the names the
  plugin's `FilterModule.filters()` returns, at both refs, and greps the role trees, the shared
  templates and the play's own trees for each one. A call from `deploy.yml`, `pre_tasks/`,
  `tasks/` or `post_tasks/` still refuses, as do an inventory value that calls the filter, a
  `filters()` that is not a literal dict, a deleted plugin and a plugin another plugin imports.
  `scripts/deploy_tools/narrow_filters.py` holds the rule.
- **A setup role that calls a filter is outside both runs**, because `deploy.yml` applies no
  setup role. The deployer reads those callers from
  `deploy_cross_role.SETUP_ROLES_CALLING_FILTER_PLUGINS`, a static table that
  `ansible/tests/setup/test_setup_cross_role_files.py` holds to the tree. A plugin change joins
  the setup plane beside the deploy plane. The tick applies an `initial_setup.yml` caller such as
  `gitops_deploy`, and records a `k3s-bringup.yml` caller such as `k3s` as a `manual_plane` line.
  `narrow_setup.plugin_tags` narrows each caller to the readers of the vars key whose value calls
  one of the plugin's filters, so a `service_tier.py` change names `longhorn_backup,longhorn_r2`
  rather than `--tags k3s`. An inventory value that calls a filter refuses, and so does a role
  that names none of them. A refusal falls back to the whole-role tag.

**Every deploy-plane tick also logs a render-digest shadow line.** The `narrow shadow:` line names
the services whose applied digests differ from a render record of the commit being applied,
counts the ones that match, and counts the ones with no usable record, grouped by reason. It
applies nothing. It is the first step toward replacing the refusal's full run with a digest diff,
but it measures little today: `setup/render_records` writes one record per service
`scripts/deploy_tools/render_targets.py` lists, hourly at `:17`, from the newest commit on
origin/master with green CI. A tick that has just fetched a merge applies a commit no render has
seen, so the line reads `unknown: render is of another commit`. From 2026-10-01 to 2026-10-05
that held for every service on all 39 deploy-plane ticks.

**A full play measures itself instead.** `release_stamp.yml` hashes every service it applies
through the same `release_digest.yml` a render record uses, and at one commit on 2026-10-05 all
56 release digests equalled the render digests. When `narrow` refuses and the tick runs the whole
play, the tick snapshots the release records before the apply. After a successful apply it logs
one `narrow measured:` line naming the services whose digests moved, which is exactly what a
digest diff at that commit would have applied, plus the counts of unchanged services and those
with no usable record. The measurement costs no render and no lock time. A narrowed tick
re-stamps only its own tags and logs no such line, and a failed full play takes the hold path.
`deploy_release.applied_diff` is the reader, and `deploy_release.digest_diff` is a stdlib
restatement of `probe_lib/releases_render.py:digest_verdict` that
`tests/test_deploy_release_digest.py` runs over the same records.

A failed narrowed apply adds the plane it named to `hold_plane` as the entry
`ansible/deploy.yml <tags>`. Only an apply covering those tags drops that entry: an untagged full
run does, and a narrowed run covering a different service does not.

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

A range carrying `roles/setup/k3s/` or `roles/setup/common/` parked the whole tick until
2026-09-11. The wait bought nothing: the role needs `ansible-playbook ansible/k3s-bringup.yml
--tags k3s` whether or not the range is merged, while every other session's landing behind it
exits 4 from `deploy.sh` until a hand pulls the primary checkout.

The tick fast-forwards instead, and writes the role to the `owed` ledger
(`/var/lib/gitops-deploy/owed.jsonl`) as one `manual_plane` line per role, carrying its
origin SHA, playbook, first-seen stamp and narrow tags. A role already listed is not re-added,
so its first-seen stamp is the age everything else reads. Five consequences:

- the journal says `manual_plane pending: <roles> — apply by hand: <commands>` on **every**
  tick, not only the one that recorded it. The recording tick says
  `manual_plane recorded: <roles>` instead, naming only the roles it added, so no tick prints
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
`coredns,kubeconfig`, and the bare form takes `coredns` with yours. That change is then
merged, unapplied, and recorded nowhere:

```bash
ansible-playbook ansible/k3s-bringup.yml --tags kubeconfig
uv run python scripts/deploy_tools/gitops_state.py clear-owed manual_plane k3s --applied kubeconfig
```

The clear then keeps the line and prints what is still pending. Every surface that prints a
narrowed apply prints the matching `--applied` beside it, so following the printed pair is enough.
A clear whose `--applied` covers the whole row takes the line. A clear with `--applied` against an
EMPTY or missing row keeps the line and says so: an empty row means the whole role is pending,
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
`gitops_markers.SETUP_ROLES_OFF_THE_TICK_HOST` maps the role to its host, and the remediation
prints `ansible-playbook ansible/initial_setup.yml --tags optimize_pi -e target=daniel-pi`.
`ansible/tests/deploy/test_setup_roles_the_tick_host_skips.py` derives that table from the
playbook gates, so a second role gated off the tick's host fails it until the table names it.

#### The tag the remediation prints is derived, not the role tag

`--tags k3s` is the whole `setup/k3s` role: it reapplies MetalLB, Longhorn, the backup targets,
the crons, CoreDNS and the node config, and arms three gated control-plane tasks. A three-line RBAC
edit to `templates/readonly-rbac.yaml.j2` needed only `--tags kubeconfig`, which is what was
applied by hand on 2026-09-22 with ok=15 changed=2. The printed command therefore names the
narrowest tags the change reaches.

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

A landing that deploys its own merge commit skips the tick, so no row exists for its range yet.
It prints the tags derived for that PR alone (`land_tags.own_narrow_tags`), with the narrowed
`--applied` clear beside them, which drops only those tags from whatever row the next tick
records.

The ledger's readers ignore unknown keys, which is why the tags live on the line itself.
**Every task file in a setup role carries its own tags**, so the derivation maps a changed path to
the tags of the tasks that read it:

- `tasks/<f>.yml` maps to the tags on its own tasks.
- `templates/<f>` or `files/<f>` maps to the tags of every task file naming `<f>`, following a
  template another template includes or imports. A host script's template is usually named in a
  `defaults/` data structure rather than a task's `src:` (`setup/k3s` collects them in
  `k3s_render_stamp_groups` and hands the group to `common/tasks/release_bin.yml`), so a name found
  only there maps to the tags of whatever reads that KEY.
- `defaults/main.yml` or `vars/<f>.yml` maps to the top-level keys whose value changed between the
  two refs, then the tags of every task file and template naming one of those keys.

A path that reaches no host is SKIPPED, not refused: a `.md` outside `files/` and `templates/`
(one inside them can be copied or rendered onto a host, so it narrows), and a role-local `tests/`
directory. Refusing on those would leave the narrowing almost never firing, because most ranges
touching `setup/k3s` carry the role's own `CLAUDE.md`. A range of nothing but such paths refuses
instead, since an empty `--tags` value runs the whole playbook.

**Any other doubt refuses**, and a refusal prints the role tag with
`deploy_remediation.maximal_tag_warning` beside it. Refusing is the safe direction, because a
`--tags` value matching nothing makes Ansible exit 0 having applied nothing. The refusals that
fire in practice:

- a task file with no `tags:` of its own (`main.yml`, `unit-logging.yml`,
  `longhorn-weekly-shard.yml`), whose tasks inherit from the import site;
- a `defaults/` change that moved no key's value, which is every comment-only edit;
- a key nothing in the role reads, a change under `handlers/` or `meta/`, or a `defaults/` file
  either ref does not carry;
- a derived tag the remediation's playbook cannot reach. The role must sit in that playbook's
  `roles:`, and the task file must be statically imported (`import_tasks`, not `include_tasks`)
  from `tasks/main.yml`. `tasks/storage_smoke.yml` belongs to `k3s-storage-smoke.yml`, so
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
./scripts/deploy_tools/gitops_tick.sh
```

It runs the identical code path the timer runs. **There is no dry-run mode** — this deploys.

`--no-wait` starts the tick and returns at once, without watching it or printing its journal.
That is what a landing uses: `land.sh` deploys the merge commit of its own pull request
(`deploy.sh --at <sha>`), so it needs the tick only to converge the primary checkout, which
the timer does within ten minutes whether the request landed or not. It kicks it after the
deploy, not before — this unit holds the git-tree lock for its whole run, so a tick started
first is one `deploy.sh` then waits out to cut its snapshot.

## Gating on CI correctly

Do not hand-poll CI. `land.sh` owns the merge, the CI wait, the tick and the verify step, and
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
`ansible/roles/setup/gitops_deploy/CLAUDE.md`: the incidents each safety arm was added
after, the measurements each budget was sized from, and the trade-offs that were accepted
rather than fixed. The role file carries the rule; this page carries the evidence. It was
moved here from the role file on 2026-09-21 (#2126), and a paragraph's "below" or "above"
refers to its original neighbours in that file.


### Health gate and rollback: the phantom-container hold

**HISTORY — the Docker health gate and the stale-compose watchdog that followed it were removed with the deployer's Docker apply arm. No `has_gitops` host runs Docker. The 2026-08-08 `configarr` false rollback came from that gate: configarr had moved to a k8s CronJob, its rendered daniel-server compose was left behind, and `containers_for()` then gated a container that could never exist.**

The hold rules still govern a failed k8s deploy, whose rollback is `deploy_handlers._rollback_k8s`.
On failure the tick resets to the previous HEAD, redeploys the prior pin, writes the bad SHA to
`/var/lib/gitops-deploy/hold_sha` so the next tick does not redeploy it, and alerts the dedicated
Discord webhook. Reverting the offending PR advances `origin` past the held SHA, which stops the
`skip_hold` short-circuit. The marker itself, and the red **GitOps Deploy — Status** monitor
(which pages on a non-empty `hold_sha` with no origin comparison), clear only when a later tick
completes a *successful service deploy on this host*: the clean-deploy branch calls
`clear_service_hold()`, and noop or docs-only ticks return before reaching it. If everything
after the held SHA maps to no service here, diagnose the hold, then clear it with
`uv run python scripts/deploy_tools/gitops_state.py clear-hold <sha>` on daniel-box.

**A BROAD hold clears only when its own plane is applied**; see *Which apply clears a hold*.
`clear_service_hold()` is the service half of that rule. A k8s deploy is
`ansible/deploy.yml --tags <services>`, so it clears a hold naming that playbook at a subset of
those tags (a failed bump on a broad tick writes one) and leaves any other broad hold standing.

### The safety arms, in full

- **CI gate — the tip must be green before anything is merged or deployed** (`REQUIRE_CI`,
  `deploy_logic.ci_verdict`). Nothing else in the pull path consults a workflow result. The gate
  queries GitHub's check-runs API for `origin/master`, **authenticated through `gh auth token`**
  when the CLI is logged in (`host_lib.github_token`; `GH_TOKEN`/`GITHUB_TOKEN` in the
  environment win, and a logged-out gh degrades to anonymous), and only on a tick that would
  otherwise deploy. Anonymous, the limit is 60 requests an hour per source IP, shared with every
  landing's `await_ci.py` poll on the same host. Two landings exhausted it on 2026-09-01 and three
  ticks deferred on `HTTP Error 403: rate limit exceeded`, which reads as "CI not finished."
  Authenticated, the limit is 5000 an hour per token.
  - **A tip that is not green sends the gate walking, and the tick deploys the newest GREEN
    ancestor instead of deferring the range.** *What a tick does* has the steps.
    `deploy_phases.assess` reads `git rev-list --first-parent <local>..<origin>` newest first,
    asks GitHub about each commit below the tip, and stops at the first `pass`. That SHA becomes
    `target.origin`, and the ff-merge, the changed-path diff, the declarations read and the
    narrowing all read it. A `fail` ancestor is skipped, and a held SHA is dropped from the
    candidates whether it arrives as the tip or below one. The walk runs ONLY on a tick that would
    otherwise defer, and `CI_ANCESTOR_WALK_MAX` (`gitops_deploy_ci_ancestor_walk_max`, 10) bounds
    it, counting the tip's own request. **An UNAUTHENTICATED host does not walk at all** and says
    so in the journal, because a ten-request burst per tick would exhaust the anonymous limit and
    every reader's next call would read `HTTP Error 403`, which this gate maps to `pending`. The
    rule is `deploy_git.ci_walk_candidates`, which carries the `DECIDED:` marker. The property the
    gate keeps is that **the tree the host runs always has its own green CI verdict.** One line
    names the choice: `origin <tip8>: CI <pending|fail>; fast-forwarding to the newest green
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
  - `CI_CONTEXTS` holds GitHub check-run **names**, which must match `ci.yml`'s `name:` exactly.
    An empty list or repo **disarms** the gate with a log line rather than passing everything,
    the same fail-closed shape as the k8s denylist. Set `gitops_deploy_require_ci: false` to turn
    it off.
- **The tick does not consult staging.** The staging arm blocked prod on a rejection from
  2026-09-02 to 2026-09-29 and stopped no deploy in that life. `docs/archive/staging-phase-c.md`
  and `docs/archive/staging-cluster.md` are the record. `roles/setup/hypervisor` stays for the
  monthly etcd restore drill's throwaway guest.
- Read-only against the repo (no push); rollback is local-only and self-guarding.
- Refuses to *deploy* from a dirty working tree (operator mid-edit), but the tick still completes
  and writes `last_run` (`next_action(..., dirty=True) -> "dirty"`). The skip is healthy, so it
  must not trip the GitOps-Alive stale-file threshold. `should_alert_dirty` throttles the page to
  twice per America/Chicago day (the first tick at or after 08:00 CT and at or after 20:00 CT),
  with the slot key `YYYY-MM-DD:am|pm` in `/var/lib/gitops-deploy/dirty_alerted_date`. Without it
  a long edit session would re-page every 10-minute tick.
- **Test-suite paths are skipped before every plane below** (`deploy_logic._is_test_only_path`):
  `ansible/tests/`, any role-local `tests/` directory, and a `test_*.py`/`conftest.py` beside the
  module it covers. Every prefix and regex here matches on path alone, so a test file used to read
  as whatever plane it sat under. PR #707 was three test files that set `broad_manual` and
  defer-alerted, which cost an operator a hand-run playbook and an ff-merge. A test-only push now
  produces an empty `ChangeSet` and takes the `if not cs.services` ff-merge branch, like a
  docs-only push. The invariant that no role ships a test file to a host is enforced by the
  `no-role-ships-a-test-file` row of `ansible/tests/repo/test_census_rows_roles.py`.
- **A new module in `files/` goes in two lists in `tasks/code.yml`**: the copy task's `loop:` that
  installs it under `/opt/gitops-deploy/`, and `stamp_deployed_pairs`, which records its render
  provenance. pytest imports from `files/` on disk, so a module forgotten in the copy loop passes
  CI and kills the deployer at import on its next tick. ENFORCED by
  `ansible/tests/deploy/test_gitops_deploy_ship_list.py`, the sibling of
  `test_monitor_bridge_modules.py`.
- **Broad changes split three ways** (`deploy_changes._BROAD_*_PREFIXES`); *Broad changes* above
  has the table, the narrowing and the tag routing. **A range carrying both the setup and the
  deploy plane applies both, setup first.** `deploy_narrow.plan` returns one plan per plane,
  `handle_broad` runs them in order, and they share one `BROAD_DEPLOY_TIMEOUT_S` so the two read
  as a single apply against the unit's ceiling. The deployer once ran an if/else here and the
  setup arm won, so two Pi retirements that landed a `roles/setup/optimize_pi` edit beside a
  `host_vars/daniel-pi.yml` edit never planned the deploy plane, and `Release Staleness Drift`
  sat DOWN over 56 unstamped records.
  - **The promoted k8s image bumps in the same range are deployed too, after both planes.**
    `split_k8s_auto_deploy` moves an eligible bump out of `ChangeSet.k8s` into `k8s_deploy`, and
    `alert_deferred` fires its k8s channel on `k8s` alone. A broad tick used to fast-forward the
    bumps, deploy none and name none, and the fast-forward then removed them from every later
    tick's range. Nine pods kept serving their old images that way on 2026-09-24.
    `deploy_broad_k8s.apply_broad_k8s` runs after the plan loop.
    - **Only a busy lock re-derives the bumps.** Its arm undoes the ff-merge, so the next tick's
      range carries them again. A FAILED apply leaves the tree fast-forwarded past them and never
      resets, so `broad_failure_alert` names every promoted bump with the `deploy.sh --tags` line
      that deploys it, and the deferred-change pages (`alert_deferred`) go out on that path too.
    - **A bump the deploy plane covers is deployed once, by the plane, ungated.**
      `narrow_broad._changed_half` builds its ChangeSet from the raw paths, so a range that also
      moves a deploy-plane path puts the bump's tag in the narrowed list, and a refused narrowing
      runs the whole play. `deploy_broad_k8s.covered_by_plane` takes those bumps out of both the
      gate and the separate deploy. A second `--tags sonarr` would re-take the Longhorn snapshot
      of every claim sonarr declares and spend the shared budget twice.
    - **Forward-only, on the broad budget.** `_rollback_k8s` resets the tree to `local`, which
      here would undo the ff-merge under a setup plane this tick already applied, so the tree
      would claim the old commit while the host runs the new one. The bumps share the plans'
      `BROAD_DEPLOY_TIMEOUT_S` instead of taking a `K8S_DEPLOY_TIMEOUT_S` of their own. The broad
      path's worst case is then 180 flock + 1800 apply = 1980 s, under the k8s path's 3240 s, so
      the unit's ceiling does not move.
    - **The bump deploy starts only when `K8S_DEPLOY_TIMEOUT_S` still fits.** With less left, the
      run would die at the timeout (a hold and a page, often for a healthy service) or its lock
      wait would raise `ServiceLockBusy` and reset the tree under planes that applied. So it
      defers with no hold and no reset: the bump joins `ChangeSet.k8s` and the defer-and-alert
      post names it. That post fires **once**, and the tick has already merged the bump.
      The budget deferral is also written to **`k8s_deferred`**, which `gitops_status` pages on at
      six hours, because `Release Staleness Drift` is DOWN for any stale record in the fleet and
      is no dependable backstop. The hand-edited and denylisted classes go to **`k8s_unapplied`**,
      which `gitops_status` never opens: forty of the fifty-four k8s roles are denylisted, so
      paging on them would hold Status red as normal operation. *The `k8s_deferred` and
      `k8s_unapplied` markers, in full* has the discharge and clear rules.
    - **A failure writes `hold_sha` and adds `ansible/deploy.yml <bumps>` to `hold_plane`**, the
      run that failed, beside any plane already held. With no plane recorded, the next unrelated
      service deploy cleared the hold through `clear_service_hold` and Status went green over the
      failed pin. `clear_service_hold` clears a hold only when the services it deployed cover the
      held tags, so the fix-forward deploy of the same service is the way out.
      `deploy_alert_text.broad_k8s_failure_alert` is a separate body from `k8s_failure_alert`: it
      must not say "rolled back locally" or send the operator after a volume revert that never
      ran. The pre-apply Longhorn snapshot IS taken, because `k8s/manifests` takes it on every
      apply of a claim-declaring service, so a hand revert stays available.
  - **The deploy plane is narrowed before it is applied**, by `deploy_narrow.plan`; *A deploy-plane
    change is narrowed before it is applied* above has the three outcomes. **The narrowed list is
    not filtered through `K8S_AUTODEPLOY_DENYLIST`.** The denylist gates promotion into the k8s
    auto-deploy machinery (snapshot, rollback), and the broad plane has none of it, running the
    plain playbook forward-only the way an operator's `deploy.sh` does. A filter could only reach
    the narrowed path anyway, since a refused range runs the whole play over every denied role.
    The `# DECIDED:` on `deploy_narrow.denylisted_in` is the long form, and the journal names the
    denied tags a narrowed apply includes. The `# DECIDED:` at the fallback in `deploy_narrow.py`
    says why doubt runs the whole play.
  - The narrowing reads the k8s role-caller graph from the WORKING TREE, which is still on `local`
    at that point. A range that adds a caller of a shared role reads one caller short, and a shared
    role that then looks caller-less refuses, which is the safe direction.
  - The inventory scan skips a whole-line comment, so a key consumed only on a `#` line inside a
    block scalar (`key: |`) would narrow rather than refuse, which is the unsafe direction. No
    inventory block scalar carries such a line. One appearing is the reason to parse instead of
    matching lines (`_defines_only` in `narrow_broad.py`).
  - **`roles/setup/<name>/` is not `initial_setup.yml --tags <name>`.** The playbook may not
    include the role (`k3s` is in `k3s-bringup.yml`, and `common` is in no playbook and is read by
    two roles on two hosts), and the tag may not be the directory name (`chezmoi_setup` is tagged
    `chezmoi`). Either way `--tags` matches no task, `ansible-playbook` exits 0, and the tick
    would record the change as applied. That happened on 2026-09-01 with a `roles/setup/k3s/`
    change that installed daniel-box's host DNS forwarder. `setup_role_playbook` and
    `setup_role_tag` own the routing, `setup_tags_for` returns nothing for a role
    `initial_setup.yml` cannot apply, and `broad_remediation` names the real playbook. The map is
    hand-written because this module runs under `uv run --no-project` and cannot import `yaml`.
    `ansible/tests/deploy/test_setup_role_playbooks_agree.py` derives the truth from the
    playbooks and fails when the two drift.
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
    (`deploy_remediation.broad_park_reason`). The Discord page is throttled once per SHA, and
    the journal used to be throttled with it, so a stuck range logged nothing from the second
    tick on. The journal is what an operator reads when `land.sh` exits 4.
  - **This role applies itself** (*Broad changes* above). A failed self-apply takes the same hold
    path as any setup role.
  - `BROAD_DEPLOY_TIMEOUT_S` (1800, `gitops_deploy_broad_timeout_s`) bounds one apply. Without it
    a wedged run is SIGTERMed at `TimeoutStartSec` with no hold written and no alert sent.
- **Behind-origin watchdog** (`deploy_logic.behind_marker`): every PARK leaves the host on an old
  tree, and `last_run` keeps ticking (Alive green) while `is_diverged` stays false (origin is a
  strict descendant, so Status is green too). Each tick therefore records
  `"<origin_sha> <first_seen_ts>"` to `/var/lib/gitops-deploy/behind_since` when it ends behind
  origin, and clears it on convergence. monitor-bridge's **GitOps Deploy — Status** pages once the
  age exceeds `GITOPS_BEHIND_MAX_MIN` (6 h). The marker is written AFTER `main()` so it reflects
  the state the tick finished in. The watchdog is age-gated because being behind is normal in the
  small: a push is behind for one tick, and the dirty path is behind for a whole edit session.
  - **The stamp measures TIME WITHOUT A FAST-FORWARD, not time behind the tip.** Any tick that
    moved the tree renews it: `entrypoint()` compares HEAD before and after and passes
    `fast_forwarded=` to `record_behind`. A tick that moved nothing keeps the stamp, so a trickle
    of pushes to a stuck host cannot restart the clock. A rollback resets HEAD to where it
    started, which counts as no progress. The ancestor walk makes the two readings differ: a
    deployer landing every merge at the newest green ancestor is behind the tip on nearly every
    tick, and a per-SHA stamp aged past six hours while the deployer worked normally.
    `entrypoint()` re-resolves the REAL `origin/<branch>` after `main()` returns, so
    `behind_since` names the tip and a tail that never goes green still pages at 6 h. Readers
    state the meaning in their own prose: `scripts/lib/deployer_park.py` (shared by `deploy.sh`
    exit 4 and the SessionStart banner) and `monitor-bridge/files/checks/gitops.py`.
    `Landing.tick_state` checks the PR's own merge commit against the primary checkout before it
    believes `behind_since`, so `land.sh` does not report `deferred` for a PR the tick landed at
    an ancestor.
  - **A wedge cannot game the re-stamp.** Every path that stops the deployer converging (a park,
    a hold at the tip, a dirty tree, a diverged tree, a tail with no green ancestor, a contention
    reset, a rollback) leaves HEAD where the tick found it, so `fast_forwarded` is false. A
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
  rotation pushed from another machine) are fast-forwarded but **not** redeployed: the new
  value only reaches a container on its next deploy, so the deployer alerts (once per SHA,
  the `secrets` slot of `alerted_shas`) to redeploy the consumers. On a broad tick that page fires
  from every exit that leaves the range merged (#2383): each of them returns with the range
  already fast-forwarded, and `alert_once` advances its marker on detection — so a page
  skipped there is never sent, and the rotation sits merged and stale with nothing naming it.
  The contention arm is the exception and sends nothing, because it resets the tree (#2459).
  `secrets.yml` is deliberately
  NOT in the broad list — the `/add-secret` flow ships it WITH the consuming template, which
  stays a scoped single-service deploy (`deploy_logic.ChangeSet.secrets`).
- **k8s-platform roles are auto-deployed ONLY for an image-pin bump to a non-denylisted service;
  every other k8s change defers-and-alerts.** `deploy_logic.split_k8s_auto_deploy` decides
  eligibility, **diff-shape first, identity second**. Gating on the service name alone would be
  unsafe, because `role_of` matches the WHOLE role directory and a name-only allowlist would
  auto-deploy ConfigMap, `tasks/` and template pushes, none of which carry Renovate's soak. A
  service qualifies only when the feature is enabled, it is not in
  `gitops_deploy_k8s_autodeploy_denylist`, the pilot scope (if set) names it, the only path the
  push touched under its role is `defaults/main.yml`, and every changed line in that file assigns
  an `*_image:` var. Everything else stays in `ChangeSet.k8s` and defers.

    `filter_plugins/k8s_autodeploy.py` derives the denylist from each role's own
    `k8s_autodeploy` declaration in `roles/k8s/<name>/defaults/main.yml`, where the reason sits
    beside the role it describes. The filter fails closed: a role that declares nothing raises at
    template time, because absence from the denylist means auto-deployable. To stop a role
    auto-deploying, set `k8s_autodeploy: false` in its defaults. No central list exists.

    **That edit alone does not reach the host.** It lands under `roles/k8s/<role>/`, so the
    deployer routes it to `ChangeSet.k8s`, and its defer-and-alert names a `deploy.yml --tags
    <svc>` run. `deploy.yml` runs no setup role, so it never re-renders
    `/etc/gitops-deploy/config.env`, and the host's denylist stays the old one. Run
    `uv run ansible-playbook ansible/initial_setup.yml --tags gitops_deploy` to push the change.
    This is the silent-no-op class that `broad_remediation()`'s docstring warns about.

    **Adding a NEW role that declares `k8s_autodeploy: false` is the same edit site with the same
    consequence, and it recurs.** `game-stats-lib` landed with `k8s_autodeploy: false` and the
    host disarmed for six hours until an operator re-rendered. A stale denylist disarms image-pin
    auto-deploy for EVERY service, because the disarm compares the whole set. The
    `new-k8s-service` skill carries the re-render as a step.

    The trigger for the re-render is asymmetric with what the denylist reads. The denylist
    derives from every role under `roles/k8s/`, while the only changed-path match that re-renders
    `config.env` is `_BROAD_SETUP_PREFIXES` (`ansible/roles/setup/`). A change to the derived
    denylist therefore matches no prefix that re-renders it.

    `deploy_phases.reconcile_denylist` closes that gap without touching the prefix sets. It
    states the invariant locally: `config.env` must agree with the declarations at the
    checkout's own HEAD. When it does not, the reconcile runs `initial_setup.yml --tags
    gitops_deploy` itself. The render reads the working tree, so it can only produce HEAD's list,
    and the heal lands on the tick AFTER the ff-merge, which is normally an idle one. Six
    properties matter before changing it:

    - **It gates on the FILE-level enable flag**, not the one the fail-closed disarm leaves
      behind. `gitops_deploy.py` flips `K8S_AUTODEPLOY_ENABLED` to False when the rendered
      denylist is empty, so a `config.env` that lost its denylist line would disarm the reconcile
      whose re-render is the repair. `Config` carries both values off one `config.env` key, the
      parsed one as `k8s_autodeploy_enabled_in_file` and the post-disarm one as
      `k8s_autodeploy_enabled`. The file says off: skip. The file says on with no denylist:
      re-render. The file says on with a denylist: compare.
      `_promote_k8s_auto_deploys` still reads the post-disarm value, so the damaged state heals
      without promoting anything.
    - **It passes `gitops_deploy_kick_after_change=false`.** Rendering `config.env` notifies this
      role's `Run gitops-deploy once` handler, whose `systemctl start` blocks on the activation
      the render runs under, so the heal would self-deadlock until its timeout. ENFORCED by
      `ansible/tests/deploy/test_denylist_render_suppresses_the_kick.py`.
    - **`denylist_rendered_sha` guards it once per checkout SHA.** That bounds the cost (the
      per-role `git show` reads are skipped on idle ticks) and the retry (a mismatch a re-render
      cannot fix renders once, not every ten minutes). The marker is written BEFORE the run, so a
      SIGTERM mid-play cannot loop it.
    - **A render ENDS that tick.** The in-memory config still holds the list the render just
      disproved. Every arm of this unit is also non-stacking by construction (the unit template
      sizes `TimeoutStartSec` as `max(broad, k8s + rollback)`), so a render that ran on to a k8s
      deploy would be the first arm to add its budget to a sibling's and could be SIGTERMed
      mid-rollback. The next tick reads the fresh config.
    - **A dirty tree is never rendered from**, because the filter reads the working tree and would
      bake a list nobody pushed.
    - **A failed render writes no `hold_sha`.** It rewrites only this deployer's own
      `config.env`, so a failure leaves the old file, which the tick already tolerates with
      auto-deploy disarmed by the origin comparison below. Parking every unrelated deploy behind a
      config render would be a larger outage than the one it heals.

    **The origin-side comparison stays the fail-safe** for the window where origin is ahead of the
    checkout. Each tick, `k8s_declarations_at(origin)` reads every role's `defaults/main.yml` at the
    SHA the tick already pinned, and `declared_denylist()` parses it with a stdlib regex, because
    the unit runs under `uv run --no-project` and cannot import `yaml` or the filter plugin. If the
    result disagrees with `K8S_AUTODEPLOY_DENYLIST`, or cannot be read, k8s auto-deploy is disarmed
    for that tick. The disarm is stateless and self-clears when the config is re-rendered, and only
    the page is throttled (the `stale_denylist` slot). Both mismatch directions usually mean config
    is behind origin, so the page leads with the re-render.

    The regex is biased toward denied: unanimity is required across every match, an absent or
    unparseable declaration counts as denied, and a shared role skips the check. A parsing bug
    therefore produces a spurious disarm rather than a permitted deploy. The one gap is a file that
    is invalid YAML overall, which the regex can read a value out of and the filter would raise on.
    `ansible/tests/deploy/test_denylist_parsers_agree.py` runs the filter against the live tree and
    fails on that shape, and `REQUIRE_CI` refuses to promote a red tip.
  - **The gate is in the play, not here.** `roles/k8s/manifests` applies,
    `roles/k8s/manifests/tasks/drain.yml` runs `rollout status --timeout`, and
    `ansible/post_tasks/k8s_stabilise_gate.yml` holds the post-Available soak that hard-fails
    on a restart-count delta or a readiness shortfall. (All three lived in
    `roles/k8s/manifests` until 5eea64e6 batched the rollouts and deferred the soak to
    end-of-play.) This deployer adds no health-poll phase for
    k8s: `containers_for()` returns `[]` for a k8s service, which is exactly the 2026-08-08
    configarr false-rollback. The denylist covers the roles that gate can't protect — see each
    role's own `roles/k8s/<name>/defaults/main.yml`, where its `k8s_autodeploy_reason` carries
    the reason.
  - **A k8s rollback is local-only, and that is not sufficient on its own.** On failure the tick
    holds the bad SHA, `git reset --hard`es, redeploys the prior pin, and pages. But `skip_hold`
    matches only while `origin_head == hold_sha`, so **the bad pin is still on master** — the next
    push past the held commit redeploys it. The Discord page says so; the fix is a revert on the
    remote (or a Renovate `allowedVersions` pin), not just clearing the hold.
  - **A clean k8s tick is the second place `hold_sha` clears.** `clear_service_hold()` also sits
    in the Docker health-gate branch, which an all-k8s host never reaches — without this the
    first rollback would leave **GitOps Deploy — Status** red permanently and need a manual `rm`.
    It clears a rollback hold, and a broad-tick bump hold its services cover; any other BROAD
    hold survives it, per *Which apply clears a hold*.
  - **One tick promotes at most `gitops_deploy_k8s_autodeploy_max_per_tick` services (default
    3).** Every promoted service shares one `ansible-playbook` run under one
    `K8S_DEPLOY_TIMEOUT_S`, and the failure path resets the whole merged range, so an overlong
    batch discards the good bumps with the bad one. The surplus stays in `ChangeSet.k8s` and
    defer-and-alerts with a Discord message naming the services to deploy by hand. **A later tick
    does not retry it**: the ff-merge runs before the deploy, so a successful tick leaves
    `local == origin` and every later `next_action()` is a noop.
  - **The Discord page above is a one-shot detection, not a durable signal.** `alert_once` fires
    it once per origin SHA, and the ff-merge that follows clears `behind_since`, so a deferred
    k8s change leaves every OTHER monitored marker clean while the cluster keeps running the old
    manifests. Renovate automerges digest bumps on the `k8s_autodeploy: false` roles
    (`renovate.json`), so this path runs unattended with one Discord line as its signal. The
    `# DECIDED:` at the `cs.k8s` branch of `deploy_alerts.alert_deferred` points here. The
    durable signal is a daniel-box cron owned by `sys_user`, not root
    (`roles/setup/k3s/templates/release-staleness-check.sh.j2`, tag `release-staleness`, every
    `k3s_release_staleness_cron_minute`), because it runs `git` and `uv` against the primary
    checkout and a root run would leave root-owned objects there. It runs
    `uv run python scripts/diagnostics/probe.py releases --stale-only`.
    - The check compares each service's release record (the applied commit that
      `roles/k8s/manifests/tasks/release_stamp.yml` stamps on every real apply) against
      `origin/master` under that service's own role AND the shared roles that supply bytes to
      every service's manifests (`probe_lib/releases.py`'s `manifest_affecting_shared_roles()`).
      It pushes the "Release Staleness Drift" Kuma monitor down when any service is stale or has
      no record.
    - A record whose service no `containers_list` entry declares is dropped first
      (`releases_retired.py`), because a retired role's record outlives the role and the commit
      that deleted it would read as drift no deploy tag can clear.
    - The entry-less roles holding only `tasks/` and `defaults/` (`volume-snapshot`,
      `volume-revert`, `cronjob-gate`, `longhorn-api`) are OUT of the shared set. They change how a
      deploy runs, never what it applies, so no stamp goes stale. Sweeping them in marked all 53
      services stale for a change that rendered no manifest.
    - The deploy plane is in the census too. For each changed path under `ansible/inventory/` or
      `ansible/templates/` since the record, `releases.py` asks `narrow_broad.broad_path_tags`,
      the per-path rule the tick narrows a broad range with, which services the change reaches. A
      path the rules refuse marks every service sharing that record stale, which is the set the
      tick's full run re-stamps.
    - The flag needs no clearing rule, because the next real apply of that service rewrites its
      record.
  - **Accepted, not fixed: the batch-abort blast radius is this branch's most likely bad day.**
    `K8S_AUTODEPLOY_MAX_PER_TICK` (3) and `ansible/tasks/k8s_batch.yml` share one
    `ansible-playbook` run with no `rescue`. One service's failed revert during a rollback aborts
    the WHOLE batch's rollback, so every co-batched service already reset for its own revert is
    left mid-rollback, on the failed commit's manifests or mid-revert with a volume attached in
    maintenance mode. The proper fix is one `ansible-playbook` run per service on the rollback
    path. See *The rollback timeout, derived* for the same batching mechanism's effect on
    `K8S_ROLLBACK_TIMEOUT_S`.
  - **A deploy cannot proceed without its snapshot.** `k8s/volume-snapshot` fails the deploy
    before the apply when any snapshot is unready, so the rollback alert's revert-status note
    (`rollback_volume_revert_note`), which names a service from its role DEFAULTS
    (`k8s_autodeploy_snapshot_pvcs`), cannot promise a revert for a service with no recovery
    point.
  - **Accepted: nothing self-heals a volume left attached in Longhorn maintenance mode after a
    failed rollback.** If `k8s/volume-revert` stops partway, the volume can stay attached with
    `disableFrontend: true` and the workload at zero replicas
    (`docs/volume-revert-drill-and-sizing.md` has the hand recovery). The role's NEXT deploy
    mounts the same RWO claim, and whether that mount succeeds, hangs or fails against a
    maintenance-mode attachment is untested. Treat a stuck maintenance-mode attach as blocking the
    service's next deploy until cleared by hand.
  - **The pilot list is empty, so the denylist alone decides.** An empty
    `gitops_deploy_k8s_autodeploy_pilot` admits every non-denylisted service rather than none.
    That is the opposite of the empty-denylist guard, which disarms the feature. The
    `ansible/tests/test_k8s_autodeploy_*.py` family enforces the three role shapes that must never
    be eligible: a rendered Deployment whose name is not in the gated set (a role gating a second
    Deployment by name via `manifests_extra_rollouts` is fine; a name that cannot be resolved
    statically counts as ungated), `manifests_rollout: ''`, and a gated Deployment with no
    `readinessProbe`.
  - **The deploy is time-bounded by `K8S_DEPLOY_TIMEOUT_S`.** Without an explicit timeout the
    only bound is systemd's `TimeoutStartSec` SIGTERM, which can land mid-rollback.
  - **A Pi Docker change riding along does not defer the bump.** No `has_gitops` host applies a
    Pi role, so there is no Docker deploy for the k8s branch to skip. `deploy_handlers.log_pi_changes`
    names whichever Pi shape rode along, from each handler's own ff-merge.

  **A k8s change that is not an eligible bump defers.** A path under `roles/k8s/**` that is not an
  eligible image-pin bump lands in `ChangeSet.k8s` (`deploy_logic.role_of` matches the whole role
  directory). The tick still `--ff-only` merges it and does not deploy it.
  `alert_deferred` posts once per SHA on the `k8s` alert slot. Deploy the change with
  `./scripts/deploy.sh --tags <svc>`, even where the post prints the bare `ansible-playbook`
  form. The compose mechanisms in this page (`containers_for()`, the
  health gate) are inert for k8s roles, because they act only on
  `containers/<svc>/docker-compose.yml`, which no k8s role renders.

  **A broad tick subtracts the roles its own deploy plane applied before it posts this**
  (`deploy_broad_k8s.apply_broad_k8s`). `narrow_broad` maps a role's changed path to its tag, so a
  range carrying a deploy-plane path narrows to a list that names the role, and a refused
  narrowing runs the whole play. Without the subtraction the tick would run
  `deploy.yml --tags radarr,sonarr` and then post "fast-forwarded but **not applied**" for the
  same roles.

  **HISTORY — before the k8s migration, the path-to-service mapper fed only the Docker
  `deploy(cs.services)` call, so a change under `roles/k8s/**` matched no branch and `main()`
  treated it as a docs-only push. The `ChangeSet.k8s` class closed that gap on 2026-08-13.**
- **A service's structural dirs (`tasks/`, `defaults/`, `vars/`, `handlers/`, `meta/`)**
  are ff-merged but NOT auto-deployed, so the deployer defers-and-alerts (once per SHA,
  the `tasks` alert slot) to redeploy the affected services by hand. `tasks/` and
  the role-root catch-all share that channel; `*.md` (CLAUDE.md/README) stays a silent
  ff-merge. This fires whether or not the tick deployed something else: a
  *combined* push (`svcA`'s template + `svcB`'s `tasks/`) deploys `svcA` but still flags `svcB`'s
  unapplied structural change (`deploy_logic.deferred_service_alerts`, keyed on the not-deployed
  remainder `cs.tasks - deployed`, run on both branches). A service whose own template changed
  rode its scoped `--tags` redeploy, so it's not re-flagged. Only fires on a clean deploy — a
  health-gate rollback git-resets the whole commit, reverting the structural change too.
- Acts **only when origin is strictly ahead of local** (`is_ancestor(local, origin)` →
  `next_action(..., origin_ahead=…)`). Un-pushed local commits make origin an *ancestor* of
  local; that's a no-op, not a deploy — otherwise the tick would diff `local..origin` (the
  *reverse* of those commits) and mis-fire a redeploy + false rollback. Push to clear it.
- **Divergence watchdog** (`deploy_logic.is_diverged`): if local and origin differ yet *neither*
  is an ancestor of the other (for example `secret-rotate` committed locally, its push failed, then origin
  advanced), the deployer can't fast-forward and every tick noops while origin's new commits — a
  Renovate/security bump — never deploy. Both other GitOps signals stay green (`last_run` keeps
  ticking, no hold), so each tick writes the diverged SHA to `/var/lib/gitops-deploy/diverged_sha`
  (cleared once resolved) and `monitor-bridge`'s **GitOps Deploy — Status** monitor pages on it.
  A merely unpushed local commit (`local_ahead`) is NOT flagged — that's the plain no-op above.
- Each `has_gitops` host runs its own independent instance of this deployer, with its own git
  clone, state dir and lock. Only daniel-box has one.
  - **By design, Pi-only services are NOT auto-deployed by GitOps.** The Pi has
    `has_gitops: false`, and no GitOps or CI deploy path reaches daniel-pi. A change under
    `roles/containers/` ff-merges and the tick logs the role it did not deploy. The Pi is a
    memory-constrained Zero 2 W driven manually over SSH, and a Renovate image bump to a Pi
    service is rare. Deploy to the Pi by hand: `./scripts/deploy.sh --tags <svc> -e
    target=daniel-pi`. A Pi-side deployer or a CI cross-host gate is worth revisiting only if
    Pi-service churn makes the manual step a real miss.

### The `k8s_deferred` and `k8s_unapplied` markers, in full

Both hold one entry per service for a k8s change a broad tick merged and did not deploy. Both
are classes of the `owed` ledger, `/var/lib/gitops-deploy/owed.jsonl`: one JSON object per line
carrying `class`, `subject`, `origin` and `at`.

**The ledger exists because the line markers break on a new field.** Each line parser accepts an
exact field count and skips anything else, and the monitor-bridge, `deploy_ui` and
`renovate_agent` roles each redeploy their copy on their own schedule. A field a new deployer
appended therefore read as no pending work in a reader that had not redeployed.
`gitops_ledger.parse_owed` ignores keys it does not know, and every writer carries them through a
rewrite.

**Four classes live in the ledger**: `k8s_unapplied`, `manual_plane`, `k8s_deferred` and
`hold_plane`. The line-marker predecessors of the last three are gone, and `tasks/install.yml`
reaps their basenames. Every reader goes through one function per class:

- `gitops_ledger.manual_plane_entries` returns the `manual_plane` lines, which carry `playbook`
  and `tags` beside the common keys. A line missing `playbook` or carrying malformed `tags` still
  pages, for the whole role. monitor-bridge carries a generated copy of the module.
- `gitops_ledger.k8s_deferred_entries` returns the `k8s_deferred` lines. monitor-bridge, the
  SessionStart banner and the `deploy_ui` panel read it, and `deploy_ui` installs
  `gitops_ledger.py` beside its `gitops_markers.py` for this. Two lines naming one service read
  as one bump, dated and attributed from the older line.
- `gitops_ledger.held_planes` returns each `hold_plane` subject once, oldest first. Its readers are
  monitor-bridge's Status check, the `deploy_ui` panel, `renovate_agent`'s skip reason,
  `k3s_upgrade_gates.held_sha` and the scheduled-jobs page. Each reaches it through
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

**`k8s_deferred` records what the tick chose to defer and does not report again.** A BUDGET
deferral goes here (#2449). The deferral post names it once and the range is merged, so no
later tick's `local..origin` carries the bump. `Release Staleness Drift` reads the unapplied
pin, but that monitor is DOWN for any stale record in the fleet, so a new deferral adds
nothing to an already-red tile. `gitops_status` therefore pages on the line's own age, at
the six hours `manual_plane` uses.

**`k8s_unapplied` records the hand-edited and denylisted classes** (#2570). Forty of the
fifty-four k8s roles are denylisted, so paging on those would hold Status red as normal
operation. `gitops_status` never opens this file; the SessionStart banner and the journal
read it, so it is a durable record that does not page.

**A second change to a service already in `k8s_unapplied` moves its line to the newer SHA**,
keeping the first-seen stamp (#2644). The line is discharged by comparing its origin to a
release record, so a line left at the oldest origin would drop as soon as any deploy descended
from the FIRST change, with the second still unapplied. `k8s_deferred` keeps its oldest origin
instead, because a tick clears that marker by deploying the service, and `unrecord` can reset
the tree under it.

**A torn line naming a service is repaired in place, on both.** The parser skips a line it cannot
read, so a writer trusting only the parsed entries would append a second line beside the torn one,
and every clear and discharge would leave that one standing. The repair happens at the next
record or clear naming that service. A torn line for a service nothing touches again stands until
`gitops_state.py clear-owed k8s_unapplied <svc>`, and a line naming no service is carried
untouched.

**Who writes, who discharges, who clears.**

- `deploy_alerts.alert_deferred` writes the lines. It covers every exit that leaves the range
  merged. The contention arm resets and returns before reaching any of them, so `unrecord` owns
  no reverse for it.
- A role whose directory is gone at origin gets no line. `deploy_phases.plan_tick` drops it from
  `cs.k8s` after a `git ls-tree` at origin, because no play can run a deleted role and a role with
  no callers has no tag whose deploy could discharge it. `land_shared.shared_roles` applies the
  same rule on the landing side. An empty or unreadable listing drops nothing. A line an earlier
  tick wrote before a later range deleted its role is dropped by
  `deploy_k8s_owed.drop_deleted_k8s_unapplied`, which `reconcile` runs right after the discharge.
- Every tick DISCHARGES a `k8s_unapplied` line whose service has since been deployed
  (`deploy_k8s_owed.discharge_k8s_unapplied`), from the service's release record and one `git
  merge-base --is-ancestor`. That drops the line for an operator's own `deploy.sh`, which the
  deployer cannot see. A record that is absent or carries no date KEEPS the line. A shared role
  (`manifests`, `image-builder`) has no record of its own, so its line drops when every tag that
  runs it carries the change, as `scripts/deploy_tools/shared_role_callers.py:caller_tags`
  derives them.
- Two more callers run the same discharge, so a deployed change does not wait ten minutes for the
  next tick. A successful `deploy.sh` runs it last (`deploy_playbook.discharge_owed_k8s`), and a
  `--detach` run runs it before its health gate (`deploy_detach.deploy_and_gate`). It waits 5 s
  for the tree lock and otherwise leaves the line to the next tick. The tick that records a line
  also drops it at once when the service's own release record already carries it
  (`deploy_k8s_owed.alert_and_record_deferred`), because a fast-path landing deploys at its merge
  commit before the tick it kicks records the change. That check reads only the own record, since
  the shared-role and render proofs each start a subprocess.
- A caller carries the change when its release record descends from the line's commit. For
  `manifests` alone, a caller also carries it when a render at a commit descending from the line's
  matches its applied digests (`deploy_release.render_proof`). The render need only hold the
  change, so the hourly producer answers within about an hour of a merge. Every other shared role
  acts outside the digest, and `deploy_k8s_owed.DIGEST_PROVABLE_ROLES` carries the `# DECIDED:`
  that says how each one does.
- A service's OWN line takes the same render proof when its role acts only through the bytes the
  digest covers. `scripts/deploy_tools/digest_provable.py` derives that per role, fail-closed:
  every task is a `k8s/manifests` include, a pure fact or check module, or an include of a task
  file that meets the same rule, and the role has no handlers or meta dependencies. A comment-only
  template edit to such a role then discharges within about an hour with no deploy. A role that
  includes `image-builder`, writes a host file or calls an API still needs its record to descend.
- Each line is written at the newest commit in the tick's range whose own diff reaches its
  service (`deploy_phases.k8s_change_commits`), not at the tick's tip. A landing deploys its PR's
  commit, so a line at a later, unrelated tip could never discharge by ancestry.
- Any tick that deploys the service clears its `k8s_deferred` line
  (`deploy_k8s_owed.clear_applied_k8s_deferred`, called from both k8s deploy paths and from the
  plane-covered set). An operator's own `deploy.sh` clears it with
  `gitops_state.py clear-owed k8s_deferred <svc>`.
- `gitops_state.py clear-owed k8s_unapplied <svc>` is the hand clear for `k8s_unapplied`. It is
  needed for a change that was reverted rather than applied, or for a shared role with a caller
  nothing can prove applied: no tag runs the role, or a caller writes no release record.


### The `has_gitops` gate, the GitHub crons and the marker module: history

`tasks/main.yml` dispatches on `has_gitops`: `install.yml` on the deployer, `teardown.yml`
everywhere else. `group_vars/all.yml` defaults the var **false**, the same safe direction
`has_docker` takes: a host nobody has thought about gets the teardown arm rather than a timer
that deploys to production on its own, and daniel-box's `host_vars` carries the one
`has_gitops: true`. The three non-deployers keep an explicit `has_gitops: false` because the code
gate below reads host_vars TEXT and fails open on an absent key.

The role was gated at the playbook level until 2026-09-09, so a host flipped to false skipped
the role and kept whatever an earlier true run installed. `teardown.yml` removes the six units,
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

**The two GitHub crons** are daily kuma-check timers on the deploy host (`kuma_check_timer.yml`
in the `common` role). Each pushes its own Kuma tile through `kuma-push-lib.sh`, exits 1 on a
down verdict, and `Restart=on-failure` reruns it every 30 minutes until it exits 0. Both
authenticate with the deploy user's `gh auth token`, and without it both are red, because GitHub
shows a ruleset's bypass list only to an admin. The timers are `Persistent=true`, so a slot missed
inside an outage runs at boot. `teardown.yml` imports the same names absent, which reaps the
units on a non-deployer host. One alerts and the other reconciles:

- **`github-ruleset-drift.sh`** reads four ruleset definitions from GitHub and alerts on drift in
  any of them. It never writes: a ruleset changes because a human changed it, and reconciling
  would undo that with no signal.
    - The repo itself must be public. On the free plan GitHub enforces no ruleset on a private
      repo, and each ruleset still reads `enforcement: active`. The repo was private from about
      13:07 to 22:47 UTC on 2026-09-21, and 29 PRs merged before their required check reported
      success. A token read names `.visibility`; an anonymous read of a private repo answers
      `Not Found`. `land.sh --arm-merge` refuses to arm or direct-merge while the repo is not
      public.
    - The master CI gate must require exactly `gitops_deploy_expected_ruleset_contexts`.
    - The branch-protection ruleset (`gitops_deploy_branch_ruleset_id`) must exclude
      `refs/heads/renovate/**`. Without that exclusion GitHub refuses Renovate's post-merge
      branch delete and its rebase force-push, and a merged branch's stale green automerges the
      next PR empty.
    - The review ruleset (`gitops_deploy_review_ruleset_id`) must stay active on master and
      require `gitops_deploy_review_ruleset_approvals` approving reviews. Stale approvals must be
      dismissed on push, and the last push must be approved by someone other than its pusher.
    - The fence ruleset (`gitops_deploy_fence_ruleset_id`) must restrict creating, updating and
      deleting every branch except exactly `gitops_deploy_fence_ruleset_exclude`. Without it, the
      agent's account could push onto a branch an operator session opened, and `land.sh`'s admin
      merge would carry that commit to master.
    - Each bypass list must equal its declaration: none on the CI gate, the admin role and the
      Renovate app on the review ruleset and the fence. The agent's GitHub account is in no list,
      so its PRs reach master only through the lander.
- **`github-interaction-limit.sh`** re-applies `gitops_deploy_interaction_limit` every day.
  GitHub grants an interaction limit for six months at most and lets it lapse silently, so
  re-applying it restores the declared state rather than overriding a decision. The limit keeps
  the public repo closed to anyone but a collaborator. To change or clear it, edit the default;
  `none` clears it with a DELETE on the next run. A missing token, a failed PUT and a stored value
  that differs from the declared one all push DOWN with `UNVERIFIED` or `NOT applied`, never
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
(`gitops_tick.sh`, `deploy-ui.service.j2`, monitor-bridge's hostPath) are pinned to `STATE_DIR`
by the same test.

### Which apply clears a hold

**`hold_sha` clears only when the plane the hold names is applied** (`Hold.cover` /
`Hold.cover_services` in `gitops_hold.py`, which `DeployerState.clear_broad_hold` and
`DeployerState.clear_service_hold` call, deciding through `gitops_hold.broad_hold_cleared_by`).
`gitops_hold.Hold` is the rule's one owner: the deploy UI's Clear calls `Hold.clear` rather than
restating which files a clear removes. The rule is coverage, not equality. An untagged run
applies the whole playbook and covers any tag set held against it. A tagged run covers a held
tag set it is a superset of, and covers an untagged hold not at all. A narrowed deploy-plane
apply holds the entry `ansible/deploy.yml <tags>`, so a narrowed run covering a different service
does not clear it.

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
the next failure's hold then waits on planes nobody owes. A k8s rollback hold records no plane,
and `clear-hold` clears it the same way.

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
is set. `probe.py gitops-state` prints the command with the held SHA filled in, and so does
`gitops_tick.sh` after every tick.

**The cost: a surviving hold parks the Renovate agent** (`agent_logic.decide` returns `run=False`
for any non-empty `hold_sha`). That is intended, because an unapplied plane is not the moment to
land more bumps, and it makes the manual clear load-bearing. It does not park deploys: `skip_hold`
matches only while `origin_head == hold_sha`, so a stale hold stops nothing once origin advances.

### A failed run's error string

`run()` raises a `RuntimeError` carrying the argv, the exit code, then a bounded tail of
**stdout followed by stderr**. Stdout is the half that matters: `ansible-playbook` writes the
failing `TASK` header, the `fatal:` line with its `msg` and the `PLAY RECAP` there, and reserves
stderr for warnings and deprecation notices. The error string was stderr-only until 2026-09-02,
when a broad apply of `ansible/deploy.yml` failed and left a deprecation warning as the only
surviving detail. That arm is forward-only, so the alert told an operator to fix forward with
nothing to fix forward from, and the diagnosis needed a 20-minute `deploy.yml` re-run.

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
than the end of stderr. A plain tail failed within a day: a broad apply that failed one task in
1950 had a recap and twenty timing rows that filled the whole window. stderr, and stdout with no
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
`ExecStopPost` journal marker, `tick skipped (lock contention)`, which
`scripts/deploy_tools/gitops_tick.sh` also reads to exit 3.

Before `SuccessExitStatus=75`, contention failed the play: on 2026-08-20 the recap read
`changed=2 failed=1`, with `Run gitops-deploy once` timing at 180.30 s, the flock wait. That
recap is the empirical proof of the mechanism.

Run the apply unlocked. The deployer takes the lock itself, which serialises it against the
timer. The outer flock converts that serialisation into a silent no-op. The same trap applies to
any role whose tasks re-enter a lock-taking command.

The smoke run is a handler, so it fires only when something changed. A no-op run (`changed=0`)
never invokes the deployer and cannot deadlock however it is wrapped. A missing smoke-run step
therefore shows that nothing needed re-rendering, not that the run was incomplete.

**The ff-merge comes before the playbook.** Ansible renders from the working tree, so a playbook
run on the pre-merge tree copies the OLD files and recaps `changed=0`, which looks like a clean
idempotent run. `broad_remediation()` emits the pair in that order, so the deployer's Discord
alert, `deploy_tags.py` and `land_tags.py` all prescribe the working sequence.
`test_broad_remediation_puts_the_ff_merge_before_the_playbook` pins the order.

### Trap: moving a config source changes which remediation the alert prescribes

`/etc/gitops-deploy/config.env` is rendered only by `initial_setup.yml --tags gitops_deploy`.
`deploy.yml` runs no setup role, so a change that must reach that file is unapplied by a
`deploy.yml` run, while a plain ff-merge clears the divergence and every repo-side check reads
green. `broad_remediation()`'s docstring names this: *"naming deploy.yml there is a silent no-op
that leaves the change unapplied"*.

The remediation the deployer prescribes is decided by the path you edited, not by what the edit
affects. The k8s auto-deploy denylist derives from each role's `k8s_autodeploy` declaration
instead of a CSV in this role's defaults. The value and the rendered `config.env` line are the
same, and the edit site differs:

| edit site | routes to | alert names | re-renders config.env? |
|---|---|---|---|
| `roles/setup/gitops_deploy/defaults/main.yml` (old CSV) | `_BROAD_SETUP_PREFIXES` | `initial_setup.yml --tags <role>` | yes |
| `roles/k8s/<role>/defaults/main.yml` (declaration) | `ChangeSet.k8s` | `deploy.yml --tags <svc>` | **no** |

So denying a role from auto-deploy leaves it auto-deployable on the host, and the alert names
the command that cannot fix it. That is the worst direction for a failure, because it hits a
safety-tightening edit. Before moving any value that lands in a host config file, check which
plane `role_of` puts its new path on and what `broad_remediation()` says for that plane.

**A set difference says what diverged, never why.** The stale-denylist alert once inferred its
remediation from the direction of the difference. `removed` (in config, not denied at origin)
read as "config is ahead, `git push` it". That branch assumed one cause and has two: an operator
who rendered locally before pushing, and a role promoted off the denylist at origin. The second
produces the identical signature while meaning the opposite, so the host would disarm
auto-deploy fleet-wide and page with a `git push` that does nothing. On a pull-based host origin
is the source of truth, so the re-render leads in both directions and the push case is a
secondary check. The direction logic lives in `deploy_phases._promote_k8s_auto_deploys`, which
`tests/test_gitops_deploy_phases.py` covers.

`test_gitops_deploy_subprocess.py` pins `deploy_k8s()`'s argv. `test_gitops_deploy_main_branches.py`
runs the failed-rollout branch against the scripted checkout and reads the rollback argv back. The
argv must carry `restore_sha=origin[:8]` EXACTLY and never `local`, under its own
`K8S_ROLLBACK_TIMEOUT_S` budget rather than the forward `K8S_DEPLOY_TIMEOUT_S`. A prefix check
would be wrong, because the full 40-character `origin` also starts with `"origin"` and matches no
snapshot.

**Accepted: `origin[:8]` is a fixed 8-character slice, and `git rev-parse --short=8` is a
minimum width.** `origin = run(["git", "rev-parse", f"origin/{BRANCH}"])` returns the full
40-character SHA, and `origin[:8]` truncates it to exactly 8 characters. `k8s/volume-snapshot`
names its snapshots from the raw stdout of `git rev-parse --short=8 HEAD`, which is the shortest
UNAMBIGUOUS abbreviation. That is 8 characters unless 8 collides with another object in the
history. `k8s/volume-revert/CLAUDE.md` and `k8s/volume-snapshot/CLAUDE.md` both say the SHA is
used verbatim, because truncating a longer abbreviation back to 8 builds a prefix that no longer
matches. `origin[:8]` does exactly that at this one call site. In the collision case the prefix
stops matching one character short, and `k8s/volume-revert`'s "no snapshot matches this deploy"
assert fires. That failure is loud, happens before the scale-down, and is the same safe mode as
every other unmatched prefix. The probability is negligible at this repo's history size, so the
code stays and this note exists so the inconsistency with the two roles' "never truncate" rule is
not mistaken for an oversight.

`deploy_logic.declares_snapshot_claims()` and `rollback_volume_revert_note()` are pure and unit
tested. They decide the rollback alert's one revert-status line: whether the rollback redeploy
itself failed, and which of the batch's services declare `k8s_autodeploy_snapshot_pvcs` (only
those revert). `deploy_io.read_local_k8s_default()` feeds them with a plain file read against the
working tree after `git reset --hard local`, matching what `roles/k8s/manifests` reads for the
claim list.

### The rollback timeout, derived

The rollback redeploy also reverts each claimed volume to its pre-deploy snapshot
(`k8s/volume-revert`), which is more work than the forward deploy. It gets its own timeout,
`K8S_ROLLBACK_TIMEOUT_S` (`gitops_deploy_k8s_rollback_timeout_s`, 1620), rather than sharing
`K8S_DEPLOY_TIMEOUT_S` (`gitops_deploy_k8s_timeout_s`, 1440).

The budget is sized for the worst single promoted (`k8s_autodeploy: true`), claim-declaring role:

```
ceiling = claims x (volume_snapshot_timeout + 3 x volume_revert_state_timeout + 3 x volume_revert_api_timeout)
          + in-role waits + manifests_rollout_timeout + k8s_rollout_stabilise_seconds
        = claims x 480 + in-role waits + manifests_rollout_timeout + 60
```

A role's own `tasks/` run before `k8s/manifests/tasks/drain.yml`, so anything they wait for adds
to the drain instead of overlapping it. prowlarr waits 300 s for its flaresolverr isolation probe
Job on top of a 780 s rollout, which makes it the worst promoted service at 480 + 300 + 780 + 60
= 1620 s. An inline `rollout status` gate is not an in-role wait, because it waits for the same
rollout the drain waits for.
`ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_timeout_budgets.py::test_k8s_rollback_budget_covers_the_worst_single_promoted_service`
computes the ceiling from role sources, so a rollout-timeout bump or a new promoted
claim-declaring role fails it instead of silently under-sizing the budget.

Three residual risks stay accepted:

- **Two claim-declaring services in one batch stack additively.** One tick can promote up to
  `gitops_deploy_k8s_autodeploy_max_per_tick` (3) services into one `ansible-playbook` run. Only
  the rollout wait is shared across them (`max()`, not `sum()`, in `drain.yml`); each role's
  snapshot and revert phase runs in sequence. The proper fix is one rollback run per service, the
  same fix as the batch-abort note under *The safety arms, in full*.
- **A slow but fully successful run can be cut short.** A failed wait ends the play, so
  failure-driven worst cases from different phases cannot stack. A run that succeeds slowly at
  every phase can still exceed the budget, and the timeout then SIGTERMs a run that was
  succeeding. The raise or cut of the timeout changes only whether that slow success survives.
- **A rollback pays an extra Recreate cycle.** The reset tree makes `manifests_render` register as
  changed, so the config-change rollout restart tears down the pod the apply just started. That
  costs about a minute (`docs/volume-revert-drill-and-sizing.md`) and has no budget line of its
  own.

**The forward attempt and the rollback run in sequence inside one unit activation.** The worst
case is 180 s of flock wait, 1440 s forward and 1620 s rollback, or 3240 s, against
`TimeoutStartSec=70min` (4200 s) in `gitops-deploy.service.j2`. The ceiling keeps its headroom
rather than shrinking, because a re-derivation would have to re-measure the two playbook budgets
it is made of. The broad path (180 + 1800 = 1980 s) is the shorter of the two.

**This unit can hold the tree lock for 3060 s** (1440 + 1620, excluding its own flock wait),
which is longer than the 10-minute tick interval. A concurrent `./scripts/deploy.sh` waits
`LOCK_WAIT=3840`, not the unit's own `-w 180`, so it outlasts the hold and then deploys. It
returns exit 75 only if the lock stays busy past 3840 s. An operator who sees exit 75 in this
window should check whether `gitops-deploy` is in a double timeout before assuming the lock is
stuck.

**Four waiters on the tree lock are pinned as a census.** `deploy.sh`, secret-rotate, docs-refresh
and eval-run each wait 3840 s, derived from the same phase timeouts by `_LOCK_WAITERS` and
`_worst_lock_hold()` in `tests/test_gitops_deploy_timeout_budgets.py`. A per-consumer test covers
only the consumers someone wrote one for, so the census plus
`test_the_lock_waiter_census_is_non_vacuous` makes a new waiter fail instead of passing silently.
Raising `gitops_deploy_k8s_timeout_s` or `gitops_deploy_k8s_rollback_timeout_s` fails those tests
rather than shortening an operator's wait.

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
in `files/deploy_locks.py` says why the two orders cannot deadlock.

**A busy service lock is contention, not a failed deploy.** `deploy_locks` raises
`ServiceLockBusy`, each of the three deploy handlers catches it ahead of its failure arm, and
`deploy_defer.for_contention` logs `service lock <tag> busy for <N>s — deferring to the next
tick`, resets to `local` and returns 0. The tick writes no `hold_sha` and no `hold_plane`, runs
no rollback and sends no page, because nothing was applied. The reset undoes the ff-merge, so
`local..origin` carries the range for the next tick.
`tests/test_gitops_deploy_lock_contention.py` drives all three handlers.

**Every marker the tick wrote about that merge goes back with it**, through
`deploy_defer.unrecord`: the `manual_plane` ledger line this tick appended, the `tags` it widened
on an existing line, and this origin's receipt. `land.sh` reads the receipt to tell a plane the
tick applied from one it merely fast-forwarded past. An earlier tick's receipt is still true, so
it stays. The promoted bumps of a narrowed deploy plane are not annotated on this path, because
the next tick re-applies the plane. No `secrets` alert slot needs taking back, since the secrets
page is sent from the non-contention exits only.

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
itself to every term in `_worst_lock_hold()`, and the four jobs that size their tree-lock waits
from that sum would start giving up and paging for ordinary contention.
`deploy_locks.locked_budget` shares one deadline between the wait and the playbook. The cost is
that a phase queued for most of its budget can have its playbook SIGTERMed early, which reads as
a failed deploy and rolls back.

## The deployer's Python layout

Moved out of the role's `CLAUDE.md` by #2999. Three layers, and which one a function belongs in
is decided by what it touches.

| layer | modules | holds |
|---|---|---|
| decisions (pure) | `deploy_changes`, `deploy_cross_role`, `deploy_git`, `deploy_health`, `deploy_inventory`, `deploy_k8s`, `deploy_remediation` | every branch the tick takes, as functions over plain values |
| what a phase hands the next | `deploy_tick_types` | `TickTarget`, `TickPlan` and `RetryableFetchError`, no behaviour |
| transport | `deploy_io`, `deploy_alerts` | subprocess, when an alert is sent, and the alert queue's own I/O |
| the message bodies | `deploy_alert_text` | one pure function per alert — what each post SAYS (#2600) |
| transport leaves | `gitops_markers`, `gitops_hold`, `deploy_config`, `deploy_state`, `deploy_failtext` | the marker table, its parsers and line rewrites, the hold's owner and its clear rule, the config file, the state directory with every marker's reader and writer, and the text a failed run's alert quotes |
| the seam | `deploy_toolbox` | `DeployTools`, one frozen object holding every boundary the tick crosses |
| the phases | `deploy_phases`, `deploy_handlers`, `deploy_defer`, `deploy_broad_k8s` | `assess` and `plan_tick`; one `handle_*` per terminal branch |
| the k8s changes owed | `deploy_k8s_owed` | the one reader and writer of the `k8s_deferred` and `k8s_unapplied` ledger classes: record, discharge, the tick-start `reconcile` (#3669) |
| the tick | `gitops_deploy` | the config constants, `tick_config()`, `main()` and `entrypoint()` |

- **`main()` sequences, it does not decide.** `assess()` returns a frozen `TickTarget`,
  `plan_tick()` a frozen `TickPlan`, and one `handle_*` owns each terminal branch. No leaf imports
  `gitops_deploy` (ENFORCED by `test_no_leaf_imports_the_entry_module`). The branch order — broad
  before k8s — is load-bearing: the broad plane has to win.
- **Every process boundary is injected, not patched.** `main(tools, config, state)` threads
  one frozen `DeployTools` through every phase; a test builds one from `tests/_deploy_fakes.py`.
  `deploy_io.deploy_k8s` and `deploy_broad` stay outside it because the suite asserts on the argv
  they build, so they take a required `run=tools.run` instead. `tests/conftest.py` patches
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

## The two timeout budgets, and the waiters they move

The rollback redeploy also reverts each claimed volume to its pre-deploy snapshot
(`k8s/volume-revert`), so `K8S_ROLLBACK_TIMEOUT_S` is sized for the worst SINGLE promoted,
claim-declaring service. `test_k8s_rollback_budget_covers_the_worst_single_promoted_service` in
`tests/test_gitops_deploy_timeout_budgets.py` computes it from role sources and fails when it is
under-sized.

- Two claim-declaring services in one batch stack additively, which
  `gitops_deploy_k8s_autodeploy_max_claim_services_per_tick` bounds. A failure aborts the play, so
  failure-driven worst cases cannot stack; the residual is a slow but successful run cut short.
- Forward and rollback run sequentially in one activation: `TimeoutStartSec` in
  `gitops-deploy.service.j2` is `max(broad, k8s + rollback)` plus the flock wait.
- **The FORWARD cap is derived from the worst promoted role** (#2397), and it cannot be raised
  alone: it moves the four waiters below and `TimeoutStartSec` with it, in the same change.
  Under-sized, a cap kill lands MID-DRAIN and routes to `_rollback_k8s`.
- **The lock waiters and the service-lock budget** are under *The rollback timeout, derived*:
  `deploy.sh`, secret-rotate, docs-refresh and eval-run each wait 3840 s, and
  `deploy_locks.locked_budget` spends the phase's own budget on a service-lock wait. An overrun
  never produces a second concurrent run, because the timer coalesces the new start into the
  activation in flight.
