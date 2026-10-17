# D4 — Ordering (`dependsOn`, deferred applies)

Status: DONE

PR D4.0, netpol-baseline drains before it applies: [#4747](https://github.com/DanielH2018/server/pull/4747) (issue #4743).

PR D4.9, the saturated-workers rule: [#5051](https://github.com/DanielH2018/server/pull/5051).
Its step waits on D1.6, D1.6b and M1 and names no D2 PR, so it landed ahead of D2. After the
deploy, Prometheus's `flux` group held 5 rules. `controller_runtime_max_concurrent_reconciles`
read 4 for the `kustomization` controller, and the alert held no series.

PR D4.1, position-only constraints become edges: [#5157](https://github.com/DanielH2018/server/pull/5157).
On its base, netpol-baseline was already the last entry, so it moved no entry, and
`homelab-render cluster` gave the same output at the base and the head. Its landing read
`needs-manual-apply` with 55 services owed. In a window the operator agreed on 2026-10-15,
a full `./scripts/deploy.sh` ran from 23:04:20 UTC with `failed=0`. Afterwards
`k8s_unapplied` read `none`, and no non-Job pod had started since the run began.

PR D4.2, the renderer writes `dependsOn`: [#5177](https://github.com/DanielH2018/server/pull/5177).
It also closed #5154. After its promotion, `469ba1108`, the bento-pdf Kustomization carried
`homelab/ansible-owned-deps: authelia,traefik`. `dependsOn` stayed empty on all three
Kustomizations, and bento-pdf still ran ReplicaSet `865c4b8dd4`.

PR D4.3, the per-file rule and the chained timeout: [#5191](https://github.com/DanielH2018/server/pull/5191).
It also closed #5172. `verify`'s `timeout-minutes` moved to 335 and the `promote` job's to
360, and `land-wait` allows 67 minutes. The promote run on its merge, `43a32d2e6`, concluded
`success` under the chained timeout, and all 25 check runs on that commit passed.

PR D4.4, the cross-plane gate: [#5214](https://github.com/DanielH2018/server/pull/5214). The
operator ruled Open question 9 on 2026-10-16: a `tasks/` change counts only for a tag Ansible
owns at the head. Its replay over the 30 days before then counts two refusals, #1975 and
#2002.

PR D4.5, texbrain moves to Flux: [#5217](https://github.com/DanielH2018/server/pull/5217).
The operator ran the two-writer check at the PR's head merged with master, because
`deploy.sh` refuses a tree behind `origin/master` before it reads `--tags`. It printed
`DEPLOY-VERDICT: flux-owned (--tags texbrain)` and exited 5. The landing read `settled` at
`7106ab425`, texbrain's Kustomization read Ready, `probe.py health texbrain` passed, and the
editor page rendered.

PRs D4.6 to D4.8, the drill: [#5220](https://github.com/DanielH2018/server/pull/5220),
[#5222](https://github.com/DanielH2018/server/pull/5222) and
[#5224](https://github.com/DanielH2018/server/pull/5224). The operator accepted the drill on
2026-10-16 (Open question 8). D4.6 settled as E = `7466d078d`, with texbrain's `dependsOn`
naming bento-pdf and its `lastAppliedOriginRevision` at E. D4.7 merged as D = T =
`1175ddd7e`. Inside bento-pdf's health-check window, the source named T, texbrain's Ready
message read `dependency 'flux-system/bento-pdf' is not ready`, and texbrain stayed at P =
E, which lacks D. No texbrain ReplicaSet carried `FLUX_DRILL`, and T's statuses held
`flux/bento-pdf` = `failure` and no `flux/texbrain`. `promote` set `DEPLOY_HOLD` to
`<T> failed=bento-pdf` and reset `prod` to E, and D4.7's landing read `deploy-failed` as
planned. A D4.8 branch built before the squash merge would have removed only the edge, so
D4.8 was built from master after it, as a revert of D and E. After the operator cleared the
hold and dispatched `promote`, D4.8's landing read `settled` at `551fbf84c`, `DEPLOY_HOLD`
read `none` and `last-good` matched `prod`. The full readings are on #5222.

All six exit criteria hold. The drill is criterion 4. The suites under *Verification* cover
the rest, and at `551fbf84c` they read `5268 passed`: `scripts/renderer/tests`,
`ansible/tests/deploy`, `scripts/dev/tests`, `scripts/deploy_tools/tests`,
`scripts/validate/tests` and the text and roles census rows.

Depends on: [D2](d2-first-leaf.md), with two exceptions. PR D4.0 depends on nothing, and it
must land before D2.7 (Open question 3). PR D4.9 waits only on D1.6, D1.6b and M1, as its step
says. No PR of D3 waits on D4: the publish layout that D3.3 needs is
D2.6b's (Open question 5).

Unblocks: [D5](d5-bulk-move.md) and H1

## Goal and exit criterion

After D4, Flux applies flux-owned roles in an order generated from
`ansible/filter_plugins/toposort.py`. No role template hand-writes `dependsOn`. Every ordering
constraint that rests on list position at `70591c307` becomes an edge, either derived in
toposort or declared as inventory data:

- netpol-baseline after every workload;
- configarr after sonarr and radarr;
- jellyfin and tdarr after dri-device-plugin.

In the Ansible play, a drain keeps netpol-baseline's constraint, not its batch position. The
deferred applies of pihole, headlamp and netpol-baseline become `<tag>-deferred`
Kustomizations, and `promote` verifies each one. A CI gate refuses two kinds of pull request.
The first changes the rendered output of two roles on different planes when one depends on
the other. The second adds an edge in the same commit as a change to the dependent.

**Exit criterion.** Six checks, each under *Verification*:

1. **Generated, never hand-written.** For every flux-owned tag, the `dependsOn` in
   `DIR/flux/<tag>.yaml` equals the flux-owned subset of that tag's `build_k8s_dep_map`
   edges, plus the renderer-rule edges that *Design* lists. A `Census` row finds no
   `dependsOn:` in any role template or shared template.
2. **Order no longer rests on list position.** `DOCUMENTED_ORDERINGS` in
   `ansible/tests/deploy/test_k8s_toposort.py` names the three new constraints and pihole
   before pihole-exporter, and each pair survives that file's adversarial list. Every k8s
   entry sorts before netpol-baseline on an adversarial list. No entry is listed after
   netpol-baseline, and the scaffolder inserts a new entry ahead of it.
3. **netpol-baseline never shares a drain with a workload.** netpol-baseline's tasks drain the
   rollouts queued earlier in their batch before any task that reads live state or applies.
4. **An aborted dependency holds its dependents back.** The live drill is
   **operator-only** (D4.7) and takes two commits:
    - commit E gives texbrain `depends_on: [bento-pdf]`. texbrain's live spec shows the edge,
      and texbrain is Ready at E;
    - commit D breaks bento-pdf's readiness probe and changes texbrain. T is the commit that
      `promote` promotes and then holds, the first field of `DEPLOY_HOLD`'s value
      `<T> failed=<names>` (C2): D itself, or a later master commit that contains D;
    - during bento-pdf's health-check window, the OCIRepository's revision annotation names
      T, and texbrain's Ready message names bento-pdf. texbrain's
      `lastAppliedOriginRevision` names a commit P that does not contain D: E, or a later
      commit that descends from E;
    - no texbrain ReplicaSet ever carries D's change. T's commit statuses hold
      `flux/bento-pdf` = `failure`, the control that the list is not empty, and no
      `flux/texbrain`;
    - `promote` holds, with `DEPLOY_HOLD` reading T and a `failed=` list that names
      bento-pdf;
    - after D4.8, the operator's clear leaves `DEPLOY_HOLD` reading `none`.
5. **Deferred applies are published and verified.** The renderer emits `pihole-deferred`,
   `headlamp-deferred` and `netpol-baseline-deferred` with the edges below, each as the
   second document in its tag's file. The publish census admits a `-deferred` document only
   in the file of a tag that passes `manifests_deferred_files`, and the render fails on a
   deploy tag that ends in `-deferred`. D2.5's `verify` already expects `flux/<name>` for
   every Kustomization document, so it waits on each `-deferred` with no change. D5
   exercises each one live when its role moves.
6. **The cross-plane gate goes red.** It fails a fixture PR whose render changes a flux-owned
   role and an Ansible-owned role it depends on. It also fails a fixture PR that adds an edge
   and changes the dependent's render together. It passes a fixture flip whose flipped
   tag's render moves and gains edges.

## Baseline at `70591c307`

- **How the play orders roles.** `ansible/deploy.yml` builds the map with
  `ansible/filter_plugins/toposort.py:build_k8s_dep_map`, from the full `containers_list`,
  and sorts with `toposort_containers`, a stable sort. Its comment says the sort is a no-op on
  the live inventory, which `ansible/tests/deploy/test_k8s_toposort.py` checks.
- **How the play batches and drains.** `ansible/deploy.yml` includes `tasks/k8s_batch.yml`
  once per slice of `containers_list | batch(k8s_rollout_batch_width)`, and
  `ansible/inventory/group_vars/all.yml:k8s_rollout_batch_width` is 5. `k8s_batch.yml` runs
  every role in the batch, then includes `k8s/manifests` `drain.yml` once. A role's rollout
  therefore completes only at the end of its batch.
- **Three kinds of edge.**
    - A routed entry, one with a `hostname` or a template that renders a `traefik.io`
      object, depends on traefik. `K8S_CRD_EDGE_EXEMPT` exempts crowdsec, which must precede
      traefik.
    - An entry with `use_authelia: true` depends on authelia.
    - An entry's own `depends_on` adds edges. traefik declares `depends_on: [crowdsec]`,
      pihole-exporter declares `[pihole]`, and janitorr declares
      `[media-volume, sonarr, radarr, jellyfin]`.
- **What the toposort tests already pin.** In `ansible/tests/deploy/test_k8s_toposort.py`:
    - `test_shuffled_list_still_sorts_to_respect_every_edge` checks the traefik, authelia and
      crowdsec edges on a seeded shuffle.
    - `test_crowdsec_still_needs_the_crd_edge_exemption` checks that crowdsec still renders a
      Traefik CRD.
    - `DOCUMENTED_ORDERINGS`, read by
      `test_documented_pairwise_ordering_survives_an_adversarial_list`, holds mosquitto
      before zigbee2mqtt, media-volume before its seven mounters, loki-homelab before
      game-stats, and sonarr, radarr and jellyfin before janitorr. The test asserts that each
      pair is a declared `depends_on`, so a derived edge cannot be a row there.
    - No row names pihole before pihole-exporter.
    - `docs/facts.lock` records that test node as an atom for three role `CLAUDE.md`
      sections.
    - `test_derived_order_matches_todays_hand_ordered_list` asserts that sorting the live
      list is a no-op. `toposort_containers` breaks ties by list index, so an entry listed
      after one of its dependents moves, and the test fails.
    - `test_the_derived_traefik_edge_agrees_with_the_render` asserts, for every entry outside
      `OVER_DERIVED_TRAEFIK_EDGE` and `K8S_CRD_EDGE_EXEMPT`, that the entry is in
      `_derived_traefik_edge` exactly when its render holds a Traefik CRD.
      `_derived_traefik_edge` collects every entry whose `build_k8s_dep_map` edges include
      traefik. `OVER_DERIVED_TRAEFIK_EDGE`'s one entry, livesync, is a role whose hostname
      derives an edge that no manifest of its own needs.
- **Five texts state the edge set as the derived edges plus crowdsec's.** Each is true at
  `70591c307` and false after D4.1:
    - the comment above the "Build k8s role dependency map" pre_task in `ansible/deploy.yml`,
      which names "the one edge no template carries (crowdsec before traefik ...)";
    - the `toposort.py:build_k8s_dep_map` docstring, which says "Two of the three real
      ordering constraints are mechanically derivable";
    - the header comment above `containers_list` in
      `ansible/inventory/host_vars/daniel-box.yml`, which names the traefik-CRD and
      authelia-middleware edges and calls list order "only the toposort's tiebreak";
    - section 2 of `.claude/skills/new-k8s-service/SKILL.md`, which says "Where in the list
      doesn't matter" and lists the two derived edges;
    - the docstrings of `scripts/dev/new_k8s_service.py:entry_lines` and
      `new_k8s_service.py:append_entry`, which say that position in the list does not matter.
- **Three constraints rest on list position only.**
    - **netpol-baseline** is the last entry in `containers_list` and has no edge.
      `docs/networkpolicy-slice-answers.md` requires every workload role to finish before
      netpol-baseline applies. An aborted run that broke that order returned 5xx on four
      routes for about 17 minutes
      ([ADR-0012](../../adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md)).
      The netpol-baseline role's `CLAUDE.md` says the same: "workload role first and
      netpol-baseline last".
    - **One append path breaks that position.**
      `scripts/dev/new_k8s_service.py:append_entry` writes a new entry "after the last item
      of `containers_list`", which is netpol-baseline. Its docstring says that "the list's
      order is not the deploy order", which is false for netpol-baseline until D4.1. Any
      role scaffolded before D4.1 therefore lands after netpol-baseline. D1.2's text says to
      "Append the entry `flux` with `platform: k8s` only", but D1.2 landed in `cfe3bfa43`
      with `flux` inserted just before netpol-baseline. On `origin/master` at `09ba629f1`
      (2026-10-12), netpol-baseline is still the last entry, and no entry sits after it.
    - **netpol-baseline's batch** matters as well as its position. Its entry comment says it
      "must NOT sit in traefik's rollout batch", because the drain runs at the end of a batch
      and the role's probe would fail on timing. The task "Fail if any probe target has no
      ready endpoint" reads `netpol_baseline_probe_targets`, which lists nut and scrutiny.
      With 56 k8s entries at `70591c307`, the last batch is netpol-baseline alone, by
      arithmetic only. D1.2's `flux` entry makes 57, so at `09ba629f1` the last batch is
      flux and netpol-baseline, in that order. D2.1 batches `containers_list |
      ansible_owned`, so from D2.7, with bento-pdf out of the Ansible list, netpol-baseline
      is the 56th entry and alone in its batch again. The batch before it is nut,
      nut-exporter, pi-peer-backup, scrutiny and flux. (The k8s names of `daniel-box.yml`'s
      `containers_list` at `09ba629f1`, in order and without bento-pdf, give that split.)
      Any entry added or removed before then moves the boundary again. When nut or scrutiny
      shares netpol-baseline's batch, a roll of either fails netpol-baseline's readiness
      check after its policies are applied. configarr has the same need and includes
      `drain.yml` itself before its reconcile. netpol-baseline does not.
    - **configarr**'s entry comment says it "Must follow sonarr and radarr", because it
      addresses them by bare Service name. The entry has no `depends_on`.
    - **jellyfin and tdarr** request the `devic.es/dri` extended resource
      (`jellyfin_k8s_dri_resource`, `tdarr_k8s_dri_resource`) that dri-device-plugin
      advertises. Neither has an edge to it. dri-device-plugin is simply listed earlier.
- **The survey's diagram reads "netpol-baseline ─▶ traefik-crds ─▶ volume-claim ─▶
  services".** Neither `traefik-crds` nor `volume-claim` is a deploy tag at `70591c307`.
  Claims render inside each role (`ansible/roles/k8s/manifests/CLAUDE.md`). The incident
  record puts netpol-baseline last, and this plan follows the incident record.
- **The deferred applies.** Three roles pass `manifests_deferred_files`:
    - pihole applies `deployment-2.yaml` only after instance 1 is serving. Both instances once
      rolled together and LAN DNS was down for 52 seconds (#2884, the pihole role's
      `CLAUDE.md`).
    - headlamp applies `rbac-observability.yaml` in the `observability` namespace.
    - netpol-baseline applies its three observability policies. The armed apply passes
      `-n homelab`, which refuses a document in another namespace (#3575, #3591). That is
      the only reason this deferral exists.

  R1 renders deferred files into `DIR/cluster/<tag>-deferred/`. D1 adds the flux role as a
  fourth caller: its `sync.yaml.j2` is a `manifests_deferred_files` entry, staged in
  `manifests_deferred_dir_name: flux-sync` (D1 *Design*). C3's list of deferred applies names
  only the first three. No tag name ends in `-deferred` at `70591c307`:
  `grep -c 'name: .*-deferred$'` prints 0 for each file under `ansible/inventory/host_vars/`.
- **netpol-baseline's probe tests its deferred policies.** In
  `ansible/roles/k8s/netpol-baseline/tasks/main.yml`, the role applies its main manifests,
  then the observability policies, then runs its probe Job. The comment on the second apply
  says it is "Applied before the probes, because the slice-3 probe tests these exact
  policies". The `prometheus` and `loki` rows of `netpol_baseline_probe_targets` name
  `networkpolicy-prometheus.yaml.j2` and `networkpolicy-loki.yaml.j2`, which are both
  deferred files. The probe Job is templated by the role's own tasks, outside `k8s/manifests`,
  so R1 does not render it.
- **How a toposort change lands.** `deploy_changes.py:_BROAD_DEPLOY_PREFIXES` lists
  `ansible/filter_plugins/` with the comment "toposort". The `narrow_lib/filters.py`
  docstring says refusing is right "for a filter the play calls (`toposort.py`'s, from
  `deploy.yml`)". Per `docs/gitops-pipeline.md`, a refusal fast-forwards and records a
  `k8s_unapplied` line for each service it may reach. `land.sh` then reports
  `needs-manual-apply`. A change to one k8s role's `tasks/` is different: `land.sh` scopes
  the deploy to the PR's own files and deploys that role's tag (`docs/landing.md`).
- **No template reads `depends_on`, and D2's census row names only `owner`.**
  `narrow_lib/containers.py` holds no `depends_on` at `70591c307`. Six role templates
  mention `depends_on` (`grep -rln depends_on ansible/roles/*/*/templates ansible/templates`),
  each in a YAML comment and none inside a Jinja expression. D2.1 adds the `owner` exclusion
  to `narrow_lib/containers.py` and does not name a set. D2's `Census` row in
  `ansible/tests/repo/test_census_rows_roles.py` checks that no Jinja expression reads
  `owner`, with a red fixture that reads `entry.owner` (D2 *Design*). It checks no other
  field.
- **How Flux checks a dependency**, read through `gh` from kustomize-controller v1.9.6's
  `internal/controller/kustomization_controller.go:checkDependencies`:
    - A dependency must exist, its `observedGeneration` must match its `generation`, and its
      Ready condition must be true.
    - When the dependency reads the same OCIRepository, its `lastAppliedRevision` must equal
      the current revision.
    - Otherwise the dependent's Ready condition is set to `DependencyNotReady`, with the
      error as its message: "dependency 'flux-system/<name>' is not ready", or "... revision
      is not up to date". The dependent emits an `info` event that reads "Dependencies do not
      meet ready condition, retrying in 30s", which names no dependency and no revision. D1's
      baseline shows that notification-controller drops that event for a commit-status
      provider.
    - A `dependsOn` naming a Kustomization that does not exist fails the same way on every
      reconcile, so an edge must never name an Ansible-owned role.
- **Only the origin revision names a commit.** Read through `gh` at the same versions:
    - kustomize-controller v1.9.6 `api/v1/kustomization_types.go` defines
      `LastAppliedRevision` as the revision of the applied artifact. For the OCI source that
      is a tag and a digest, `prod@sha256:<digest>`, as D1's baseline shows.
    - The same file defines `LastAppliedOriginRevision`, which for OCI is the value of the
      `org.opencontainers.image.revision` annotation. C2's push writes `master@sha1:<sha>`
      there. `kustomization_controller.go:getOriginRevision` reads it from the artifact's
      metadata, and the apply sets both fields together.
    - source-controller v1.9.6 `ocirepository_controller.go` copies the manifest's
      annotations into the OCIRepository's `status.artifact.metadata`. So the commit a
      source has fetched is that annotation, and its `status.artifact.revision` is the tag
      and digest.
    - `checkDependencies` compares a dependency's `lastAppliedRevision` with the source's
      artifact revision. Both are in tag-and-digest form, so the `checkDependencies` bullet
      above names the right field. Every reading in this plan that names a commit uses the
      origin field.
- **What D2 publishes and verifies.** D2 owns the bundle layout and the expected statuses,
  from D2.5 and D2.6b (D2 *Publication and verification*):
    - D2.6b's `promote.py check-bundle` census requires `out/flux/` to hold
      `kustomization.yaml` plus one `<name>.yaml` per flux-owned tag and per member of
      `SHARED_KINDS`, a closed literal set that is empty at D2. It computes that allowlist
      from the renderer's inputs. Its reserved-name check refuses a deploy tag named
      `homelab` or a member of `SHARED_KINDS`.
    - D2.5's `verify` expects `flux/homelab` plus `flux/<metadata.name>` for every document
      of apiVersion `kustomize.toolkit.fluxcd.io/v1` and kind `Kustomization` under the
      bundle's `flux/`. kustomize's own `flux/kustomization.yaml` adds no status.
    - D2's census checks files, not the documents inside a tag's file. D2 leaves "the
      per-file `-deferred` rule" to D4. D3.3a adds `cluster-vars` to `SHARED_KINDS`, and
      D5.1a1 adds its own kinds.

  So `verify` already waits on a `-deferred` document in its tag's file, and D3.3 needs no
  PR of D4. D4 adds only the rule for which documents a tag's file may hold, and the
  `-deferred` reserved suffix.
- **A dependent reconciles a new revision with the spec it already holds.**
    - `kustomization_manager.go` watches the OCIRepository. On a revision change,
      `kustomization_indexers.go:requestsForRevisionChangeOf` enqueues every Kustomization
      on that source.
    - `main.go` runs 4 reconciles at once by default (`--concurrent`).
    - A reconcile reads its own object first and reads its dependencies later, through the
      API reader.
    - So the root `homelab` Kustomization (path `./flux`, `wait: false` in D1) and an
      existing dependent reconcile revision R at the same time. If the root has not yet
      written the dependent's new spec, the dependent applies R under its old `dependsOn`. A
      Kustomization that the root creates at R is born with its edges.
- **Each hop costs up to 30 seconds.** `main.go` declares `--requeue-dependency` with a
  30-second default. A `DependencyNotReady` reconcile requeues after that interval, and no
  watch re-enqueues a dependent when its dependency turns Ready.
- **A health check holds a worker.** `checkHealth` calls `WaitForSetWithContext` with the
  Kustomization's timeout inside the reconcile. A Kustomization that waits on health holds one
  of the `--concurrent` workers until it passes or times out. Every Kustomization on the
  source reconciles each revision, so an unchanged one that is unhealthy holds a worker too.
- **Role rollout budgets differ.**
  `ansible/roles/k8s/manifests/defaults/main.yml:manifests_rollout_timeout_default` is 600s.
  Five roles pass a larger `manifests_rollout_timeout`:
    - sonarr and radarr 660s (`sonarr_k8s_rollout_timeout`, `radarr_k8s_rollout_timeout`);
    - prowlarr 780s (`prowlarr_k8s_rollout_timeout`);
    - valheim 1800s (`valheim_k8s_rollout_timeout`);
    - tdarr 3000s (`ansible/roles/k8s/tdarr/defaults/main.yml:tdarr_k8s_rollout_timeout`).
      tdarr's `CLAUDE.md` gives the reason: a cold pull of the 1432 MiB image took 25m37s on
      2026-10-08. A pre-pull task in its `tasks/main.yml` shortens the downtime, and the
      3000s budget stays as its backstop. tdarr has a hostname and `use_authelia: true`.
- **A GitHub-hosted job stops at 6 hours.** Every job in `.github/workflows/ci.yml` runs on a
  GitHub-hosted `ubuntu-26.04` runner, and no slice gives `promote` a self-hosted one.
  GitHub's Actions limits reference says: "Each job in a workflow can run for up to 6 hours
  of execution time", and "If a job reaches this limit, the job is terminated and fails."
- **Master moves often.** 1276 commits landed on master between 2026-09-11 and 2026-10-11
  (`git log --since=2026-09-11 --until=2026-10-11 --oneline | wc -l`). Another merge between
  a drill commit and the `promote` run that follows it is likely, and `decide` then promotes
  a later commit (C2).
- **Shared macros reach both planes.** bento-pdf's render reads eight shared templates under
  `ansible/templates/`: `workload-shell.yml.j2`, `container-resources.yml.j2`,
  `security-context.yml.j2`, `pod-affinity.yml.j2`, `ingressroute.yml.j2`, `service.yml.j2`,
  `ingressroute-default.yaml.j2` and `service-default.yaml.j2`. authelia's and traefik's
  Deployments import `workload-shell.yml.j2` and `container-resources.yml.j2` too. 48 k8s
  roles import the first, and 53 import the second. In the 30 days to 2026-10-11, 23 commits
  touched the eight files (`git log --since=2026-09-11 --until=2026-10-11 --oneline --` over
  the eight paths, piped to `wc -l`). The count includes any commit that moves no render,
  such as a comment edit, so 23 is an upper bound on the commits that move both renders.

## Design

### Every constraint becomes an edge

The edges live in toposort and the inventory, so the Ansible play and the renderer read one
map:

- **`K8S_APPLIES_LAST`** joins `ansible/filter_plugins/toposort.py`, a dict of role to
  written reason, like `K8S_CRD_EDGE_EXEMPT`. Its one entry is `netpol-baseline`, with the
  reason from the slice answers. `build_k8s_dep_map` gives an entry in this dict an edge to
  every other k8s entry.
- **Inventory data**:
    - configarr gains `depends_on: [sonarr, radarr]`;
    - jellyfin's `depends_on` becomes `[media-volume, dri-device-plugin]`;
    - tdarr's becomes the same.
- **`depends_on` joins the deploy-mechanics fields**, because no template reads it. D4.1
  turns D2.1's `owner` exclusion in `narrow_lib/containers.py` into one named set,
  `DEPLOY_MECHANICS_FIELDS`, holding `owner` and `depends_on`, unless D2.1 has already named
  one. D2's `Census` row then reads that set and checks every member, never a second list.
  `depends_on` is a named member the row must find in the set, and a template that reads
  `entry.depends_on` is the row's second red fixture.
- **The hand-ordered list stays a topological order.** D4.1 moves every entry listed after
  netpol-baseline ahead of it. At `09ba629f1` there is none, because D1.2 inserted `flux`
  before netpol-baseline, but a role scaffolded before D4.1 lands there. Without the move,
  `toposort_containers` would reorder the live list and
  `test_derived_order_matches_todays_hand_ordered_list` would fail. The same test would fail
  after every later scaffold, so `append_entry` inserts a new entry before the first
  `K8S_APPLIES_LAST` entry, which it imports from `toposort.py` with its own `sys.path`
  bootstrap (`.claude/rules/python-layout.md`).
- **The derived-traefik test skips `K8S_APPLIES_LAST`.** `_derived_traefik_edge` leaves out
  the roles in `K8S_APPLIES_LAST`, with the reason in a comment: their traefik edge is
  unconditional, so it says nothing about their templates and can never be under-derived.
  Without the skip, netpol-baseline reads derived and not rendered, and
  `test_the_derived_traefik_edge_agrees_with_the_render` fails. netpol-baseline does not join
  `OVER_DERIVED_TRAEFIK_EDGE`, whose entries are roles whose own route derives an edge.

Once D4.1 has moved the entries listed after netpol-baseline, every new edge already holds on
the live list. The play's order then changes only for the moved entries, which run before
netpol-baseline instead of after it.

### netpol-baseline drains before it applies

An edge orders roles, but it does not wait for a rollout. The Ansible play stays
netpol-baseline's writer until D5 moves it, so netpol-baseline needs its own drain:

- **The change.** netpol-baseline's `tasks/main.yml` gains configarr's include of
  `k8s/manifests` with `tasks_from: drain.yml` as its first task. Every rollout queued earlier
  in the batch then completes before netpol-baseline reads the exempt sets, applies its
  policies or runs its probe. For those roles this holds at any batch position.
- **What the drain does not cover.** It waits only for roles that run earlier in the batch.
  A role listed after netpol-baseline, such as one scaffolded before D4.1, runs after the
  policies apply. D4.1's edge is what moves such a role ahead, so the drain alone does not
  protect it. D1.2's `flux` is not such a role: it sits just before netpol-baseline, in the
  same last batch at `09ba629f1`. flux queues no rollout (`manifests_rollout: ''`) and waits
  on each controller inside its own role, so its controllers have rolled before
  netpol-baseline starts.
- **A tagged run.** In a run of `--tags netpol-baseline` alone the queue is empty, and the
  drain does nothing.
- **Cost.** The drain moves the batch's wait earlier but does not lengthen the batch. The role
  queues no rollout of its own (`manifests_rollout: ""`), so the drain at the end of the batch
  has nothing left to wait for.
- **The budget tests do not move.** `_role_tasks.py:_include_target` follows `include_tasks`
  and `import_tasks` only, and the drain is an `include_role`. `in_role_wait_s` therefore
  reads the same for netpol-baseline, and the netpol-baseline rows of
  `test_gitops_deploy_timeout_budgets.py` keep their values.
- **Rejected:** running the `K8S_APPLIES_LAST` entries as a final batch of their own in
  `deploy.yml`. That works too, but it edits the play that every deploy runs. It also ties
  netpol-baseline's safety to how the batch loop is written, which D2.1 changes.

### An edge governs from the revision after it is applied

This rule follows from the baseline above. A dependent that changes in the same revision as
its new edge may apply that revision before the edge exists. Three consequences:

- **A PR must not add an edge and change the dependent's render in the same commit.** The
  gate's second arm enforces this rule.
- **A flip is safe.** A role that moves to Flux is created with its edges. Its flux-owned
  dependents gain an edge to it in the flip revision, and a flip PR changes no dependent's
  render.
- **The drill lands its edge first** (D4.6), then breaks the dependency (D4.7).

Rejected: `dependsOn: [{name: homelab}]` on every Kustomization. The dependent reads its own
spec before it reads its dependencies, so the root can finish in between, and the stale spec
still applies. The root edge narrows that window without closing it, and it adds a 30-second
hop to every chain.

### The renderer writes `dependsOn`

- **For a flux-owned tag T**, `dependsOn` lists the flux-owned members of
  `build_k8s_dep_map(...)[T]` in sorted order, plus the renderer-rule edges below. An edge to
  an Ansible-owned role is dropped, because it would name a missing Kustomization.
- **`K8S_APPLIES_LAST` in the renderer.** A tag in that dict also depends on every flux-owned
  `<tag>-deferred` Kustomization except its own. In Ansible, pihole applies instance 2 inside
  its own role, before netpol-baseline runs, and this edge keeps that order. D5's `soak`
  carries no `dependsOn` edges (D5 Open question 19), so the tag has no edge to it either.
  Whether the tag also waits on another tag's `<tag>-snapshot` or `<tag>-verify` is D5.1a1's
  to decide, when those kinds exist.
- **Dropped edges stay visible.** They are listed in the annotation
  `homelab/ansible-owned-deps` for a reader. Each one becomes a real `dependsOn` in the
  first render after that dependency moves to Flux.
- **A deferred tag** with `manifests_deferred_files` gets a second Kustomization. It is the
  second YAML document in `DIR/flux/<tag>.yaml`, so C1's one file per tag holds:

  ```yaml
  metadata: {name: pihole-deferred, namespace: flux-system}
  spec:
    path: ./cluster/pihole-deferred
    dependsOn: [{name: pihole}]
    # same sourceRef, interval, timeout, prune, wait and deletionPolicy as its tag
  ```

  The edges come from data, never from a hand list. A deferred document in a namespace
  that another role creates adds an edge to that role. The renderer reads each document's
  `metadata.namespace` and maps `k8s_observability_namespace` to `observability`. That gives
  `headlamp-deferred` the edges `[headlamp, observability]` and `netpol-baseline-deferred`
  the edges `[netpol-baseline, observability]`.
- **The `-deferred` rule keys on `manifests_deferred_files`, not on a list of roles.** At
  `70591c307` pihole, headlamp and netpol-baseline pass it, and D1 adds the flux role as a
  fourth caller. The flux role stays Ansible-owned through D4, so no `flux-deferred` is
  rendered. If a later slice moves it to Flux, the same rule gives it one. C3's list of
  deferred applies names only the first three (Open question 7).
- **The reserved suffix.** The render fails on a deploy tag that ends in `-deferred`, so no
  tag's Kustomization or status can collide with another tag's deferred one. This joins
  D2's reserved names, `homelab` and the members of `SHARED_KINDS` (C3's *Reserved names*).
  It lands in D4.2, the first PR that emits a `-deferred` name.
- **A verifying Job is D5's, in `<tag>-verify`.** C3 puts a verifying Job in `<tag>-verify`,
  which depends on the tag's last Kustomization: `<tag>-deferred` when the tag has one,
  `<tag>` otherwise. D5.1a1 renders that edge and tests it on a fixture tag with a deferred
  file, and C1 has the `-verify` output exist only from D5.1a1. D4 renders no Job and no
  `-verify` kind. netpol-baseline's probe Job tests policies in both its main and its
  deferred directory, which is why its edge must point at `netpol-baseline-deferred`. D5's
  netpol-baseline PR adds the Job as `netpol-baseline-verify` and names it as a member.
- **Some generated edges do not come from toposort.** The edges to `cluster-vars` (D3), to
  `<tag>-snapshot`, `<tag>-verify`, `homelab-namespace` and `soak` (D5), the `-deferred`
  edges above, and the namespace edges of a `-deferred` Kustomization come from the
  renderer's own rules. C3's *Ordering* row allows an edge from a renderer rule that the
  adding slice names, and this list names D4's.
- **Either order with D3.** D3 writes the `cluster-vars` edge, and D4 writes the toposort
  edges. Whichever slice lands second merges its edges into the writer that the other added.
  Neither slice's `dependsOn` PR assumes the other has landed. The publish layout that
  `cluster-vars` needs is D2.6b's, so D3.3 waits on no PR of D4.

### What `promote` reads

- **The expected statuses and the shared-kind layout are D2's.** D2.5's `verify` derives the
  expected set from Kustomization names, and D2.6b holds the allowlist census,
  `SHARED_KINDS` and the reserved names (*Baseline*). A `-deferred` document is therefore
  waited on with no change to `verify`. D4 adds two rules, in D4.2 and D4.3, and the chained
  timeout.
- **Each tag's file** holds exactly the Kustomization `<tag>`, plus `<tag>-deferred` only
  when the tag passes `manifests_deferred_files` (D4.3). `check-bundle` computes that from
  the renderer's inputs, as D2.6b's census does for the files, so a renderer bug cannot add
  a document. A shared kind's file holds exactly its own Kustomization (D3.3a). D5.1a1
  extends the rule to `<tag>-snapshot` and `<tag>-verify`.
- **The reserved `-deferred` suffix** joins D2's reserved names (D4.2, under *The renderer
  writes `dependsOn`*).
- **`DEPLOY_HOLD` has no empty value.** D1 creates the variable as `none`, because `gh`
  cannot set an empty value, and `decide` reads `none` as no hold. Any other non-empty
  value is a hold. An empty or missing value is an error, never "no hold". This plan writes
  "held" for a value other than `none`. It writes "clear" for setting `none` with
  `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`, which is
  **operator-only**. C2 and C6 state the same rule.
- **The promote timeout follows the chain** (D4.3). D2 derived the timeout from the largest
  single Kustomization timeout. With edges, a dependent starts only after its dependencies
  finish. So `promote.py verify` takes the longest `dependsOn` path in T's bundle that ends
  at a name in T's changed set (C2). A name in the changed set counts its own `timeout`:
  C3's per-role field where a role sets it, and the bundle default otherwise. An unchanged
  ancestor on the path counts no `timeout`. `dependsOn` still waits for it to reconcile T,
  but its apply changes nothing, and a healthy one passes its health check without waiting.
  It adds:
    - one `requeue-dependency` interval per edge on that path;
    - a queueing term: the number of names in the changed set, dependents included, divided
      by `--concurrent`, rounded up, times the largest `timeout` among them;
    - the OCIRepository interval;
    - the Kustomization `retryInterval`.

  The walk covers every Kustomization in the bundle, so D3's `cluster-vars` and D5's
  `<tag>-snapshot` and `<tag>-verify` count whenever they sit on the path, with no change to
  the rule. D5's `soak` carries no edges (D5 Open question 19), so D5.3b2 takes it out of both
  terms: its `timeout` is a floor beside them, and it holds a worker of its own.
  `requeue-dependency` and `--concurrent` are read from the kustomize-controller args in the
  flux role's vendored components when they set them, and are otherwise the v1.9.6 defaults:
  30 seconds and 4. D5.1 sets `--concurrent` to `k8s_rollout_batch_width`, and D5.3b2 adds
  the soak's worker.
- **Which Kustomizations count as changed.** The timeout reads C2's changed set, the set
  whose statuses `verify` expects (D2.5), and derives no second one. A name counts when its
  `footprint` digest in T's `digests.json` differs from the one on `prod`'s commit before
  the move (C1). Every Kustomization whose `dependsOn` chain reaches such a name counts
  too, and a suspended one does not. A change under `_unclassified` counts every name. The
  timeout and the expected statuses therefore cannot disagree.
- **Why the queueing term is in, as D5 asked.** A Kustomization that waits on health holds a
  worker (*Baseline*), so a revision that changes more Kustomizations than `--concurrent`
  queues behind the workers. Without the term, `verify` would time out on such a revision
  and hold. Clearing a hold is **operator-only** (C2), so every false hold costs an operator
  action. The term lengthens the ceiling instead. The ceiling binds only when a status never
  arrives, because `verify` returns on the first `failure` and on the last `success`.
- **What the ceiling cannot see.** The revision change enqueues every Kustomization on the
  source, changed or not (*Baseline*). An unchanged Kustomization that is unhealthy holds a
  worker for its whole `timeout`, retries after its `retryInterval`, and holds a worker
  again. The bundle does not show which Kustomizations are unhealthy, so the ceiling leaves
  them out. When `--concurrent` of them fail at once, for example after a node loss, they
  hold every worker. Every apply then queues, the fix included, and `verify` times out on a
  changed name that never applied. The rule under *Saturated reconcile workers* reports
  that state, D4.7 and D5 measure it, and Open question 13 holds the remedy.
- **The cap.** GitHub stops a job on a GitHub-hosted runner after 6 hours (*Baseline*).
  D2.5 runs `verify` as its own job, with no concurrency group, and `settle` runs on
  `always()`. A `verify` job that its `timeout-minutes` or the 6-hour limit stops therefore
  leaves no verdict, and `settle` writes `<T> unverified` (C2 *On failure*). That hold names
  no Kustomization and the revert rule never passes it, so `promote.py`'s own derived
  timeout must fire first and write `<T> failed=<names>`. `verify` caps its derived timeout
  at 5.5 hours for that reason, and at the cap `settle` writes the hold like any other
  timeout.
    - **Three limits move with the derived timeout.**
      `ansible/tests/repo/test_promote_workflow_verify.py` holds them against each other:
      `verify`'s `timeout-minutes` must exceed the derived timeout, `decide`'s in-flight wait
      (`scripts/deploy_tools/promote_lib/verify_promotion.py:IN_FLIGHT_WAIT_S`) must cover
      `verify`'s and `settle`'s `timeout-minutes`, and the `promote` job's `timeout-minutes`
      must exceed that wait. D2.5 sets them at 25 minutes, 40 minutes and 50 minutes, sized
      for bento-pdf's 13-minute timeout. D4.3 raises all three from the largest timeout it
      can derive.
    - **The 6-hour limit binds the `promote` job, not `verify`.** A later run's `decide`
      waits on a verification in flight inside its own `promote` job. So `verify`'s and
      `settle`'s `timeout-minutes`, plus ten minutes of runner queueing, plus any wait a
      later slice adds before `decide`, must fit inside 6 hours. D5's wait for the
      weekly-reboot window is one, and D5 ends it as `deferred` without waiting. At the
      5.5-hour cap that leaves about 15 minutes.
    - **A wait shorter than the verification is safe and costs a promotion.** When the
      wait ends first, `decide` gives up as `current` and moves nothing. The commit waits
      for the next run, and `PromotionStalled` pages once a stall passes 2 hours.
- **The size.** Each figure is before the OCIRepository interval and the retry.
    - At the end of D4 the bundle holds bento-pdf and texbrain, with no edge between them,
      because D4.8 reverts D4.6. A change to bento-pdf expects bento-pdf alone: one
      600-second default plus one queueing round of 600 seconds, about 20 minutes. While
      D4.6's edge stands, texbrain joins the changed set as a dependent. The path is then
      two 600-second defaults and one hop, plus one round, about 31 minutes. An unchanged
      `cluster-vars` at the head of the path adds one hop, and a changed one adds its own
      `timeout` too.
    - On the full map, every changed set holds netpol-baseline and netpol-baseline-deferred,
      because `K8S_APPLIES_LAST` makes netpol-baseline depend on every tag. A one-role
      change at the 600-second default therefore expects three Kustomizations on one path:
      30 minutes of timeouts, at least two hops, and one queueing round of 10 minutes,
      about 41 minutes.
    - tdarr keeps its 3000-second budget as its own `timeout` unless D5 replaces its
      pre-pull task, because Flux runs no such task and a cold pull took 25m37s. A change
      to tdarr alone is the worst case within `--concurrent`: 70 minutes of timeouts, 2.5
      minutes of hops from crowdsec to netpol-baseline-deferred, and one round of 50
      minutes, about 2 hours.
    - The longest chain is crowdsec ─▶ traefik ─▶ authelia ─▶ tdarr ─▶ netpol-baseline ─▶
      netpol-baseline-deferred. When the changed set holds all of it, five 600-second
      defaults and tdarr's 3000 seconds sum to 100 minutes, and five hops add 2.5 minutes.
      The chain through sonarr and janitorr is shorter, about 74 minutes.
    - The changed set shrinks the expected set for a narrow revision, not for a broad one.
      A revision that changes every importer of `workload-shell.yml.j2`, 48 roles, expects
      those 48 plus netpol-baseline's two Kustomizations. At `--concurrent` 5 that adds ten
      rounds of up to 3000 seconds, so it reaches the 5.5-hour cap. Open question 14 asks
      whether `verify` should hold one runner that long.
    - D5's `<tag>-snapshot` and `<tag>-verify` Kustomizations lengthen a path where they sit
      on it. `soak` sits on none, and its `timeout` is a floor of the wait.

  `land-wait` gives up at twice the promote timeout (C6): about 82 minutes for a one-role
  change, about 4 hours for a tdarr change, and 11 hours at the cap. `settle` writes the hold
  when `verify` reaches its own timeout, and `land-wait` ends on a `DEPLOY_HOLD` other than
  `none`, so `land-wait`'s ceiling binds only when the workflow run itself never finishes.

### Saturated reconcile workers

C7 gives D4 the rule for saturated reconcile workers (P2).

- **The rule.** D4.9 adds `FluxWorkersSaturated` to D1.6b's `flux` rule group, in M1's rules
  directory. It fires when kustomize-controller's active workers equal its maximum
  concurrent reconciles and its work queue is not empty, for 30 minutes.
  controller-runtime exports an active-workers gauge and a maximum-concurrent-reconciles
  gauge per controller, and D1.6's scrape of the controllers' `http-prom` port collects
  them. The rule reads the maximum from its gauge, never a literal, because `--concurrent`
  is 4 until D5.1 changes it. The D4.9 PR body quotes both series names and the
  `controller` label value from a live scrape.
- **Labels.** `service="flux"` and `severity="warning"`. A broad revision fills the workers
  for hours while it progresses, so the rule reports that applies are queueing, not that a
  deploy failed. The soak reads only `severity="critical"` and `verify="true"` alerts (C7),
  so this rule never fails a promotion's soak.
- **Series.** The rule reads series that D1.6's scrape already collects and adds no label.
  Its delta against M1's series budget is zero.
- **The measurement.** The 30-minute window is a starting value. D4.7's drill holds one
  worker on bento-pdf's failing health check, which gives the first live reading of worker
  occupancy. D5 repeats the reading on its first Wave D revisions, and the PR that tunes
  the window quotes both readings.
- **The shed frees workers within one timeout.** D5.1b's shed steps in `docs/break-glass.md`
  suspend each flux-owned tag on a down node by hand. A suspend is an API write and needs no
  worker. Nothing in this plan shows that a suspend ends a health-check wait already in
  progress, so the bound is one `timeout`: each held worker frees when its current wait
  ends. The suspended tag then does not retry.
- **tdarr's 50 minutes.** tdarr's 3000-second `timeout` is the longest that one unhealthy
  Kustomization holds a worker. D5 owns tdarr's move and its `timeout` (*The size*), so D4
  adds no PR for it. A cross-slice note asks D5 to replace the cold pull with an image
  pre-pull that Flux applies, so that tdarr can take the 600-second default.

### The cross-plane gate

Until D5 ends, an edge between a flux-owned role and an Ansible-owned role has no ordering
mechanism. `land.sh` applies the Ansible half after the merge, while `promote` applies the
Flux half after CI. Usually that costs a hold and a retry. In the policies-first case it costs
5xx responses.

`scripts/validate/cross_plane_order.py --base <sha>` closes that window for one PR:

- **What counts as changed.** The gate renders the cluster at the base and at the head with
  `homelab-render cluster`. A tag counts when its `cluster/<tag>/` output differs, unless its
  role's only diff is an image pin, judged as `deploy_k8s.py:is_image_only_diff` judges it.
  A role whose `tasks/` changed also counts when Ansible owns it at the head, because a task
  change moves no render. A flux-owned role's tasks run nowhere, so only the render they feed
  counts (Open question 9). A PR none of whose paths reaches an Ansible apply counts
  its flux-owned roles alone. Ansible applies its own templates, never `homelab-render` output,
  so a renderer change with its docs and tests moves nothing Ansible applies (#5213).
  `cross_plane_order.py:reaches_ansible` decides which paths reach an apply. A role that reads another entry's fields counts exactly when its
  own render moved. For example,
  `auth_tier` on bento-pdf is read by authelia's access rules
  (`narrow_lib/containers.py:FILTER_FIELDS`). An `auth_tier` edit moves authelia's render
  alone, so no pair forms.
- **Arm 1, plane order.** The gate fails when two counted roles sit on different planes and
  one is in the other's transitive dependency set, `K8S_APPLIES_LAST` included. The message
  names the order to land them in. It also says that the second PR merges only after the
  first PR's `land.sh` verdict reads `settled`.
- **Arm 2, edge first.** For each tag that is flux-owned at both the base and the head, the
  gate compares the `dependsOn` rendered at the base and at the head. It fails when such a
  tag gains an edge and its own `cluster/<tag>/` output also moved. The message says to land
  the edge alone first. A tag that becomes flux-owned in the PR is not judged: the root
  creates its Kustomization at the head, born with its edges (*An edge governs from the
  revision after it is applied*). Its render moves in the same commit, because D3's
  renderer stamps `checksum/restart` only on a flux-owned tag's restart targets, so judging
  it would refuse every flip.
- **A shared-template change exits by copy and migrate.** A change to a macro under
  `ansible/templates/` moves the render of every role that imports it, on both planes. Arm 1
  refuses it whenever a flux-owned importer depends on an Ansible-owned one, and bento-pdf
  and authelia both import `workload-shell.yml.j2`. The standard exit is a split:
    1. Add the changed macro under a new name, and move the dependency's plane to it. Land
       that PR until `settled`.
    2. Move the dependent's plane to the new macro.
    3. Delete the old macro, which moves no render.

  When a dependency set crosses planes more than once, step 1 repeats once per plane, in
  dependency order. That is about three PRs per such macro change until D5 ends. *Baseline*
  counts 23 commits to bento-pdf's eight shared templates in 30 days, an upper bound on how
  often the cost falls due. Before D4.4 merges, the session replays the gate over the PRs
  merged in the previous 30 days, with D4.4's ownership, and quotes the number it would have
  refused (Open question 9).
- **An edit that one inventory field carries exits by handback.** Take a `hostname` edit on
  bento-pdf. It moves bento-pdf's route and authelia's access rules, and authelia is
  Ansible-owned. No macro copy splits it. The exit is D2's handback: hand bento-pdf back to
  Ansible, make the change while the play orders both, then flip bento-pdf again. While
  bento-pdf is handed back, its flux-owned dependents lose their edge to it. The recipe
  therefore suits a role with no flux-owned dependents, or with dependents that tolerate the
  gap. The second flip rolls the workload once more, because a takeover rolls it (D2
  *Risks*).
- **No bypass.** The gate offers no bypass trailer, because a bypass would become the default
  route.
- **What it does not see.** Two PRs that merge back to back each pass alone. The gate's
  message and `docs/landing.md` state the `settled` rule for them. The gate also passes an
  image bump that does need ordering.

CI runs it in the `hooks` job for pull requests only, with the PR's base SHA, because a full
sweep has no diff to judge. The step's condition must stay compatible with
`ansible/tests/repo/test_ci_dispatch_sweeps_everything.py`, which guards the scoping
conditions of dispatch runs.

### Tree diff

```diff
  ansible/roles/k8s/netpol-baseline/tasks/main.yml + drain include first (D4.0)
  ansible/filter_plugins/toposort.py               + K8S_APPLIES_LAST, edges in build_k8s_dep_map
  ansible/inventory/host_vars/daniel-box.yml       netpol-baseline comment (D4.0);
                                                   configarr, jellyfin, tdarr depends_on,
                                                   any entry after netpol-baseline moves ahead,
                                                   header comment names K8S_APPLIES_LAST (D4.1);
                                                   texbrain owner: flux (D4.5);
                                                   texbrain depends_on (D4.6 to D4.8)
  ansible/deploy.yml                               dependency-map comment (D4.1)
  ansible/roles/k8s/configarr/tasks/main.yml       comment names the second drain
  ansible/tests/deploy/test_k8s_toposort.py        five DOCUMENTED_ORDERINGS rows, K8S_APPLIES_LAST test,
                                                   _derived_traefik_edge skips K8S_APPLIES_LAST
  ansible/tests/repo/test_census_rows_roles.py     D2's row reads DEPLOY_MECHANICS_FIELDS (D4.1)
  scripts/deploy_tools/narrow_lib/containers.py    + DEPLOY_MECHANICS_FIELDS (owner, depends_on)
  scripts/dev/new_k8s_service.py                   append_entry inserts before K8S_APPLIES_LAST
  .claude/skills/new-k8s-service/SKILL.md          section 2 names K8S_APPLIES_LAST (D4.1)
  scripts/renderer/                                dependsOn, <tag>-deferred, ansible-owned-deps,
                                                   reserved -deferred suffix (D4.2)
  scripts/deploy_tools/promote.py                  per-file document rule, chained timeout with
                                                   the queueing term and the cap (D4.3)
+ scripts/validate/cross_plane_order.py
  .github/workflows/ci.yml                         a PR-only step in the hooks job
```

## Steps, as PRs

1. **D4.0 — netpol-baseline drains before it applies** (about 40 lines). No dependency, and
   it lands before D2.7.
    - Add the drain include as the first task of netpol-baseline's `tasks/main.yml`.
      Rewrite the inventory comment above netpol-baseline's entry and the role's
      `CLAUDE.md` to say that the drain keeps it off a shared drain, not its batch position.
    - Test in `ansible/tests/deploy/`: netpol-baseline's first task includes `k8s/manifests`
      with `tasks_from: drain.yml`, ahead of every task that applies or reads live state.
      netpol-baseline is a named member. The red half is a fixture role whose drain comes
      after its apply.
    - **Landing.** This is a `tasks/` change to one k8s role, so `land.sh` deploys
      `--tags netpol-baseline` on its fast path. The tagged run's queue is empty, the drain
      does nothing, and the probe runs as before. The expected verdict is `settled`.
2. **D4.1 — Position-only constraints become edges** (about 210 lines, with the comments
   and docs).
    - Add `K8S_APPLIES_LAST` and the inventory `depends_on` lines.
    - Re-derive which k8s entries sit after netpol-baseline in `containers_list` on the
      branch's base, and move each ahead of it. At `09ba629f1` there were none: D1.2 put
      `flux` just before netpol-baseline. Any role scaffolded since then by `append_entry`
      sits after it. If the list is clean, the step moves nothing and the PR body says so.
    - Add `DEPLOY_MECHANICS_FIELDS` to `narrow_lib/containers.py`, holding `owner` and
      `depends_on`, and have the narrowing drop every member. Widen D2's `owner` row in
      `ansible/tests/repo/test_census_rows_roles.py` to read that set: no Jinja expression
      reads any member. `depends_on` is a named member the row must find in the set, and a
      template that reads `entry.depends_on` is a second red fixture beside D2's
      `entry.owner`.
    - Change `new_k8s_service.py:append_entry` to insert before the first `K8S_APPLIES_LAST`
      entry. Its test in `scripts/dev/tests/`, beside
      `test_the_entry_lands_inside_containers_list`, gives a fixture list that ends in
      netpol-baseline and expects the new entry just ahead of it. The test fails on today's
      `append_entry`, which puts the entry last.
    - In `test_k8s_toposort.py`, `_derived_traefik_edge` skips the `K8S_APPLIES_LAST` roles,
      with the reason from *Design* in a comment.
      `test_derived_order_matches_todays_hand_ordered_list` is the red proof for the list
      move: an entry left after netpol-baseline fails it.
    - In `test_k8s_toposort.py`, extend `DOCUMENTED_ORDERINGS` with five declared pairs:
      (sonarr, configarr), (radarr, configarr), (dri-device-plugin, jellyfin),
      (dri-device-plugin, tdarr) and (pihole, pihole-exporter).
    - In the same file, add one parametrized test. For every k8s entry X, an adversarial list
      with netpol-baseline first and X last sorts X ahead of netpol-baseline. netpol-baseline
      is a named member of `K8S_APPLIES_LAST`. The red half empties `K8S_APPLIES_LAST` and
      expects the reversed list to fail.
    - crowdsec stays exempt from the traefik edge through `K8S_CRD_EDGE_EXEMPT`, because it
      must precede traefik. `test_shuffled_list_still_sorts_to_respect_every_edge` already
      checks both directions, so no new row repeats them.
    - D4.0's test widens to every `K8S_APPLIES_LAST` role.
    - configarr's comment "THE ONE CONSUMER-SIDE BARRIER IN THE k8s PLAY" names
      netpol-baseline's drain as the second. This edit waits for D4.1, which already reaches
      every service, so that D4.0's landing stays one tag.
    - Until G2 retires `docs/facts.lock`, and only if `fact_status.py` reports the pairwise
      test's atom as moved, run
      `uv run python scripts/dev/fact_status.py verify '<doc>#<heading>'` for each of the
      three sections, and commit the lock. Once G2 has retired it, this step does not run.
    - **Landing.** `toposort.py` is play-called, so the narrowing refuses and the tick
      records a `k8s_unapplied` line for every k8s service. `land.sh` reports
      `needs-manual-apply`. The edges move no render. The list move can reorder the output
      of a template that loops over every entry, so the PR body quotes a diff of
      `homelab-render cluster` at the base and at the head and names each role whose render
      moved. The lines of the
      other digest-provable roles discharge by render proof within about an hour. The
      session discharges the rest, the moved roles among them, with
      `./scripts/deploy.sh` for the tags those lines name, or with one full run. Either way it
      runs in an agreed window, when `probe.py landing` shows no `hold_sha`, `DEPLOY_HOLD`
      reading `none` and no deploy in flight.
3. **D4.2 — The renderer writes `dependsOn`** (about 170 lines).
    - Add `dependsOn`, the `K8S_APPLIES_LAST` rule for `-deferred` and `soak`, the
      `<tag>-deferred` document and its namespace edges, the `homelab/ansible-owned-deps`
      annotation, and the reserved `-deferred` suffix. No Job placement: C3 puts a verifying
      Job in `<tag>-verify`, and D5.1a1 renders that edge.
    - Tests in `scripts/renderer/tests/`:
        - an edge to an Ansible-owned role is dropped and annotated;
        - an edge between two flux-owned roles is kept;
        - a deferred tag gets its second document in the tag's own file;
        - a fixture applies-last tag depends on a fixture `<tag>-deferred` but not on its own;
        - a cycle in the map fails the render, with a fixture that holds a `soak`-shaped
          Kustomization and the applies-last tag's own `-deferred`;
        - a fixture tag named `fixture-deferred` fails the render, and a plain fixture tag
          passes.
    - A `Census` row in `ansible/tests/repo/test_census_rows_text.py`:
        - **Selector:** the tracked files under `ansible/roles/*/*/templates/` and
          `ansible/templates/`. No k8s role applies a static manifest from `files/` at
          `70591c307`, so templates are the only place a hand-written edge would take effect
          outside the renderer. The row's reason says so.
        - **Out of selector, with reasons:** the vendored `gotk-components.yaml` from D1,
          whose CRD schema names `dependsOn`, and `docs/`, whose pages show examples.
        - **Named members:** `ansible/templates/networkpolicy.yml.j2`,
          `ansible/roles/k8s/netpol-baseline/templates/networkpolicy.yaml.j2`, and the flux
          role's root Kustomization template from D1.
        - **Red fixture:** a role template that holds a `dependsOn:` line.
    - **Live effect.** At this point every toposort edge of a flux-owned tag names an
      Ansible-owned role, so no live `dependsOn` changes. The annotation is the only new
      field. A `cluster-vars` edge from D3, if D3 has landed, stays as it is.
4. **D4.3 — The per-file rule and the chained timeout** (about 150 lines). It depends on
   D2.5, D2.6b and D4.2. D2.5's name-based `verify` and D2.6b's census, `SHARED_KINDS` and
   reserved names stay D2's, and this PR changes none of them.
    - Add the per-file document rule to `check-bundle`, and the chained timeout, with the
      queueing term and the 5.5-hour cap, to `verify`. The timeout reads C2's changed set,
      the same set `verify` expects.
    - Raise `verify`'s `timeout-minutes`, `IN_FLIGHT_WAIT_S` and the `promote` job's
      `timeout-minutes` together, from the capped timeout (*The cap*). Without that, a
      tdarr change derives about 50 minutes, D2.5's 25-minute `timeout-minutes` stops
      `verify` first, and `settle` writes a false `<T> unverified` hold.
    - Tests in `scripts/deploy_tools/tests/`, each a pair:
        - a tag file that holds `<tag>` and `<tag>-deferred`, for a fixture tag with
          `manifests_deferred_files`, passes the census. A file that holds a third document,
          or a `-deferred` for a tag with none, fails it;
        - a fixture bundle whose tag file holds both documents makes `verify` expect
          `flux/<tag>` and `flux/<tag>-deferred`. A missing `-deferred` status keeps it
          waiting, and a `failure` holds. This is the red proof that D2.5's derivation reads
          the second document in a file;
        - the timeout of a three-Kustomization fixture chain is three timeouts, two
          `requeue-dependency` intervals, one queueing round, the interval and the retry. A
          chain with a fixture shared kind at its head counts the shared kind's timeout;
        - eleven changed Kustomizations under `--concurrent` 5 add three rounds of the largest
          changed timeout. A Kustomization outside the changed set adds nothing to the
          queueing term, and a dependent of a changed name adds one name to it;
        - an unchanged ancestor on the path adds one `requeue-dependency` interval and no
          `timeout`, and a changed one adds its `timeout`;
        - a fixture whose derived timeout exceeds 5.5 hours gets 5.5 hours, and at the cap
          `verify` prints `failed`, which `settle` writes as the hold. The workflow test's
          three-limit check fails a `verify` `timeout-minutes` below the cap.
    - **Live effect.** The census admits what it did before, because no flux-owned tag has
      a `-deferred` until D5. The promote timeout grows by one queueing round.
5. **D4.4 — The cross-plane gate** (about 200 lines).
    - Add `cross_plane_order.py` and its CI step. Its tests in `scripts/validate/tests/` are
      `_is_clean` and `_is_flagged` pairs, as
      `scripts/validate/tests/test_every_validator_has_a_red_proof.py` requires:
        - flagged: a template change in bento-pdf and authelia together;
        - flagged: an inventory edit whose render moves both bento-pdf and authelia, such
          as `hostname`;
        - clean: an `auth_tier` edit on bento-pdf, which moves authelia's render alone;
        - clean: an image-pin bump in both roles, and a change to bento-pdf alone;
        - flagged by arm 2: `depends_on: [bento-pdf]` on texbrain together with a texbrain
          template change; clean: the edge alone;
        - clean: a flip of texbrain whose render moves by the `checksum/restart` stamp and
          which gains edges, because arm 2 judges only a tag that is flux-owned at both ends;
        - flagged by arm 1: a change to `workload-shell.yml.j2` that moves bento-pdf's and
          authelia's renders; clean: step 1 of its copy-and-migrate split, which moves only
          the Ansible-owned importers.
    - **Before merge**, the session replays the gate over every PR merged in the 30 days
      before, at each PR's base and head, with D4.4's ownership. The PR body quotes the
      command, the number of PRs refused and the number refused for a shared template, and
      `docs/landing.md` states the measured cost (Open question 9).
6. **D4.5 — texbrain moves to Flux** (about 20 lines).
    - texbrain has the same shape as bento-pdf: one task, `RollingUpdate`, no Secret, no PVC.
    - Run D2.7's preconditions and two-writer check, adapted to texbrain.
7. **D4.6 — The drill's edge** (one line). Add `depends_on: [bento-pdf]` to texbrain's entry.
    - `depends_on` reaches no template reader, so the Ansible side has nothing to apply.
      `land.sh` waits on promotion, and the expected verdict is `settled`.
    - **Precondition**, quoted in the PR body: D2's `promote.py land-wait` reads
      `status.lastAppliedOriginRevision`. A `land-wait` that reads `lastAppliedRevision` gets
      `prod@sha256:<digest>`, which `merge-base --is-ancestor` cannot take, so it never
      reads `settled` and ends `gave-up`.
    - Before D4.7 merges, quote two readings in the D4.7 PR body: texbrain's
      `.spec.dependsOn` names bento-pdf, and its `lastAppliedOriginRevision` is
      `master@sha1:<E>`, or a later commit that descends from E.
8. **D4.7 — The drill** (about 10 lines, **operator-only**). A production drill under C8,
   because it merges a known-bad commit to production on purpose. Two changes:
    - bento-pdf's readiness path pointed at `/flux-drill-not-found`;
    - an env var `FLUX_DRILL: "d4"` in texbrain's Deployment.

   No edge changes, so the gate's second arm passes. The expected `land.sh` verdict is
   `deploy-failed` with cause `promote-held`, and the PR body says so. **Preconditions**,
   each checked and quoted in the PR body:
    - the operator's acceptance of this drill, quoted (Open question 8). D2 Open question
      1's acceptance covers D2.8 only;
    - D4.6's two readings, and D4.6's `land-wait` precondition;
    - `probe.py landing` shows no tick in flight, no `hold_sha` and `DEPLOY_HOLD` reading
      `none`. An empty or missing `DEPLOY_HOLD` fails this precondition, because `decide`
      treats it as an error;
    - no other slice's drill is in flight, such as D2.8 or a D3 drill, because every drill
      writes the one `DEPLOY_HOLD`;
    - no Renovate PR for `bento_pdf_k8s_image` or `texbrain_k8s_image` is open;
    - the revert (D4.8) is prepared as a branch before the merge;
    - the session posts in the deploy channel before merging.

   **Blast radius**, stated in the PR body as C8 requires:
    - *Objects it touches:* bento-pdf's Deployment, which gains a ReplicaSet that never
      becomes Ready; texbrain's Deployment, which Flux must leave alone; and the `bento-pdf`
      and `texbrain` Kustomizations. While `DEPLOY_HOLD` holds T, every flux-owned change
      waits: bento-pdf's and texbrain's, plus `cluster-vars` once D3.3 has landed and peanut
      once D3.6 has.
    - *What keeps serving:* both old ReplicaSets. bento-pdf's rollout is `RollingUpdate`
      with a 600-second `progressDeadlineSeconds` (D2 *Rollback*), so its old pod stays
      available. texbrain's Deployment never rolls.
    - *Rollback:* D4.8, then the operator clears `DEPLOY_HOLD` and dispatches `promote`,
      with the two commands D4.8 names (**operator-only**).

   **Why C8's other substitutes do not suffice.** A kind or k3d cluster in CI can show that
   kustomize-controller holds a dependent back, but that is upstream behaviour, and it has
   no `prod` tag, no master commit to post statuses on and no `DEPLOY_HOLD`. The drill
   proves D4's own chain: the renderer's `dependsOn` in a published bundle, applied by the root, the
   statuses notification-controller posts on T, and `promote`'s hold and reset. A fixture tag
   in a scratch namespace on production would fail into the same `DEPLOY_HOLD`, so it would
   have the same blast radius and add a tag to retire.
9. **D4.8 — Revert the drill.** One PR reverts D4.7 and D4.6. Removing an edge in the same
   commit as a change errs on the safe side: a dependent that still holds the old edge waits
   one more hop. After the revert merges, the operator clears the hold and dispatches
   `promote`, by D2's procedure (**operator-only**):

   ```bash
   gh variable set DEPLOY_HOLD --body none -R DanielH2018/server
   gh workflow run promote.yml -R DanielH2018/server --ref master -f sha=<master tip>
   ```

   Run outside a checkout, `gh workflow run` without `-R` fails with `failed to run git:
   fatal: not a git repository`, as it did on 2026-10-16.

   `decide` then promotes the newest eligible commit, which contains the revert.
   `probe.py landing` shows `DEPLOY_HOLD` reading `none` (*Verification*).
10. **D4.9 — The saturated-workers rule** (about 40 lines with its tests). Waits on D1.6,
    which scrapes the controllers, on D1.6b, whose `flux` rule group it joins, and on M1,
    which owns the rules directory and its promtool run. It needs no other D4 PR.
    - Add `FluxWorkersSaturated` to the `flux` rule group, as *Saturated reconcile workers*
      says, with its promtool test beside the rule.
    - The PR body quotes the two gauge names and the `controller` label value from a live
      scrape of kustomize-controller, and states the series delta of zero.
    - **Live effect.** None until the workers fill. The rule fires as a warning only.

## Verification

```bash
# Exit 1: what the renderer wrote for each flux-owned tag, and the census.
# Add --role peanut once D3.6 has landed.
uv run python scripts/renderer/homelab_render.py cluster --out /tmp/d4 \
  --role bento-pdf --role texbrain
grep -A4 dependsOn /tmp/d4/flux/*.yaml
uv run pytest scripts/renderer/tests ansible/tests/repo/test_census_rows_text.py

# Exits 2 and 3, with the scaffolder and the mechanics-field census.
uv run pytest ansible/tests/deploy scripts/dev/tests \
  ansible/tests/repo/test_census_rows_roles.py

# Exit 4, after D4.6 merges as commit E. Every commit reading uses the origin field: the
# plain lastAppliedRevision and artifact.revision print prod@sha256:<digest>, no commit.
kubectl -n flux-system get kustomization texbrain \
  -o jsonpath='{.spec.dependsOn} {.status.lastAppliedOriginRevision}'
  # bento-pdf; master@sha1:<E>, or a later commit that descends from E

# Exit 4, after D4.7 merges as commit D. Take these readings inside bento-pdf's
# health-check window, before promote resets prod: about 10 minutes from the source
# fetching the new revision. The annotation names T, the commit promote moved prod to.
kubectl -n flux-system get ocirepository homelab \
  -o jsonpath='{.status.artifact.metadata.org\.opencontainers\.image\.revision}'
  # master@sha1:<T>
kubectl -n flux-system get kustomization texbrain -o \
  jsonpath='{.status.conditions[?(@.type=="Ready")].message} {.status.lastAppliedOriginRevision}'
  # names flux-system/bento-pdf; master@sha1:<P>, where P is not T
git merge-base --is-ancestor <D> <P>; echo "contains=$?"           # contains=1: P lacks D

# Exit 4, before or after the reset. These records survive it.
uv run python scripts/diagnostics/probe.py landing                 # DEPLOY_HOLD = <T> failed=..., naming bento-pdf
git merge-base --is-ancestor <D> <T>; echo "contains=$?"           # contains=0
kubectl -n homelab get rs -l app=texbrain -o jsonpath='{..env}' | grep -c FLUX_DRILL   # 0
kubectl -n homelab get rs -l app=bento-pdf -o jsonpath='{..readinessProbe}' \
  | grep -c flux-drill-not-found                                   # 1, the control
gh api repos/DanielH2018/server/commits/<T>/statuses \
  --jq '[.[] | select(.context | startswith("flux/")) | .context + "=" + .state] | unique'
  # holds flux/bento-pdf=failure, the control; no flux/texbrain entry

# Exit 4, supporting evidence only: the event names no dependency and no revision.
kubectl -n flux-system get events --field-selector involvedObject.name=texbrain \
  | grep -c 'Dependencies do not meet ready condition'

# Exit 4's way out, after D4.8 merges and the operator clears the hold.
uv run python scripts/diagnostics/probe.py landing                 # DEPLOY_HOLD = none

# Exit 5.
uv run pytest scripts/renderer/tests scripts/deploy_tools/tests

# Exit 6.
uv run pytest scripts/validate/tests
```

**Each new check can go red.**

- D4.0's drain test fails on a fixture role that drains after its apply, and it names
  netpol-baseline as a member, so a renamed role fails it rather than passing vacuously
  (failure class 4).
- The new `DOCUMENTED_ORDERINGS` rows go red through the existing test when a `depends_on`
  line is dropped. The `K8S_APPLIES_LAST` test's red half empties the dict.
- The `_derived_traefik_edge` skip removes only the `K8S_APPLIES_LAST` roles from the
  comparison, so the test still goes red for every other role. Its comment names why those
  roles cannot be under-derived.
- `test_derived_order_matches_todays_hand_ordered_list` fails on an entry left after
  netpol-baseline. The scaffolder's new test fails on today's `append_entry`.
- D2's widened census row fails on a template that reads `entry.depends_on`, and it names
  `depends_on` as a member it must find in `DEPLOY_MECHANICS_FIELDS`, so a set that loses
  the field fails the row rather than passing vacuously (failure class 4).
- The `dependsOn` census fails on its red fixture and names its members.
- The cycle test is the renderer's red proof, and the reserved-suffix test fails a fixture
  tag that ends in `-deferred`.
- Each census and timeout change in D4.3 is a pair. A missing `-deferred` status must never
  read as success (failure class 3), and the fixture that holds two documents in one file
  proves that `verify` reads the second one. The census fails a third document and a
  `-deferred` for a tag without deferred files.
- The gate's flagged fixtures, one per arm. The clean flip fixture proves that arm 2 does not
  refuse every flip.
- The drill is the live red proof that `dependsOn` holds a dependent back at the old
  revision. A reading of texbrain's Kustomization taken after `promote` resets `prod` proves
  nothing, because the reset returns texbrain to the old revision anyway. So the
  verification takes the Ready message during the health-check window. That message names
  bento-pdf, and `lastAppliedOriginRevision` names a commit P that does not contain D, while
  the OCIRepository's revision annotation already names T. `DEPLOY_HOLD` later names the same
  T, and T contains D. Every reading is keyed on T, not on D: another merge between D and the
  `promote` run makes T a later commit, and then no status is ever posted on D. A check of
  D's statuses would then pass on an empty list (failure class 3). The plain
  `lastAppliedRevision` prints a tag and a digest, which a reader cannot match to a commit
  without a registry lookup. Three records outlive the reset:
    - no ReplicaSet in texbrain's history carries `FLUX_DRILL`;
    - bento-pdf's newest ReplicaSet does carry the broken probe path. That reading is the
      control: without it, an empty texbrain reading could mean the drill never ran;
    - T's statuses hold `flux/bento-pdf` = `failure` and no `flux/texbrain`. The failure is
      the control that the status list is not empty.

  The events grep is supporting evidence only. Its message names no dependency and no
  revision. Once D3's `cluster-vars` edge exists, texbrain can emit it for that edge on any
  revision (failure class 2).
- D4.9's promtool test is the rule's red proof. A fixture at full occupancy with a non-empty
  queue for 30 minutes must fire. A fixture one worker below the maximum, and one with an
  empty queue, must not. pint's `promql/series` check fails the rule when Prometheus holds
  no series under the quoted names, so a renamed metric reads as red rather than as quiet
  (failure class 3).
- The occupancy reading during D4.7 is the live proof that a failing health check holds a
  worker. While bento-pdf's health check waits, kustomize-controller's active-workers gauge
  reads at least one. Its peak over the drill is D4's first occupancy measurement, which
  the D4.7 PR body quotes beside the Ready message.

## Rollback

Each bound below says how long a PR can be reverted alone.

- **D4.0.** Revert it only while no role that queues a rollout shares netpol-baseline's
  batch. At `09ba629f1` that batch holds flux, which queues none, and after D2.7 it holds
  netpol-baseline alone. Any entry added or removed can move a rollout-queuing role into
  it. From then on the drain is what keeps netpol-baseline off a shared drain, so it stays
  even when D4.1 is reverted.
- **D4.1.** Revert the edges and the inventory lines, and keep D4.0's drain. The revert
  lands as `needs-manual-apply`, as D4.1 did, and discharges the same way. The revert is safe
  alone while netpol-baseline, configarr, jellyfin and tdarr are all Ansible-owned, because
  the list order still satisfies their constraints there. Once any of them is flux-owned, the
  revert travels with that role's handback. The revert may keep the moved entries, which stay
  a valid order without the edges. If it also reverts `append_entry`, new scaffolds land
  after netpol-baseline again, as at `70591c307`.
- **D4.2.** Revert it alone only while every toposort edge of every flux-owned tag names an
  Ansible-owned role. From D4.6 on, and through D5, a revert removes live edges. It then
  travels with the handback of each flux-owned role that has a flux-owned dependency. After
  D5 moves netpol-baseline, a lone revert would bring back the policies-first state that
  ADR-0012 records. If D3 landed after D4 and extended D4.2's writer, the revert would also
  drop D3's `cluster-vars` edge, so it keeps that edge.
- **D4.3.** Revert it alone until D4.6. After that, the timeout falls back to D2's largest
  single timeout, which a chain can outlast, so a slow but healthy chain holds falsely. From
  D5's first flux-owned deferred role on, the census would also stop refusing a stray
  document in a tag's file. `verify` keeps waiting on every `-deferred` status either way,
  because the name-based derivation is D2.5's.
- **D4.4.** Revert it. The window it guards reopens.
- **D4.5.** The handback from D2.
- **D4.6 and D4.7.** D4.8 reverts both.
- **D4.9.** Revert it. No check or runbook reads the rule's alert.
- **What a partial failure leaves.**
    - An edge does not govern the revision that adds it, as *Design* explains. A PR that
      added an edge and changed the dependent together would apply the dependent once
      without waiting. The gate's second arm refuses that PR.
    - A flip that creates a dependency in the same revision as its dependents' new edge lets
      each dependent read "not found" once and requeue, or apply that revision under its old
      spec. A flip PR changes no dependent's render, so the dependent applies nothing new out
      of order.
    - If D4.7's revert cannot land, `DEPLOY_HOLD` keeps T and every flux-owned change waits.
      At this point that means bento-pdf and texbrain, plus `cluster-vars` once D3.3 has
      landed and peanut once D3.6 has. Each keeps serving what Flux last applied.

## Risks

- **An edge treated as live in the revision that adds it** (failure class 5, a sequence
  treated as a transaction). The root writes the spec and the dependent reconciles at the
  same time, so a new edge can miss its own revision. Mitigation: the rule in *Design*, the
  gate's second arm, and the drill landing its edge one commit before the break.
- **A safety property held by batch arithmetic.** netpol-baseline stayed off a shared drain
  only because 56 divides into batches that leave it alone. D1.2 broke that: at 57 entries
  flux shares its batch, which is harmless only because flux queues no rollout. D2.7 leaves
  it alone again, and every later entry added or removed moves the boundary.
  Mitigation: D4.0's drain and its test, for every role earlier in netpol-baseline's batch at
  any position, and D4.1's edge for a role listed after it.
- **A position assumption in prose and in the scaffolder** (failure class 8, prose that
  nothing re-derives). Five texts and the scaffolder say that list position does not matter,
  and `append_entry` puts every new entry after netpol-baseline. Mitigation: D4.1 changes
  the scaffolder and all five texts, and
  `test_derived_order_matches_todays_hand_ordered_list` fails on any entry left after
  netpol-baseline.
- **A cycle.** `K8S_APPLIES_LAST` makes netpol-baseline depend on everything, so any edge into
  netpol-baseline makes a cycle. That includes an edge to its own `-deferred` or to `soak`.
  `toposort_containers` raises `AnsibleFilterError`, and the renderer must refuse too.
  Mitigation: D4.2's cycle test with both fixtures, and the toposort tests running on every
  PR.
- **One failing workload blocks policy changes.** netpol-baseline waits for every workload at
  the same revision. That is the purpose of the edge. The cost is that a policy fix waits for
  an unrelated failure to be fixed or held.
- **The gate is a heuristic.** Two PRs that merge back to back each pass alone, and an image
  bump that does need ordering passes it. The handback exit drops a role's edges while it is
  handed back. Mitigation: the gate's message and `docs/landing.md` state the `settled` rule,
  and `docs/flux-deploy-plane.md` says what the gate does not see.
- **The gate refuses shared-template changes often.** A macro that both planes import moves
  both renders, and *Baseline* counts up to 23 such commits in 30 days. Each refused change
  becomes about three PRs until D5 ends. Mitigation: copy and migrate is the documented exit,
  and the replay before D4.4 merges measures the real count (Open question 9).
- **The promote timeout grows with the chain and the queue.** On the full map it is about
  41 minutes for a one-role change, up to about 2 hours for a revision within
  `--concurrent`, and capped at 5.5 hours. `land-wait` waits twice that. `verify` runs in no
  concurrency group (C2 *A verification in flight blocks the next promotion*). The next
  master commit's `decide` waits on it inside its own `promote` job, and that job holds the
  group `promote`. A broad revision can hold one runner up to the cap and a second one for
  the wait, and no other promotion runs meanwhile (Open question 14).
  GitHub evicts a queued run beyond the first (D1's baseline), and the next run promotes the
  newest descendant, so no commit is skipped. Mitigation: `verify` returns on the first
  `failure` status and on the last `success`, so a ceiling binds only when a status never
  arrives. The cap keeps `verify` inside the 6-hour job limit, so a stuck verify still writes
  the hold.
- **Unhealthy Kustomizations starve the workers** (failure class 4, a selector narrower than
  the hazard). The queueing term counts the changed set, but every Kustomization on the
  source reconciles each revision. `--concurrent` unhealthy ones hold every worker, and
  tdarr's holds one for 50 minutes. Every apply queues behind them, the fix included.
  Mitigation: `FluxWorkersSaturated` reports the state (D4.9), D5.1b's shed frees each
  worker within one `timeout`, and Open question 13 holds the remedy, triggered by the
  measured saturation.
- **A drill reading taken too late** (failure class 2). See the red-proof note above. The
  Ready message must be read inside bento-pdf's health-check window.
- **A reading of the wrong revision field** (failure class 2). `lastAppliedRevision` and
  the OCIRepository's `artifact.revision` hold the tag and digest, so a reading or a
  `land-wait` that expects a commit there never matches. Mitigation: every commit reading in
  this plan uses `lastAppliedOriginRevision` or the revision annotation, and D4.6 quotes
  that D2's `land-wait` does too.
- **A document in a tag's file that the census does not know** (failure class 4, a selector
  narrower than the hazard). D2.6b's census checks files, not the documents inside a tag's
  file. Mitigation: D4.3's per-file rule, computed from the renderer's inputs, admits
  `<tag>-deferred` only for a tag with deferred files. D5.1a1 extends the rule to its own
  per-tag kinds with a fixture.
- **A drill reading keyed on the wrong commit** (failure class 3, empty read as clean).
  `promote` may promote a later commit than D, and then nothing is posted on D. Mitigation:
  every drill reading is keyed on T, the first field of `DEPLOY_HOLD`'s value, and `flux/bento-pdf` =
  `failure` on T is the control that the status list is not empty.
- **The drill puts a known-bad commit on master** (failure class 5). Mitigation: the drill is
  **operator-only**, with the operator's acceptance and the blast radius in its PR body,
  D4.7's preconditions, D2.8's prepared revert and deploy-channel post, and the rule that
  only one drill holds `DEPLOY_HOLD` at a time.
- **Positional order survives in the Pi's play.** `deploy.yml`'s Docker play still runs in list
  order for daniel-pi. That is out of scope here, and H5 removes it.

## Tests and docs that change in the same PR

- D4.0: the drain test in `ansible/tests/deploy/`. The inventory comment above
  netpol-baseline's entry and the netpol-baseline role's `CLAUDE.md` name the drain.
- D4.1, tests:
    - the five `DOCUMENTED_ORDERINGS` rows, the `K8S_APPLIES_LAST` test and the
      `_derived_traefik_edge` skip in `ansible/tests/deploy/test_k8s_toposort.py`;
    - D2's `owner` row in `ansible/tests/repo/test_census_rows_roles.py`, widened to
      `DEPLOY_MECHANICS_FIELDS`, with the `entry.depends_on` red fixture;
    - the `append_entry` test in `scripts/dev/tests/`.
- D4.1, docs and comments. Each names `K8S_APPLIES_LAST` and the new declared `depends_on`
  edges:
    - the comment above "Build k8s role dependency map" in `ansible/deploy.yml`;
    - the `toposort.py:build_k8s_dep_map` docstring, which also stops counting "three"
      constraints;
    - the header comment above `containers_list` in `daniel-box.yml`;
    - section 2 of `.claude/skills/new-k8s-service/SKILL.md`, which says that
      netpol-baseline is ordered after every entry and that the scaffolder inserts ahead of
      it;
    - the `entry_lines` and `append_entry` docstrings in `scripts/dev/new_k8s_service.py`;
    - the netpol-baseline role's `CLAUDE.md`, which names `K8S_APPLIES_LAST` in place of
      "netpol-baseline last", and one line in `docs/networkpolicy-slice-answers.md` pointing
      at it;
    - configarr's tasks comment, which names the second drain.

  Until G2 retires `docs/facts.lock`, the lock changes too, if the pairwise test's atom
  moved.
- D4.2: tests in `scripts/renderer/tests/` and the `Census` row.
  `docs/flux-deploy-plane.md` gains the ordering section, including the edge rule and the
  reserved `-deferred` suffix.
- D4.3: tests in `scripts/deploy_tools/tests/`. `docs/flux-deploy-plane.md` gains the
  per-file rule, the timeout derivation with its queueing term and cap, and its size.
- D4.4: tests in `scripts/validate/tests/`. `docs/landing.md` says what the gate refuses, how
  to split the PR, the copy-and-migrate exit with its measured cost, the `settled` rule, and
  the handback exit.
- D4.5: `ansible/roles/k8s/texbrain/CLAUDE.md`.
- D4.9: the promtool test beside the rule. `docs/flux-deploy-plane.md` gains the rule, the
  gap the ceiling cannot see, and the shed bound.

## Complexity delta

- **Added:**
    - one dict and its edge rule in toposort;
    - one drain include in netpol-baseline;
    - three `depends_on` lines and five test rows;
    - one named set of deploy-mechanics fields, and a scaffolder that inserts before
      `K8S_APPLIES_LAST`;
    - about 170 renderer lines for `dependsOn` and the reserved suffix, and about 150 lines
      in D4.3 for the per-file rule and the chained timeout with its queueing term and cap,
      read over C2's changed set;
    - one alert rule with its promtool test, about 40 lines in D4.9, with a series delta of
      zero;
    - a 200-line gate that renders the cluster twice on every PR until D6 deletes it;
    - the tests above.
- **Removed:** positional ordering and batch arithmetic as load-bearing facts. That removal is
  real, because a reorder of `containers_list` or a change in batch width stops being a
  hazard.
- **Cost that grows:**
    - the promote timeout scales with chain depth and with the size of the changed set:
      about 41 minutes for a one-role change on the full map, up to about 2 hours for a
      revision within `--concurrent`, and the 5.5-hour cap for a broad revision;
    - until D5 ends, a shared-template change that reaches both planes costs about three PRs
      instead of one, and a one-field inventory edit costs a handback and a second flip. The
      replay before D4.4 merges puts a number on how often.

## Open questions

1. **Should the gate stay after D5?** Once every role is flux-owned, no PR spans two planes.
   Resolution: D6 deletes it as a check whose subject is gone, per
   `.claude/rules/python-layout.md`. The second arm's subject, an edge added beside a change,
   outlives D5. D6 decides whether to keep that arm as a renderer check.
2. **Is netpol-baseline's dependency on every workload too wide?** It could depend only on
   the workloads whose pods its policies select. Resolution: measure the fan-in's reconcile
   cost when netpol-baseline moves in D5. Narrow the rule only with a census of selected pods
   and a test that names its members.
3. **D4.0 lands before D2.7, though the slice index puts D4 after D2.** D4.0 depends on
   nothing. This plan first placed the hazard it closes at D2.7. On the order at `09ba629f1`,
   D2.7 leaves netpol-baseline alone in its batch, so the hazard opens at whichever list
   change next puts a rollout-queuing role there. Resolution: the contracts slice index
   notes that D4.0 has no dependency and is a precondition of D2.7, and D2.7's preconditions
   list it.
4. **Which Kustomization holds a verifying Job?** Resolved by C3: a verifying Job lives in
   `<tag>-verify`, which depends on the tag's last Kustomization and sets `wait: true`. D5
   Open question 11 asked D4 to adopt that edge. D4.2 renders no Job and no placement rule,
   D5.1a1 renders the `-verify` edge, and D5's netpol-baseline PR names
   `netpol-baseline-verify` as its member (D4-R10-02).
5. **Who owns the expected statuses and the shared-kind layout?** Resolved: D2 does. D2.5
   derives the expected statuses from Kustomization names, and D2.6b holds the allowlist
   census, the empty `SHARED_KINDS` and the reserved names `homelab` and the set's members.
   D4 adds only the per-file document rule (D4.3) and the `-deferred` suffix (D4.2).
   Resolution, applied on 2026-10-14: contracts.md C2 names D2.5 and C3 names D2.6b, and the
   slice index has no D3.3 → D4.3a edge. D3's row has D3.0 to D3.3 wait on D2.1 to D2.6c,
   which covers D2.6b.
6. **Renderer-rule edges and C3.** Resolved: C3's *Ordering* row allows an edge from a
   renderer rule that the adding slice names. *Design* names D4's.
7. **C3's list of deferred applies.** C3 names pihole, headlamp and netpol-baseline. D1 adds
   the flux role as a fourth `manifests_deferred_files` caller, and the renderer's rule keys
   on that variable rather than on a list. Resolution: the contracts.md maintainer words
   C3's row as "a role that passes `manifests_deferred_files` gets `<tag>-deferred` once it
   is flux-owned", and adds that D4.2 reserves the suffix.
8. **Does the operator accept the D4.7 drill on production?** D4.7 merges a failing probe on
   purpose and holds every flux-owned change while `DEPLOY_HOLD` holds T. D2 Open question 1
   asks the operator about D2.8 only. Resolution: the operator accepts or declines D4.7
   before D4.6 merges (**operator-only**), and the D4.7 PR body quotes the answer. If the
   operator declines, Exit criterion 4 waits for D5's netpol-baseline PR, which repeats the
   drill on the real policies-first edge.
9. **How often does arm 1 refuse a shared-template change?** *Baseline* gives an upper bound,
   23 commits to bento-pdf's shared templates in 30 days. Resolution: the replay before D4.4
   merges counts the PRs the gate would have refused, and the PR body quotes it. If the
   count exceeds one a week, the D4.4 PR asks the operator to accept the copy-and-migrate
   cost until D5 ends, and D5's wave order reads the count, because the cost ends for a role
   once its dependencies share its plane. **Ruled 2026-10-16.** D4.4's replay over the 1598
   PRs of the 30 days to 2026-10-16 counted five refusals, about 1.2 a week. Two came from the
   `ingressroute()` macro, #1975 and #2002. Three came only from bento-pdf's own `tasks/`, in
   mechanical edits across many roles. The operator ruled that a `tasks/` change counts only
   for an Ansible-owned role, because a flux-owned role's tasks run nowhere. That leaves two
   refusals in 30 days, under the bar, so the copy-and-migrate cost needs no acceptance.
   `docs/landing.md` states the measured cost.
10. **D5's queueing term for the verify timeout.** D5 *Controller concurrency* asked D4 to
    add the `-snapshot` and `soak` timeouts and a queueing term. Resolved in D4.3: the
    longest-path walk already counts every Kustomization on the path, and the queueing term
    is in, with the 5.5-hour cap and its reason under *What `promote` reads*. D5's
    measurement before the first Wave D flip checks the term against a real revision.
11. **D4 reads `none` as no hold before C2 and C6 say so.** Resolved. C2 reads `none` as no
    hold and any other non-empty value as a hold, and C6 ends `land.sh`'s wait on a value
    other than `none`. D4's preconditions, its clear steps and its *Verification* follow
    that rule (*What `promote` reads*).
12. **Does the cross-plane gate share D6.1's layout?** *The cross-plane gate* says that
    `cross_plane_order.py` renders the cluster at the base and at the head, and names no
    layout. D6.1 gives the renamed `render_diff_gates.py` a layout function for
    `--moved-tags`. It runs `git archive <sha>` into a `moved-tags-` temporary directory
    outside the repository, and it runs each commit's own renderer with `uv run --frozen` and
    one commit value. Resolution, low priority: D4 adopts that function for its CI arms, or
    accepts that D6.1 makes it the module's single layout, so that the CI gate and the
    host-side mode cannot diverge. C1 replaces the `--moved-tags` mode with the `footprint`
    digests in `digests.json`, so the question covers only the gate's own two renders. D6's
    owner says whether a shared layout function survives that change.
13. **Should health checks stop holding workers for unchanged tags?** (P2) The trigger is the
    measured saturation: `FluxWorkersSaturated` firing outside a broad revision, or D5's
    reading showing unchanged Kustomizations holding workers. Three remedies:
    - `wait: false` with generated `healthChecks`, which scopes the health check to the
      objects the renderer lists. C3 fixes `wait: true` on every per-tag Kustomization, so
      this needs a request on C3.
    - A shorter `timeout` for a tag that is often unhealthy, starting with tdarr's 3000
      seconds once D5 replaces its cold pull.
    - No change, relying on the shed, which frees each worker within one `timeout`.

    Recommendation: first read `checkHealth` at the pinned kustomize-controller version,
    to learn whether it skips the health check of a Kustomization whose apply changed
    nothing. If it does not skip, take the shorter `timeout` first, because it needs no
    contract change. Resolution: D4's owner decides when the trigger fires, and files the
    C3 request only if a shorter `timeout` does not end the saturation.
14. **Should `verify` hold one runner for up to 5.5 hours?** (P5) A broad revision, such as
    one that moves every importer of `workload-shell.yml.j2`, holds `verify`'s runner up to
    the cap. The next run's `decide` holds a second runner while it waits, and it gives up
    as `current` if its wait ends first, so no other promotion runs meanwhile.
    Recommendation:
    run `verify` as a short job that re-enters, or as a scheduled poller. Each run reads
    T's statuses once. It writes the hold or moves `last-good` when the verdict is in, and
    otherwise exits and leaves the deadline to the next run. Either form changes C2's
    *Promotion target*, so it is a request on C2. Resolution: D5 measures `verify`'s
    duration on its first broad revision, after the changed-set change. D4's owner files
    the C2 request if that duration delays a later promotion.

## Review log

### Round 1 (per-slice review)

Thirteen findings were raised: five high and eight medium. All thirteen were fixed, and none
was refuted. Each finding's claim was checked against the tree at `70591c307`, or against
kustomize-controller v1.9.6 read through `gh`, before the fix was written.

- **D4-R1-01 (high). The drill commit races its own new edge.** Fixed. The baseline now cites
  `requestsForRevisionChangeOf`, `--concurrent` 4, and the reconcile reading its own object
  before its dependencies. The drill is split into D4.6, the edge, and D4.7, the break. Two
  readings between them confirm the spec and the revision. *Design* states the rule that an
  edge governs from the revision after it is applied. The gate's second arm enforces it. The
  root-edge alternative is rejected with the reason. The Rollback claim is corrected, and
  failure class 5 is named under *Risks*.
- **D4-R1-02 (high). `K8S_APPLIES_LAST` adds no drain to the Ansible play.** Fixed. The batch
  arithmetic was reproduced from the inventory. The last batch is netpol-baseline alone with
  56 entries, and it becomes nut, nut-exporter, pi-peer-backup, scrutiny and netpol-baseline
  once bento-pdf is filtered out. New PR D4.0 adds configarr's drain include to
  netpol-baseline as its first task, with a test. D4.0 lands before D2.7, because the hazard
  opens there. The final-batch alternative is rejected with the reason.
- **D4-R1-03 (high). Exit criterion 1's census cannot pass.** Fixed. The row is scoped to role
  and shared templates, with named members and a red fixture. Its reason records that no k8s
  role applies a static manifest from `files/`, and it gives the reasons for leaving out
  `gotk-components.yaml` and `docs/`.
- **D4-R1-04 (high). `-deferred` Kustomizations fit neither D2's census nor its verify.**
  Fixed. A `-deferred` is the second document in its tag's file. D4.3 derives the expected
  statuses from object names, extends the census, and tests both red halves. Open question 5
  asks D2.5 to adopt the name-based derivation.
- **D4-R1-05 (high). `netpol-baseline-deferred` reverses the role's own order.** Fixed. The
  probe Job goes in `netpol-baseline-deferred` under a rule for verifying Jobs, with a fixture
  test now and a named member in D5. Open question 4 raises the C3 gap and the
  single-Kustomization alternative. Superseded in round 10 by D4-R10-02: C3's `<tag>-verify`
  replaces the placement rule.
- **D4-R1-06 (medium). D4.1 lands as `needs-manual-apply`.** Fixed. D4.1 states the verdict,
  the `k8s_unapplied` discharge by render proof or `deploy.sh`, and the preconditions of the
  window. The D4.1 rollback says that a revert lands the same way.
- **D4-R1-07 (medium). The must-precede test duplicates `DOCUMENTED_ORDERINGS`.** Fixed. D4.1
  extends that table with the five pairs and adds one `K8S_APPLIES_LAST` test in the same
  file. The crowdsec exemption is explicit, and the `docs/facts.lock` step is included.
- **D4-R1-08 (medium). netpol-baseline's `dependsOn` leaves out the `-deferred`
  Kustomizations.** Fixed. The rule excludes netpol-baseline's own `-deferred` and `soak`,
  because a literal "every `-deferred`" would form a cycle. The cycle test carries both cases.
- **D4-R1-09 (medium). Exit 3's event check reads a proxy.** Fixed. The verification takes
  the Ready message and `lastAppliedRevision` inside the health-check window, after
  confirming the source revision. The events grep is now supporting evidence only.
- **D4-R1-10 (medium). The gate's inventory fan-out, escape and multi-PR case are
  undefined.** Fixed. A render diff at the base and the head decides what counts, so the
  `auth_tier` case forms no pair. The fixtures cover it. The unsplittable case exits through
  D2's handback, and its cost is stated. The `settled` rule covers back-to-back PRs.
- **D4-R1-11 (medium). The coupling to D3 is undeclared, and drills can collide.** Fixed.
  *Design* makes both orders explicit, and the peanut claims are gone: the render command
  adds peanut only after D3.6. D4.7 gains D2.8's mitigations and a one-drill-at-a-time
  precondition.
- **D4-R1-12 (medium). The rollback bounds become unsafe once D5 starts.** Fixed. Each PR now
  has its own bound, and D4.1's revert keeps D4.0's drain.
- **D4-R1-13 (medium). The chained timeout omits the per-hop requeue.** Fixed. The formula
  adds one `requeue-dependency` interval per edge and states the free-worker assumption, and
  D4.3 tests it. The ceiling, about 73 minutes on the full map, is stated with its effect on
  `land-wait` and `concurrency: promote`.

### Round integration (cross-slice review)

Three findings named D4: two high and one medium. All three were fixed, and none was
refuted. Each claim was checked against the tree at `70591c307`, against the other slice
documents, or against kustomize-controller and source-controller v1.9.6 read through `gh`.

- **X-03 (high). D4's Exit 4 read `lastAppliedRevision` as a git SHA, but it holds the OCI
  digest revision.** Fixed. Confirmed upstream at v1.9.6, the version D4 already cites,
  rather than the finding's v1.6.0: `api/v1/kustomization_types.go` defines
  `LastAppliedOriginRevision` as the `org.opencontainers.image.revision` value, and
  `kustomization_controller.go` sets it from `getOriginRevision` in the same apply as
  `LastAppliedRevision`. source-controller `ocirepository_controller.go` copies the manifest
  annotations into `Status.Artifact.Metadata`. A new *Baseline* bullet records both fields.
  Exit criterion 4, the D4.6 readings, the Exit 4 commands and the red-proof note read
  `lastAppliedOriginRevision`. The source reading takes the OCIRepository's revision
  annotation instead of `artifact.revision`. The `checkDependencies` bullet keeps
  `lastAppliedRevision`, because both sides of that comparison are tag and digest. D4.6
  gains a precondition that D2's `land-wait` reads the origin field, since its `settled`
  verdict depends on it. The drill now accepts a later descendant of E, because a docs-only
  commit between D4.6 and D4.7 moves `prod` too.
- **X-04 (high). D3.3's non-tag `cluster-vars` Kustomization collides with D2's and D4's
  publish census and verify set.** Fixed in D4's part. The collision was confirmed in the
  slice documents: D2's census requires the files in `out/flux/` to equal the flux-owned
  tags, D3's *Design* never says where `cluster-vars` is published, and no slice-index edge
  orders D3 against D4. New PR D4.3a, split from D4.3, depends only on D2.5 and D2.6 and
  lands before D3.3. It adds the name-based statuses, a closed `SHARED_KINDS` set that
  ships empty, one file per shared kind under `out/flux/`, an allowlist census computed
  from the inputs, and reserved names, `homelab` among them. D3.3 adds `cluster-vars` to the set. D5.1a1's existing allowlist design
  extends this rule. The rest of D4.3 becomes D4.3b. Open question 7 and the cross-slice
  notes carry the amendments to C1, C3, the slice index, D3.3 and D5.1a1. *Risks* names the
  gap as failure class 4. Superseded in round 10 by D4-R10-01: D2.5 and D2.6b took the
  layout, D4.3a is gone, and D4.3b is D4.3 again.
- **X-21 (medium). D4.1 runs `fact_status.py verify` and commits `docs/facts.lock` with no
  condition, though G2 retires both.** Fixed. D4.1's step and its *Tests and docs* line now
  start "Until G2 retires `docs/facts.lock`", R4's wording, and say the step does not run
  after G2.

### Round 10 (per-slice review)

Eleven findings were raised: three high and eight medium. All eleven were fixed, and none was
refuted. Each claim was checked against the tree at `70591c307`, against the other slice
documents, or against GitHub's Actions limits page before the fix was written.

- **D4-R10-01 (high). D4.3a duplicated D2.5 and D2.6b, so the shared-kind layout had four
  owners and D4's baseline about D2 was false.** Fixed. Confirmed in D2: *Publication and
  verification* derives the expected statuses from Kustomization documents of apiVersion
  `kustomize.toolkit.fluxcd.io/v1`, and D2.6b adds the empty `SHARED_KINDS`, the input
  census that admits `kustomization.yaml`, and the `homelab` reservation. D2 is the owner.
  D4.3a is gone. Its remaining parts are the `-deferred` reserved suffix, which moves to
  D4.2, the first PR to emit that name, and the per-file document rule, which joins the
  chained timeout in D4.3, the old D4.3b. The *Baseline* bullet on D2, Exit 5, *What
  `promote` reads*, *Either order with D3*, the header, the tree diff, *Rollback*, *Risks*,
  *Complexity delta* and Open questions 5 and 7 now say so. Cross-slice notes carry the
  contracts.md attribution, the slice-index edge, design.md's ordering line, D3.3a's
  conditional membership and stale references, and D5's "D3.3 creates".
- **D4-R10-02 (high). D4.2's Job-placement rule contradicted C3's `<tag>-verify`.** Fixed.
  C3 and D5 Open question 11 confirmed. D4.2 renders no Job and no placement rule. D5.1a1
  renders the `-verify` edge to the tag's last Kustomization, and its named member is
  `netpol-baseline-verify`. *Design*, D4.2's tests, the red-proof list and Open question 4
  match. D4-R1-05 carries a superseded note.
- **D4-R10-03 (high). D4.7 was a production drill without an operator-only marker or a
  stated blast radius.** Fixed. C8's third substitute confirmed, and D2 Open question 1
  names D2.8 only. D4.7 is **operator-only**, its preconditions quote the operator's
  acceptance (new Open question 8), and its PR body states the objects, what keeps serving,
  and the rollback. The blast radius and the partial-failure rollback name `cluster-vars` and
  peanut when D3 has landed them. A paragraph says why a kind or k3d cluster, or a fixture
  tag, does not suffice.
- **D4-R10-04 (medium). The drill's readings were keyed on D, but `promote` holds T.**
  Fixed. C2's `decide` confirmed, and `git log --since=2026-09-11 --until=2026-10-11
  --oneline | wc -l` prints 1276. Exit 4 and *Verification* key on T, `DEPLOY_HOLD`'s value,
  with `git merge-base --is-ancestor <D> <T>`. The annotation reads `master@sha1:<T>`.
  texbrain's revision P must not contain D, by `merge-base` exiting 1. T's statuses must
  hold `flux/bento-pdf` = `failure` as the control and no `flux/texbrain`.
- **D4-R10-05 (medium). D4.1 turned `test_the_derived_traefik_edge_agrees_with_the_render`
  red.** Fixed. Confirmed in `test_k8s_toposort.py:_derived_traefik_edge`: an edge to every
  entry includes traefik. `_derived_traefik_edge` skips the `K8S_APPLIES_LAST` roles, whose
  traefik edge is unconditional and so cannot be under-derived. netpol-baseline does not
  join `OVER_DERIVED_TRAEFIK_EDGE`. While checking this, a second test was found that D4.1
  turns red: `test_derived_order_matches_todays_hand_ordered_list`, once D1.2's `flux` sits
  after netpol-baseline. D4.1 now moves such entries ahead of netpol-baseline.
- **D4-R10-06 (medium). Arm 1 refuses shared-macro PRs often, and the handback was the only
  exit.** Fixed. Measured independently: 23 commits touched bento-pdf's eight shared
  templates in the 30 days to 2026-10-11, and authelia and traefik import
  `workload-shell.yml.j2` and `container-resources.yml.j2`. The finding counted 18 over
  seven files. Copy and migrate is the standard exit for a shared template. The handback
  stays for a one-field inventory edit. D4.4 replays the gate over 30 days of merged PRs
  before merging, and the cost appears in *Complexity delta*, *Risks*, `docs/landing.md`
  and Open question 9.
- **D4-R10-07 (medium). Arm 2 failed every flip.** Fixed. D3's ownership-dependent
  `checksum/restart` stamp confirmed. Arm 2 judges only a tag that is flux-owned at both
  ends, and D4.4 gains the clean flip fixture.
- **D4-R10-08 (medium). The chained timeout left out D5's queueing term and assumed uniform
  10-minute timeouts.** Fixed. Confirmed `tdarr_k8s_rollout_timeout: 3000s` and its reason
  in tdarr's `CLAUDE.md`, and GitHub's 6-hour job limit. D4.3 adopts the queueing term, says
  that each Kustomization's own `timeout` feeds the sum and that the walk covers `-snapshot`
  and `soak`, and caps the timeout at 5.5 hours, because a job stopped mid-verify writes no
  hold. The full-map size is recomputed through tdarr: about 103 minutes of chain, up to
  about 2.5 hours within `--concurrent`, and `land-wait`'s ceiling at about 5 hours. Open
  question 10 answers D5.
- **D4-R10-09 (medium). D2's census row checks `owner` only, so `depends_on` had no red
  proof.** Fixed. Confirmed in D2 *Design*, and D2.1 names no set. D4.1 adds
  `DEPLOY_MECHANICS_FIELDS` with `owner` and `depends_on` and widens D2's row to read it,
  with `depends_on` as a named member and an `entry.depends_on` red fixture.
- **D4-R10-10 (medium). The scaffolder and D1.2 append after netpol-baseline.** Fixed.
  Confirmed in `new_k8s_service.py:append_entry` and D1.2. *Baseline* names both append paths
  and corrects the batch arithmetic: the last batch with `flux` is netpol-baseline and flux,
  and from D2.7 flux runs alone after netpol-baseline's batch. *Design* says D4.0's drain
  covers only roles earlier in the batch, and D4.1's edge protects a role listed after it.
  D4.1 moves those entries and changes `append_entry`, with a test. The flux role is named
  as a fourth deferred caller, and Open question 7 hands C3 the wording.
- **D4-R10-11 (medium). Four texts became false at D4.1 and were missing from its docs
  list.** Fixed. All four confirmed, plus a fifth: the `entry_lines` and `append_entry`
  docstrings in `new_k8s_service.py`. *Baseline* lists all five, and D4.1's *Tests and docs*
  line changes each to name `K8S_APPLIES_LAST` and the new edges.

Refuted: none.

### Round integration-2 (cross-slice review)

One finding named D4: X2-01, high. It was fixed, and none was refuted. The claim was checked
against the other slice documents and contracts.md before the fix was written.

- **X2-01 (high). `DEPLOY_HOLD`'s no-hold value is `none` in D1 and D2 but "empty" in C2,
  C6, H1, D5 and D4's own checks.** Fixed in D4's part. Confirmed in the documents. D1
  *Design* reads "the value `none` is 'no hold'". D1-R10-08 confirmed upstream, in cli/cli's
  `getBody`, that `gh variable set` ignores an empty `--body`. D2's bullet
  "`DEPLOY_HOLD` has no empty value" adopts `none`. contracts.md C2 still reads "the repo
  variable `DEPLOY_HOLD` is empty", C6 reads "A non-empty `DEPLOY_HOLD`", and *Requested
  amendments not yet applied* has no row for the change. The finding placed D4's check in
  *Verification*; it sat in D4.7's preconditions. Under `none` that precondition could
  never pass, and it would have passed only on the error state, an empty or missing value
  (failure class 3, empty read as clean). Changes:
    - *What `promote` reads* gains the rule: `none` is no hold, any other non-empty value is
      a hold, and an empty or missing value is an error. It defines "held" and "clear", and
      names the clear command, which is **operator-only**.
    - D4.7's precondition reads "`DEPLOY_HOLD` reading `none`", and an empty or missing
      value fails it.
    - Found during the fix: D4.1's landing window read "no `DEPLOY_HOLD`", which the
      finding did not list. Under `none` the variable always exists, so it now reads
      "`DEPLOY_HOLD` reading `none`".
    - D4.8 names `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server` and D2's
      `gh workflow run promote.yml` dispatch. D4.7's *Rollback* bullet points at them.
    - "While `DEPLOY_HOLD` is set" and "stays set" read "holds T" or "keeps T" in D4.7's
      blast radius, *Rollback* and Open question 8, because the variable is always set.
      `land-wait` ends on a `DEPLOY_HOLD` other than `none`.
    - Exit criterion 4 and *Verification* gain the way out: after D4.8 and the clear,
      `probe.py landing` shows `DEPLOY_HOLD = none`.
    - New Open question 11 names D4's departure from C2 and C6 until D1 Open question 9's
      amendment lands, gated before D1.3. The register row and the C2 and C6 wording go to
      the contracts.md maintainer as a cross-slice note.

  *Complexity delta* is unchanged: the fix adds no code, test or object, only wording and
  one read-only reading.

Refuted: none.

### Cross-slice follow-up (2026-10-12)

- Cross-slice note from D6: Open question 12 records D6.1's layout function as the
  candidate layout for the cross-plane gate.

### Pillar review fold (2026-10-12)

Folded from the 2026-10-12 pillar review; not adversarially reviewed. The pre-start review round covers it.

- **P2, worker starvation.** *Baseline* says an unchanged, unhealthy Kustomization holds a
  worker too. *Design* reads C2's changed set for the timeout: *Which Kustomizations count
  as changed* reads the `footprint` digests in place of a second pulled bundle, an
  unchanged ancestor on the path counts one hop, and dependents count in the queueing
  term. *What the ceiling cannot see* states the gap. *Saturated reconcile workers* adds
  C7's rule, its labels, a series delta of zero, the measurement at D4.7 and in D5, and
  the shed bound of one `timeout`. tdarr's timeout stays with D5, by a cross-slice note.
  *The size*, `land-wait`'s figures, *Risks* and *Complexity delta* are restated: about 41
  minutes for a one-role change, about 2 hours for tdarr, and the cap for a broad
  revision. *Steps* adds D4.9 at about 40 lines, and D4.3 grows to about 150 lines with
  split tests. *Verification* gains the promtool and pint red proofs and the drill's
  occupancy reading. *Risks* gains the starvation entry. The remedy went to Open
  question 13.
- **P5, long verify.** The *Risks* entry on the promote timeout names the runner hold.
  Open question 14 recommends a re-entering `verify` or a scheduled poller, as a request on
  C2, resolved by D5's measured worst case after the changed-set change.
- **Contract sync.** Open question 11 is resolved, because C2 and C6 read `none` as no
  hold. Open question 12 notes that C1 replaces `--moved-tags`. Exit criterion 4, the
  Exit 4 reading and the drill's *Risks* entry take T from the first field of C2's
  `<T> failed=<names>` hold, and the hold's `failed=` list must name bento-pdf.

### D2.5 verification fold (2026-10-15)

- **#5078: D4 reasoned from a `verify` inside the group `promote`.** Confirmed against
  `.github/workflows/promote.yml`. D2.5 runs `verify` and `settle` in no group, and `settle`
  writes `<T> unverified` for a `verify` stopped with no verdict. Fixed: *The cap* now rests
  on that hold, names the three limits that move with the derived timeout, and places the
  6-hour limit on the `promote` job's in-flight wait. D4.3 raises the three limits together.
  *Risks* and Open question 14 describe the wait in the next run's `decide`, and three
  sentences that named `verify` as the hold's writer name `settle`. D4-R1-13 and D4-R10-08
  keep their wording as history.
