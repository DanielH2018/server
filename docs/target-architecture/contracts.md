# Target architecture — cross-slice contracts

Every slice plan in this directory builds against the interfaces below. A slice that needs
an interface to change says so in its *Open questions* section and names the contract row. A
slice must never silently diverge from a contract. [design.md](design.md) explains why each
part of the architecture exists. This page states only what the parts agree on.

**Baseline.** Measure the tree at commit `70591c307` (2026-10-11). Each slice's *Baseline*
section states that SHA. The deployer code lives under
`ansible/roles/setup/gitops_deploy/files/deployer_lib/`, and the narrowing code under
`scripts/deploy_tools/narrow_lib/`. Other sessions edit both paths, so cite symbols, not line
numbers.

## Slice index and dependency edges

The *Depends on* column names the slices a slice waits on. A sub-edge such as "D2.7 also
D3.3" says that one PR waits on one PR of another slice, and the slice's other PRs do not.
Sub-edges keep the graph acyclic where two slices interleave: D2 with D3 and D4, D5 with H2,
and D5 with M6. A sub-edge marked *live* waits until the earlier PR is deployed, not only
merged.

| ID | Slice | Depends on |
|---|---|---|
| R1 | Shadow render with zero diff | — |
| R2 | Secret placeholders and CI secret checks | R1 |
| R3 | Remove the pre-apply live reads | R1 |
| R4 | Delete the k8s Ansible path | D6 |
| D1 | Flux installed, owning nothing | — |
| D2 | One stateless leaf end to end on production | D1, R1, and R2's PRs 1 and 2 (placeholder mode and the five rules). D2.7 also D4.0, and D3.3 under C3's default source for `domain` |
| D3 | Secrets through Flux | D2, R2. D3.0 to D3.3 need only D2.1 to D2.6, which include D2.6b's empty `SHARED_KINDS` and census |
| D4 | Ordering (`dependsOn`, deferred applies) | D2. D4.0 needs nothing |
| D5 | Bulk move | D3, D4, R3, M1, M2. traefik's flip and the flux PR also H1.1a, and the flux PR also H1.7b. Wave D also H2.1, and the H2 family PR of each role H2 moves. The PR that flips the last Ansible-owned stateless leaf also M6's B-1e |
| D6 | Delete the old deploy path; ADR superseding 0010 | D5 |
| M1 | Alertmanager and its watcher | — |
| M2 | One bridge family to rules | M1 |
| M3 | One host timer to the textfile collector | M1. The cutover also M2's PR 3 |
| M4 | blackbox_exporter for the ingress probes | M1. PR 3 also M2's PR 3 |
| M5 | loki-homelab ruler | M1. The cutover also M2's PR 3 |
| M6 | Switch the readers and retire Kuma | M2, M3, M4, M5. A7's shadow PR also M5's ruler, live; A7's Cutover-a also M2's PR 4a, live. Phase C's C-4b also D6 |
| H1 | Hand the cluster content to Flux | D2, D4. H1.7b also D3; H1.9 also R4 |
| H2 | Shrink the host plane (crons → CronJobs) | H1.1a to H1.8. H2.3b also D3; H2.3c also H2.3b, live; H2.8a also D3, H6.4a and H6.4b |
| H3 | Pi pilot: builder, updater, versioning | R1 |
| H4 | Secrets unit; NUT on daniel-server first | H3 |
| H5 | Pi Compose | H3, H4 |
| H6 | system-upgrade-controller for k3s | D2, H1.1a to H1.8, H4, M1 |
| H7 | Remaining roles, then delete Ansible | H2, H5, H6, R4 |
| G1 | Diff notice alongside the lock | — |
| G2 | Retire the lock; ADR amending 0019 | G1 |
| G3 | (optional) Notice dispatches the freshness reviewer | G2 |
| G4 | Drop the CI size test | —. The optional PR 3 also G2 |
| G5 | Shrink root CLAUDE.md | G2 |

H2 depends on H1.1a to H1.8 and never on H1.9. H1.9 waits on R4, R4 on D6, and D6 on D5,
whose wave D waits on H2's family PRs, so a dependency on all of H1 would be a cycle.

D5's edge to M6's B-1e keeps one Ansible-owned stateless leaf alive for M6's live
health-gate drill. The drill's tag must not be flux-owned, because D5's soak and
`probe.py health` share one selector (M6 Open question 4). The edge adds no cycle. B-1e
waits on PRs of M1 to M6 and on C7's selector amendment, and none of those waits on D5. Only
M6's C-4b waits on D6, and B-1e does not wait on C-4b. The edge does put M6's B-1 on D5's
path to its exit when M6 finishes later. If M6 moves the drill to a tag the soak may own,
which needs a drill exclusion in C7 that no slice has requested, the edge goes.

## C1. Renderer

- **Entry point.** One CLI, `homelab-render`, run as
  `uv run python scripts/renderer/homelab_render.py`. R1 chose that path under the rules in
  `.claude/rules/python-layout.md`. The renderer reuses `scripts/lib/ansible_jinja_env.py`,
  `scripts/deploy_tools/render_targets.py` and `ansible/filter_plugins/`. R4 moves
  `render_targets.py`'s logic into the renderer rather than deleting it. Comparison and stub
  tooling beside it, such as R1's `shadow_diff.py`, renders nothing itself and is not a second
  renderer.
- **Modes:**
  - `homelab-render cluster --out DIR [--role TAG ...] [--secret-stubs FILE]`. Without
    `--secret-stubs`, the render is in placeholder mode (R2). With it, the render is in stub
    mode. R1's shadow comparison reads stub mode, and so do R2's `roundtrip`, from R2's PR 1,
    and R1's determinism module. Those two keep reading it after R4's PR 9 deletes the
    comparison (R2, R4). Before R2 lands, `cluster` without `--secret-stubs` exits 2.
  - `homelab-render host --host NAME --out DIR`
  - `homelab-render package --name PKG --out DIR`, for the shared packages of C5 (H3).
- **Only placeholder mode publishes.** The publish job runs `cluster` without
  `--secret-stubs`, and its command says so. It refuses a bundle in which a Kustomization
  without `postBuild` has a directory that holds `${<key>}` for a key name of `secrets.yml`.
  That refusal is the check that stops a route rendered without its real `domain` before
  `prod` moves.
- **Cluster output:**
  - `DIR/cluster/<tag>/` holds the role's manifests and a `kustomization.yaml` that lists them.
  - `DIR/cluster/<tag>-deferred/` holds the files the role passes as
    `manifests_deferred_files` (R1).
  - `DIR/flux/<tag>.yaml` holds the Kustomization `<tag>` and its suffixed kinds, see C3.
  - `DIR/flux/<name>.yaml` and `DIR/cluster/<name>/` hold a shared kind, see C3.
  - From D5.1a1, `DIR/cluster/<tag>-snapshot/` and `DIR/cluster/<tag>-verify/`. From D5.1a2,
    `DIR/digests.json` at the bundle root. No Kustomization's path reaches `digests.json`.
- **Vendored files.** A role may ship verbatim upstream files and kustomize `patches:` in its
  `kustomization.yaml` (H1). The renderer refuses a directory that holds vendored files and a
  placeholder, so such a directory never substitutes.
- **Host output:**
  - `DIR/hosts/<host>/root/` is the file tree the package installs.
  - `DIR/hosts/<host>/nfpm.yaml` is that package's nfpm spec.
  - `DIR/packages/<pkg>/root/` and `DIR/packages/<pkg>/nfpm.yaml` are a shared package's.
- **No live reads.** The renderer reads only the git tree. It never contacts a cluster, a host,
  a registry or a decryption key, and two renders of one commit are byte-identical.
- **Secret references, cluster mode only.** A reference renders as `${<key>}`, where `<key>` is
  the exact key name in `ansible/vars/secrets.yml`. A template a role lists in
  `render_sops_manifests` renders no document in placeholder mode, because its Secret takes
  C3's second path (R2). Host and package modes render no secret and keep every `${` literal
  (H3).
- **Substitution and escaping, cluster mode only.** The unit is a Kustomization directory
  under `DIR/cluster/`, and `<tag>` and `<tag>-deferred` are decided separately. A directory
  *substitutes* when its render holds at least one placeholder. Exactly then its
  Kustomization carries `postBuild` (C3), and exactly then the renderer escapes: a `$`
  followed by `$`, by `{` or by a secret reference renders as `$$`, so Flux's substitution
  restores every literal `$` run (R2). A directory that does not substitute keeps every `$`
  as its templates write it, because Flux never substitutes it (H1).
    - **The one source** is the renderer's placeholder key set for each directory, computed
      in process from the render. The escape, R2's CI rules, R2's round trip and the writer
      of each Kustomization's `postBuild` (D2, then D3's `substituteFrom` rule) all read that
      key set, and none of them derives it a second time.
    - **The projection.** Wherever the renderer writes a directory's Kustomization under
      `DIR/flux/` (D2 on), `spec.postBuild` is present there exactly when the key set is
      non-empty. A reader that sees only the bundle reads that field. The field is not the
      source, because R2's checks land before D2 writes the first `DIR/flux/` file, and
      from D2 on CI's all-tag render writes one only for a tag passed with `--role` (R2).

## C2. Cluster artifact and promotion

- **Artifact.** `oci://ghcr.io/danielh2018/homelab-manifests`. The `publish` job pushes it
  with `flux push artifact --revision master@sha1:<40-hex sha>` and signs it with cosign
  keyless under the `promote` workflow's identity (D1).
- **Tags:**
  - `sha-<40-hex>` for every green master commit;
  - `prod`, the revision Flux follows;
  - `last-good`, the newest revision whose statuses all passed.
- **Promotion target.** `promote` is a GitHub Actions workflow with `concurrency: promote`. It
  runs after each green CI run on master. GitHub may evict a queued run, so a run is not tied
  to one commit. Each run promotes the newest published master commit: `promote.py decide`
  walks `origin/master` from its tip back to `prod`'s commit and takes the first commit T with
  a published `sha-<T>` tag, a signature that verifies and a green CI verdict. It prints
  `promote`, `held`, `current` or `error`, and moves `prod` to T only when:
  - the repo variable `DEPLOY_HOLD` reads `none`; and
  - T descends from the current `prod`.
- **`DEPLOY_HOLD`'s values.** `none` means no hold. Any other non-empty value is a hold, so a
  mistyped hold still holds. An empty value or a missing variable is an error: `decide`
  prints `error`, exits non-zero and moves no tag. `gh` cannot set an empty value, so D1
  creates the variable as `none` (D1).
- **Verification.** `promote.py verify T` checks T, the commit `decide` printed, never the
  commit that triggered the run. It expects `flux/homelab` plus `flux/<name>` for every
  Kustomization name published under `flux/` (D2.5). A missing status keeps it waiting and
  never reads as success. When every expected status is `success`, `last-good` moves to
  `sha-<T>`.
- **On failure**, meaning a `failure` status or a timeout, it writes T to `DEPLOY_HOLD` and
  then resets `prod` to `last-good`. The hold is written first, so a failed reset still stops
  the next promotion. Clearing `DEPLOY_HOLD` means setting it back to `none` with
  `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`, and it is
  **operator-only**.
- **Commit statuses.** notification-controller posts one status per Kustomization, with context
  `flux/<kustomization>`, on the commit that the origin revision names (C6).

## C3. Flux objects

- **Namespace.** Everything lives in `flux-system`, with one `OCIRepository` named `homelab`.
- **Root.** One Kustomization, `homelab`, with path `./flux`, applies every Kustomization the
  bundle publishes. It sets `prune: true` and `wait: false`, because each child reports its
  own health and status (D1).
- **Per-tag Kustomizations.**
  - One per deploy tag, named exactly as the tag, with path `./cluster/<tag>`, `prune: true`
    and `wait: true`.
  - A deferred apply (pihole, headlamp, netpol-baseline) is a second Kustomization,
    `<tag>-deferred`, which depends on `<tag>`.
  - A verifying Job lives in `<tag>-verify`, which depends on the tag's last Kustomization
    and sets `wait: true` (R4, D5).
  - D5.1a1 adds `<tag>-snapshot` for a role that declares snapshot claims.
- **Per-role fields.** A role may set three fields of its own Kustomization from its defaults:
  `prune: false`, for H1's vendored CRDs; its own `timeout` (H1, H6); and `healthCheckExprs`
  for its own custom resources (H6). No other field varies by role.
- **Shared kinds.** A Kustomization that belongs to no deploy tag is a shared kind. The
  renderer names every shared kind in a closed literal set, `SHARED_KINDS`. D2.6b adds the
  set empty, D3.3a adds `cluster-vars`, D5.3b2 adds `soak`, and D5's namespace PR adds
  `homelab-namespace`. Each joins in the PR that renders it, because the census fails a
  member with no file. C1 says where a shared kind is published, and C2 verifies it through
  `flux/<name>` like a tag. From D6.1, each member also needs a rule in
  `render_diff_gates.py --moved-tags` that names the deploy tags its move hands `land-wait`,
  and a census test fails the PR that adds a member without one (D6).
- **Reserved names.** The render fails on a deploy tag that equals a member of `SHARED_KINDS`
  or ends in `-deferred`. D5.1a1 reserves `-snapshot`, `-verify`, `soak` and
  `homelab-namespace` too, so no deploy tag takes `soak` or `homelab-namespace` before its
  kind joins the set.
- **Publish census.** The files under `flux/` must equal the flux-owned tags plus the members
  of `SHARED_KINDS`, and each tag's file holds exactly `<tag>` and the suffixed kinds its
  role's inputs call for. The census computes this allowlist from the renderer's inputs, never
  from its output, so a renderer bug cannot widen it (D2.6b). A mismatch fails the publish,
  and nothing is pushed.
- **Deletion policy.** Every Kustomization the bundle publishes sets `deletionPolicy: Orphan`.
  D6.7 switches the per-tag Kustomizations to the default policy, if the operator approves it
  (D6 Open question 7).
- **Ordering.** `dependsOn` is generated, and no slice hand-writes it. Its edges come from
  `ansible/filter_plugins/toposort.py`, or from a renderer rule that the adding slice names,
  such as D3's `cluster-vars` edge and D4's `-deferred` edges.
- **Prune exemptions.** PVCs and Namespaces carry `kustomize.toolkit.fluxcd.io/prune: disabled`.
- **Secrets.** CI never holds an age key, and an agent never decrypts. A cluster secret takes
  one of two paths:
  - One Secret, `homelab-cluster-vars`, holds every value the cluster manifests reference as a
    placeholder. It feeds `postBuild.substituteFrom` on each Kustomization that substitutes
    (C1). D3
    owns how this Secret is produced and kept in step with `secrets.yml`. A value holds no
    newline, double quote, backslash, control character or `{{`. Flux deletes newlines from
    a substituted value, a placeholder may sit inside a JSON string, and authelia's
    template filter parses its whole configuration file, where many values land (R2, D3).
    D3's operator-only `cluster_vars.py write` refuses each, naming the key and never the
    value.
  - The keys in `SOPS_MANIFEST_KEYS` ship as SOPS-encrypted Secret manifests, one per
    `render_sops_manifests` entry, which kustomize-controller decrypts (R2, D3).

  Any step in either path that needs plaintext is **operator-only**.
- **The source of `domain` before D3.** `homelab-cluster-vars` first exists at D3.3. D2's leaf
  references one variable, `domain`, in its IngressRoute's Host rule, so it cannot flip before
  that key has a source.
  - **Default:** D2.7 waits on D3.3, and `domain` comes from `homelab-cluster-vars` like every
    other key. No plaintext reaches git.
  - **Alternative:** the operator rules that `domain` may be published, and the renderer
    writes it as a literal (**operator-only**). D2.7 then needs no part of D3.
  - **Rejected:** a Secret the operator creates by hand. D3 would have to adopt an object that
    no commit describes, and D3.5 shows that Flux removes keys only for the field managers it
    takes over.

## C4. Ownership handoff (the two-writer rule)

- **Ownership flag.** A `containers_list` entry gains `owner: flux` in the same PR that adds
  its Kustomization.
- **What skips a flux-owned entry.** In that same PR, the Ansible k8s play, `deploy.sh`, the
  GitOps deployer's k8s arms and `land.sh` all skip it. No commit may leave a role reachable
  by both Ansible and Flux.
- **Removal.** Three slices remove `containers_list`, each for its own hosts:
  - **H1.9**, after R4, moves daniel-box's k8s entries to `cluster/services.yml` and drops
    their `owner` key, because every entry is then flux-owned. It deletes `containers_list`
    from `daniel-box.yml` and `daniel-server.yml`. The renderer exposes the moved list under
    the same variable name.
  - **H5** moves the Pi's entries to `compose_services` in
    `ansible/inventory/host_vars/daniel-pi.yml` and leaves the Pi's `containers_list` empty.
    `compose_services` stays there as renderer data, because H7 keeps `ansible/inventory/`.
  - **H7** deletes the Pi's empty `containers_list` key, and the one in
    `ansible/inventory/host_vars/_example.yml`, in the PR that deletes their last reader:
    `ansible/preflight.yml` or the Ansible Docker play, whichever goes last.

## C5. Host packages

- **Package names:**
  - `homelab-<hostname>`, one per host;
  - `homelab-updater`, the pull timer;
  - `homelab-repos`, the third-party apt sources and keyrings.
- **Version.** `0.<git rev-list --count origin/master>+g<12-hex sha>`, with `Architecture: all`.
- **Repository.**
  - Suite `homelab`, component `main`, keyring `/usr/share/keyrings/homelab-archive-keyring.gpg`.
  - Served by a plain systemd unit on daniel-box, outside k3s. H3 picks the port and the URL.
- **Signing.** Only the LAN builder signs, and only commits whose SSH signature verifies. CI
  builds the same packages for diffs and tests but never signs.
- **Package rules.** No conffiles, and enable links ship as package files. The postinst calls
  only `systemctl`. Packages carry SOPS ciphertext only, which `homelab-secrets.service`
  decrypts with the host's own age key into `/run/credstore`.

## C6. Applied-commit reporting

- **Host plane.** `homelab-updater` writes a node-exporter textfile metric,
  `homelab_host_applied_info{host="<h>",sha="<12-hex>",version="<v>"} 1`.
- **Cluster plane.** The commit statuses from C2, plus Flux's `gotk_resource_info` series,
  whose `revision` label reads `status.lastAppliedOriginRevision` (D1.6).
- **Which revision field names a commit.** A Kustomization's `status.lastAppliedOriginRevision`
  holds `master@sha1:<sha>`, the `--revision` that `flux push artifact` wrote. Its
  `status.lastAppliedRevision` holds `<tag>@sha256:<digest>` and names no commit. Every reader
  that needs the applied commit reads the origin field. kustomize-controller's own `dependsOn`
  check compares `lastAppliedRevision` with the source's artifact revision, both in digest
  form, and a slice that describes that check uses the digest field.
- **`land.sh`.** After merging M, it waits until M is covered. It never waits on one `promote`
  run, because GitHub may evict a queued run (D2).
  - **Cluster.** Every Kustomization of the PR's flux-owned tags is Ready at an origin
    revision R that equals M or descends from it. From D5 on, `flux/soak` also reads
    `success` on R. A `DEPLOY_HOLD` other than `none` (C2) ends the wait as `deploy-failed`,
    and twice the promote timeout ends it as `gave-up`.
  - **Hosts.** For each host the PR touched, `homelab_host_applied_info` shows M or a
    descendant of M, and `homelab_host_reconcile_ok` reads 1. H3 states how the touched hosts
    are found and when the wait ends `deferred`.

  It then runs `probe.py health` for the changed services.

## C7. Alert plane

- **Rule labels.** Every rule carries `service="<deploy tag or host name>"` and
  `severity="critical|warning|info"`. For an exporter's series, `service` is a target label
  that the scrape config sets, or a literal in the rule (M6). A rule that verifies a deploy
  also carries `verify="true"` (D5).
- **Watchdog.** A rule named `Watchdog` always fires. It routes to the Healthchecks.io ping.
- **Health gate.** `probe.py health <tag>` reads
  `ALERTS{alertstate="firing",service="<tag>"}` and `probe_success{service="<tag>"}`.
- **The soak.** From D5.3, a Job in the shared kind `soak` reads the alerts labelled
  `severity="critical"` or `verify="true"` for the flux-owned services, after M1's filter for
  shadow rules and the Watchdog. Its verdict reaches GitHub as the commit status `flux/soak`,
  which C2's verification expects. The soak is a Job, and no alert rule stands for it.
- **Verify successors.** Each deploy-time verification that the Ansible play ran gets one
  successor, of one of three shapes, and R4's exit 5 accepts only these:
  1. a rule labelled `service="<tag>"` and either `severity="critical"` or `verify="true"`;
  1. a Job in the `<tag>-verify` Kustomization (C3);
  1. a check in `probe.py health <tag>`, only for a role with `k8s_autodeploy: false`.
     `probe.py health` runs only from `land.sh`, so it misses every automerged bump.
- **Log rules.** These run on loki-homelab only. Rules must never template text from the
  observability Loki, which holds transcripts.

## C8. Rules every slice document follows

- **Citations of paths that do not exist yet.** Never cite one as `file:line`, as a pytest
  node id (a test file, two colons, then a test name), or as a full path under
  `ansible/tests/`. CI checks all three shapes against the tree. Describe a future test by
  what it asserts and the directory it joins.
- **Citations of existing code.** Cite by path and symbol (`deploy_git.py:ci_verdict`), not by
  line number.
- **Links.** Markdown links point only at docs that exist under `docs/`. Write repo files as
  inline code, never as links.
- **Security.** The repo is public. Describe no security weakness that is not already public.
- **Operator-only steps.** Any step that decrypts, approves an ADR, or changes GitHub
  settings is marked **operator-only**.
- **No stage cluster.** The `daniel-stage` guest was retired on 2026-09-29
  ([the archived staging page](../archive/staging-cluster.md);
  `scripts/lib/kubectl.py:RETIRED_CLUSTER_NODES`). No step may gate on a stage run. A proof
  that needs a running cluster names one of three substitutes and says why it suffices:
  - a throwaway kind or k3d cluster in a CI job, for a proof that needs only the Kubernetes
    API, the pinned Flux controllers and a local-path volume;
  - a fixture tag in a scratch namespace on production, whose objects nothing else reads;
  - an **operator-only** production drill, whose PR states its blast radius: the objects it
    touches, what keeps serving meanwhile, and the rollback.

## Slice document format

Each slice is one file with these sections, in this order:

1. Title line `# <ID> — <name>`, then `Status: PLANNED`, `Depends on`, `Unblocks`.
2. **Goal and exit criterion**: what is true afterwards, and the observable check that proves it.
3. **Baseline at `70591c307`**: the measured facts this plan rests on, each with its evidence.
4. **Design**: what changes, including a `diff` of the file tree or call tree it adds or removes.
5. **Steps, as PRs**: each PR's scope, kept to about 100–200 lines, in order, with the
   two-writer check (C4) where it applies.
6. **Verification**: the commands that prove the exit criterion, plus a proof that each new
   check can go red.
7. **Rollback**: how to undo each PR, and what a partial failure leaves live.
8. **Risks**: each risk with its mitigation. Check them against `docs/failure-classes.md`.
9. **Tests and docs that change in the same PR.**
10. **Complexity delta**: what is added and what is removed.
11. **Open questions**: each one with a resolution path.
12. **Review log**: one entry per review round, listing the findings raised, fixed and
    refuted, with the evidence for each refutation.

## Requested amendments not yet applied

Slices request these contract changes in their *Open questions*. Each row above stays as
written until its amendment lands, before the gate named where the slice names one. A slice
that gates a PR on a request finds the request here. When a round applies a request, the
request leaves this list and the round's *Review log* entry names it.

### Requests on the slice index

- **H2's per-PR edges** (H2 Open question 1). H2.2a needs H1.5a and H1.6, H2.3b needs H1.5a,
  and H2.8a needs H1.7b and H6.4b. Every other H2 PR needs nothing outside H2. The row then
  drops H1.8 and the H2.3b → D3 edge, and "H2.8a also D3, H6.4a and H6.4b" becomes "H2.8a
  also H1.7b and H6.4b", which reaches D3 and H6.4a through those two. No gate: each change
  only loosens an order the row already enforces.
- **M6's phases** (M6 Open question 1). Split M6 into M6a (Phase A, on M2 to M5), M6b
  (Phase B, on M6a's A6) and M6c (Phase C, on M6a and M6b, and on D6 for C-4b only). No gate
  is named.

### Requests on C1

- **The flux directory's kustomization** (D2 Open question 6). *Cluster output* gains
  "`DIR/flux/kustomization.yaml` lists every file in `DIR/flux/`." Gate: before D2.6b merges.
- **A shared-only mode** (D3 Open question 6). *Modes* gains `cluster --out DIR
  --shared-only`, which writes the shared kinds and the root and no tag, for a publish with
  no flux-owned tag. Gate: before D3.3a merges.
- **The commit input** (R4 Open question 10). *Modes* names `homelab-render cluster --commit
  SHA`. The publish job passes the commit it publishes, and the render-diff job passes the PR
  head's SHA to both renders. Gate: before D6.1 or R4's PR 1 merges, whichever
  comes first (D6 Open question 13).
- **Image contexts** (D5 Open question 10). `DIR/images/<name>/` and `DIR/images/tags.json`,
  which no Kustomization's path reaches. Gate: before D5.4a1 merges.
- **Host-mode placeholders** (H4 Open question 6, with H3's *Host mode renders no secret*).
  Host mode renders a secret reference as `${<key>}` in two kinds of file, a component's
  declared outputs and its compose files, and escapes those files by the cluster-mode rule.
  Every other host file keeps every `${` literal. Gate: before H4.1a3 merges.
- **The label that names a deploy tag** (M2 Open question 1). The renderer keeps stamping
  `homelab/role`, or C7 adopts `kustomize.toolkit.fluxcd.io/name` with `-deferred` stripped.
  Gate: before D6 merges.

### Requests on C2

- **The forward-only hold** (H1 Open question 3, which quotes the full text this row adopts).
    - A failed revision that changes a forward-only tag (H1) or a snapshot role (D5) writes
      `DEPLOY_HOLD` and leaves `prod`.
    - `prod` moves to T only when every commit in (`last-good`, T] that changes a
      forward-only pin maps to a PR whose head carries a successful `homelab/upgrade-gates`
      status. A commit with no PR fails that test, and a failed lookup ends the run as
      `error`.
    - `prod` also stays when (`prod`, T] changes one forward-only pin in more than one commit,
      moves `k3s_longhorn_version` more than one minor, or carries a required gate status
      older than six hours. A tag that T drops counts as changed.
    - Before it moves `prod`, `decide` records the `flux/<name>` statuses on `prod`'s commit.
      `verify` excuses a `failure` from a name whose tag T did not change and whose recorded
      status was already `failure`. It also excuses an unchanged name with no status at the
      timeout whose `dependsOn` chain reaches an excused name.

  Gates: before H1.1b merges for the hold, and before H1.1i merges for the excused failures.
  Until then *Verification* moves `last-good` only when every status is `success`.
- **D5's changes** (D5 Open question 4).
    - Snapshot roles hold without a reset. This part also waits on the operator's ruling in
      D5 Open question 1 (**operator-only**).
    - The expected statuses become every Kustomization in the bundle's `flux/` directory,
      minus suspended ones.
    - From D5.3b2 until D5.3b3, `verify`'s observe-only set leaves out `soak` and ignores a
      `flux/soak` `failure`, so the first live soaks neither hold nor reset. D5.3b3 deletes
      the set.
    - A `reset_to_last_good` dispatch input runs in its own concurrency group,
      `promote-reset`. It ignores the reboot window, and it refuses unless `DEPLOY_HOLD`
      holds a hold: `none`, an empty value and a missing variable each refuse.
    - The hold posts to Discord, with a command block per changed snapshot role. A revision
      that changes a suspended tag holds with `suspended-changed`.
    - No promotion moves `prod` inside the derived reboot window. A run inside it ends
      `deferred` without waiting, and a derived `schedule:` run promotes at the window's end.

  Gate: before D5.1b merges. The observe-only set alone has a window of its own, from D5.3b2
  to D5.3b3.

### Requests on C3

- **The verify drill's OCIRepository** (D1 Open question 7). *Namespace* names
  `verify-drill` as a transient second `OCIRepository` in `flux-system`, present only while
  `flux_verify_drill` is true. Gate: before D1.5 merges.
- **The root's name is reserved** (D2 Open question 6). The render fails on a deploy tag
  named `homelab`, the root Kustomization's name. Gate: before D2.6b merges.
- **Deferred applies by variable** (D4 Open question 7). *Per-tag Kustomizations* reads "a
  role that passes `manifests_deferred_files` gets `<tag>-deferred` once it is flux-owned",
  because D1 adds the flux role as a fourth caller, and *Reserved names* says that D4.2
  reserves the suffix. No gate is named.
- **SOPS manifests and the decryption field** (D3 Open question 6).
    - *Per-role fields* gains a fourth field, `decryption: {provider: sops, secretRef: {name:
      sops-age}}`, derived from a non-empty `render_sops_manifests` and never set by hand.
      The shared kind `cluster-vars` sets it too. Gate: before D3.3c merges.
    - Each SOPS manifest carries `kustomize.toolkit.fluxcd.io/substitute: disabled` in its
      plaintext metadata, written before encryption, and the renderer copies it byte for
      byte. Gate: before D3.3c merges.
    - *Secrets* gains "`sops-age` is the only Secret made by hand." Gate: before D3.3a merges.
- **Suspension and hand writes** (D5 Open question 5). `spec.suspend` through
  `flux_suspend`, rendered only when true. Edges to a suspended Kustomization are dropped. A
  hand write to a Flux object uses the field manager `homelab-operator`. `<tag>-snapshot`
  inherits `<tag>`'s `dependsOn`, and `homelab-namespace` owns the namespace. Gate: before
  D5.1a1 merges.
- **`prune: false` beyond vendored CRDs** (D5, traefik's step in Wave D). *Per-role fields*
  names `prune: false` for H1's vendored CRDs only. D5 sets it, through H1.1a's
  `flux_prune`, on traefik's tag and on `flux`, whose renders hold CRDs. The field then
  reads "for a tag whose render holds a CustomResourceDefinition (H1, D5)". Gate: before
  traefik's flip merges.
- **Wider prune exemptions.**
    - PersistentVolumes and StorageClasses join PVCs and Namespaces (D6 Open question 12).
      Gate: before D6.7b merges. Without the amendment, D6.7b refuses to remove a tag whose
      render at `prod` holds a PersistentVolume, and the rename procedure excludes
      media-volume.
    - "and a ConfigMap that holds state no render restores, named by its slice", with H2's
      restore-drill stamp ConfigMap as its member (H2 Open question 12). Gate: before H2.3b
      merges.
- **The deletion policy's kept set** (D6 Open question 7). If D6.7 merges, it switches every
  `<tag>` except `flux`. `flux`, every `<tag>-deferred` and every shared kind keep `Orphan`.
  Gate: before D6.7 merges.

### Requests on C4

- **A second `owner` value for the handback** (D2 Open question 7). "`owner` takes `flux` or
  `released`. `released` leaves the tag out of the bundle while every Ansible path still
  skips it. A handback is two PRs: `released`, then the removal of `owner` once the tag's
  Kustomization reads `NotFound`." Gate: before D2.7 merges, because D2.7's rollback relies
  on it.

### Requests on C5

- **Which signature the builder trusts** (H3 Open question 1). Gate: before H3.5a merges
  (**operator-only**).
- **The version counts the built commit** (H3 Open question 7). The builder builds the newest
  green commit, which can be older than the tip. *Version* then reads
  `0.<git rev-list --count --first-parent <chosen>>+g<12-hex sha of <chosen>>`. Gate: before
  H3.5a merges.
- **Where decrypted files land** (H4 Open question 4). Besides `/run/credstore`,
  `/run/homelab-secrets/<path>` and, for a persisted output,
  `/var/lib/homelab-secrets/<path>`. A consumer reaches one through a symlink at the real
  path, or through a drop-in when another package owns that path. Each host package carries
  one per-host SOPS file. Gate: before H4.1a1 merges.
- **A package for architecture-specific tools** (H7 Open question 1, H4 Open question 2).
  `homelab-tools` is architecture-specific and built by nfpm from pinned upstream binaries,
  sops and chezmoi among them. The alternative mirrors upstream's `.deb` files. Gate: before
  H7.1 merges.

### Requests on C6

- **The soak in `land.sh`'s wait** (D5 Open question 18). `flux/soak` gates the wait from
  D5.3b3, not from D5's first PR. A `flux/soak` `failure` on R ends the wait as
  `deploy-failed` with the `Cause` `soak-failed`, before `verify` writes `DEPLOY_HOLD`.
  `land-wait` repeats the combined-status read on each poll until the soak has a verdict.
  Gate: before D5.3b3 merges.

### Requests on C7

- **CronJob checks and the health gate's selector** (M6 Open questions 3 and 4). The
  kube-state-metrics recipe for CronJob-based checks joins M3's textfile contract. The
  health gate's selector departs from C7's literal one in six ways: `shadow!="true"`,
  `alertname!="Watchdog"`, the soak's set of `severity="critical"` or `verify="true"`,
  alerts active after the tag's reference time, a `probe_success` sample newer than that
  time, and no probe half for a tag whose entry has no route. D5's soak and `probe.py
  health` share that one definition. Gates: before M6's B-1 merges for the selector, and
  before B7's pi-peer-backup shadow PR for the recipe.
- **Log rules stay outside the health gate** (M5 Open question 1). A loki-homelab ruler
  alert never reaches Prometheus `ALERTS`, and the ruler-watching alerts cover the service
  there. Gate: before M5's PR 4b merges.
- **Where Alertmanager lives** (M5 Open question 2). Alertmanager lives in `observability`
  and is reached on its Service at port 9093. D5's Kustomizations for `loki-homelab`,
  `observability` and `netpol-baseline` keep that edge. Gate: before M5's PR 2a merges.
- **`service="k3s"` for cluster add-ons** (M2 Open question 3). C7 confirms `k3s`, the setup
  role's tag, or names the host. Gate: before M2's PR 4b merges.
- **`service="unmapped"`** (M2 Open question 5). C7 records `unmapped` as the one value
  outside its rule, and names `WorkloadServiceUnmapped`, which pages with
  `service="observability"` alongside it. Gate: before M2's PR 4b merges.

### Requests on the slice document format

- **Deletion PRs** (D6 Open question 6, R4 Open question 11). Exempt a pure deletion from the
  100–200 line guide, or split D6.1, D6.4 and D6.5b by module. R4's PRs 6 and 8 rely on the
  exemption, and R4 names its split if the exemption is refused. No gate is named.

## Review log

### Round 1 — integration review (X-series)

Raised against this page: X-01, X-02, X-03, X-04, X-06, X-07, X-08, X-11, X-12 and X-19.
All are fixed here, and each slice-side half is handed to its slice. Refuted: none.

- **X-01 (high).** D2's publish could not render bento-pdf's `domain`. Fixed: the index gains
  D2 → R2. C1 makes placeholder mode the only publishing mode and refuses a placeholder in a
  tag without `postBuild`. C3 names the source of `domain` before D3, with a default and an
  **operator-only** alternative.
- **X-02 (high).** C2 still read "runs once per master commit". Fixed: C2 promotes the newest
  published master commit T and verifies T. C6's `land.sh` waits until the merge is covered
  rather than on one run.
- **X-03 (high).** `lastAppliedRevision` holds the digest, not the commit. Fixed: C6 names
  `lastAppliedOriginRevision` as the commit carrier. D1's baseline cites kustomize-controller
  v1.9.6 `api/v1/kustomization_types.go` and source-controller v1.9.6
  `OCIRepositoryReconciler.getRevision` for the two forms.
- **X-04 (high).** D3.3's `cluster-vars` broke D2's census. Fixed: C1 and C3 gain shared
  kinds, `SHARED_KINDS`, reserved names and the allowlist census, from D4.3a. The index orders
  D3.3 after D4.3a, and D4.3a after D2.5 and D2.6 only.
- **X-06 (high).** Registry GC had two owners. Fixed: D5 retires it in wave D, and H2 drops
  H2.6a and H2.6b. After D5.4 no build pushes to the in-cluster registry, and an in-cluster
  orchestrator that scales a flux-owned Deployment is the second writer C4 forbids. design.md
  records the decision. The index narrows H2 to H1.1a to H1.8 and adds D5 wave D → H2, which
  avoids the cycle H1.9 → R4 → D6 → D5 → H2.
- **X-07 (high).** R4's verify shapes contradicted D5. Fixed: C7 gains the `verify` label,
  the soak's `flux/soak` status, and the three successor shapes D5 builds.
- **X-08 (high).** Four proofs needed the retired stage cluster. Fixed here: C8 forbids a
  stage gate and names three substitutes. D2's index row reads "on production". The slices'
  own steps change in R4, D6 and H2.
- **X-11 (medium).** Slice amendments were unabsorbed, and C8's own example formed a node id.
  Fixed: each amendment listed below, and C8 describes the node-id shape without forming one.
  Requests this round did not apply are under *Requested amendments not yet applied*.
- **X-12 (medium).** The `$${` rule was split three ways. Fixed: C1 escapes exactly when the
  rendered Kustomization carries `postBuild`, and names that field as the one source. Round 2
  reopened it, because that field does not exist when R2's checks run (X2-09).
- **X-19 (medium).** Nobody removed the Pi's list. Fixed: C4's *Removal* names H1.9, H5 and H7.

**Contract changes in this round.**

- *Slice index.* An explanation of sub-edges. R3 is renamed "Remove the pre-apply live reads"
  (R3 Open question 1). D2 is renamed "One stateless leaf end to end on production". New
  edges: D2 on R2, D2.7 on D4.0 and on D3.3, D3.0 to D3.3 on D2.1 to D2.6 only, D3.3 on
  D4.3a, D4.3a on D2.5 and D2.6 only, D5 wave D on H2.1 and H2's family PRs, M3/M4/M5 on M2's
  PR 3, M6's C-4b on D6, H1.7b on D3, H1.9 on R4, H2.3b and H2.8a on D3, H2.8a on H6.4a and
  H6.4b, H6 on H1.1a to H1.8 and M1, and G4's PR 3 on G2. H2 narrows from H1 to H1.1a to
  H1.8.
- *C1.* The entry point's path, and stub tooling is not a second renderer (R1). The
  `--secret-stubs` mode and the `package` mode (R1, H3). Only placeholder mode publishes. The
  `-deferred`, shared-kind, snapshot, verify and `digests.json` outputs (R1, D4, D5). Vendored
  files and patches (H1). Secret references are cluster mode only, and
  `render_sops_manifests` templates render nothing in placeholder mode (H3, R2). The escape
  rule is conditional on `postBuild`, with R2's wording.
- *C2.* Signing, the promotion target T, verification of T with the name-based expected set,
  the verdict names, and the hold-then-reset order.
- *C3.* The root Kustomization `homelab`. `<tag>-verify` and `<tag>-snapshot`. Per-role
  `prune: false`, `timeout` and `healthCheckExprs`. Shared kinds, reserved names, the
  allowlist census and `deletionPolicy: Orphan`. Ordering edges may come from a named renderer
  rule (D4 Open question 6). The second secret path, the value-encoding rule, and the source
  of `domain` before D3.
- *C4.* *Removal* names H1.9, H5 and H7 and where each list goes.
- *C6.* `gotk_resource_info`'s label reads the origin revision. The origin field names a
  commit and the digest field does not. `land.sh` waits on coverage of the merge, for the
  cluster and for hosts (D2, H3).
- *C7.* The `verify` label, the soak and `flux/soak`, and the three verify-successor shapes.
  Who sets `service` for an exporter (M6 Open question 2).
- *C8.* The node-id example no longer forms a node id. The no-stage-cluster rule.
- *New section.* *Requested amendments not yet applied*.

### Round 2 — integration review (X2-series)

Raised against this page: X2-01, X2-03, X2-08, X2-09 and X2-11. All are fixed here, and each
slice-side half went to its slice as a cross-slice note. Refuted: none. Each claim was
checked against the slice text it quotes before the change.

- **X2-01 (high).** C2 and C6 read "empty" where D1 and D2 write `none`. Confirmed: D1
  *Design* and D1-R10-08 record that `gh variable set` ignores an empty `--body`, and D2 Open
  question 8 adopts `none`. D1 Open question 9 gated the change before D1.3 merges, and this
  round applies it rather than recording it. Fixed: C2 moves `prod` only while
  `DEPLOY_HOLD` reads `none`. A new C2 row says that any other non-empty value holds and that
  an empty or missing value is an error, and C2 names the clear command. C6 ends the wait on
  a value other than `none`. H1's land rule, D5's `promote-reset` refusal and the D4, H1 and
  H6 checks and clear steps change in those slices.
- **X2-03 (high).** C3's value rule did not exclude `{{`, which authelia's template filter
  reads. Confirmed in R2 *Risks* and D3 Open question 6. Fixed: C3's value rule names `{{`,
  and says that `cluster_vars.py write` refuses each excluded value by key name. The
  refusals for the three `_argon2` digests and for a `:` in `mqtt_password_hash` are
  per-key rules, so they stay in D3, which D5's preconditions and R4's successor list cite.
- **X2-08 (high).** M6's live drill needs an Ansible-owned stateless leaf, and D5 flips them
  all with no edge to the drill. Confirmed in M6 *The health-gate drill* and the D5 index
  row. Fixed by the index sub-edge, not by the redesign. M6 Open question 4 rejects a
  flux-owned drill tag, because one shared selector cannot exclude `drill` for one reader
  only. D5's row gains "The PR that flips the last Ansible-owned stateless leaf also M6's
  B-1e". A paragraph below the table shows that the edge adds no cycle.
- **X2-09 (medium).** C1 decided substitution per tag, from the rendered Kustomization, while
  R2 and D3 decide per directory. Confirmed: R2 Open question 5 shows that C1's source does
  not exist when R2's checks run. X-12 is reopened and fixed again. C1 *Substitution and
  escaping* takes the per-directory unit, the in-process key set as the one source and
  `spec.postBuild` as its projection. *Only placeholder mode publishes* and *Vendored files*
  take the same unit. R2's third request, the *Modes* sentence on stub mode's readers, is
  applied together with R4 Open question 9's second reader. R2's fourth request, H1's
  per-tag flag, is H1's text to change, gated before R2's PR 1 merges.
- **X2-11 (medium).** The register and the index lacked requests that slices name as merge
  gates. Confirmed against each slice's *Open questions*.
    - *Applied, because the contract contradicted the slices.* D4 removed D4.3a in its round
      10 (D4-R10-01), and D2.5 and D2.6b own what C2, C3 and the index credited to it (D2
      Open question 6, D3 Open question 5, D4 Open question 5). D5.1a1 could not add `soak`
      and `homelab-namespace`, because D2.6b's census fails a member with no file (D5 Open
      question 16). D5.1a2, not D5.1a1, adds `digests.json` (D5 Open question 10). H6 Open
      question 6 withdrew "C2, a reset that lowers the Plans", because H6.1b's
      `flux_forward_only: true` puts the Plans under H1's hold.
    - *Applied as index sub-edges.* D2 on R2's PRs 1 and 2 only (R2 Open question 5, D2's
      header). traefik's flip and D5's flux PR on H1.1a (D5's header). D5's last
      stateless-leaf flip on M6's B-1e (X2-08). M6's A7 shadow PR on M5's live ruler, and its
      Cutover-a on M2's PR 4a deployed (M6 *Order*, M2 Open question 6). H2.3c on H2.3b
      deployed (H2 Open question 1).
    - *Applied with no contract text.* M5 Open question 4's handoff to M6 changes slice
      counts, not a contract row. M6 already counts three rows and three tokens from M5, and
      design.md's M5 row follows it this round. M6's first Cutover-1a has no gate left from
      it.
    - *Recorded under Requested amendments, each with its gate.* D1 Open question 7; D2 Open
      questions 6 and 7; D3 Open question 6; D4 Open question 7; D5's C2 row, which now
      lists the observe-only set, its C3 row on `prune: false` and its C6 row (Open question
      18); D6 Open questions 7 and 12; H2 Open questions 1 and 12; H1 Open question 3's full
      text and its second gate, before H1.1i; H3 Open question 7; H4 Open questions 4 and 6;
      H7 Open question 1; M2 Open questions 1, 3 and 5; M5 Open questions 1 and 2; R4 Open
      question 10. M6's C7 row now counts six departures, not five. The deletion-PR row
      names D6.5b and R4's PRs 6 and 8.
    - *Not changed here.* H6's tag `timeout` is 100 minutes (H6 Open question 5). contracts.md
      states no timeout value, so the worst case that takes it is D4's to restate.

**Contract changes in this round.**

- *Slice index.* The *live* marker. D2 on R2's PRs 1 and 2 only. D3.3 → D4.3a and D4.3a's
  own edge removed. D5 gains traefik's flip and the flux PR on H1.1a, and its last
  stateless-leaf flip on M6's B-1e, with a paragraph on that edge. M6 gains A7's two
  sub-edges, and H2 gains H2.3c on H2.3b.
- *C1.* Stub mode's readers. The per-directory substitution unit, its one source and its
  projection, in *Substitution and escaping*, *Only placeholder mode publishes* and
  *Vendored files*. `digests.json` dates from D5.1a2.
- *C2.* `DEPLOY_HOLD` reads `none` for no hold, a new row for its values, and the clear
  command. *Verification* credits D2.5.
- *C3.* `SHARED_KINDS` joins name D2.6b, D3.3a, D5.3b2 and D5's namespace PR. D5.1a1 reserves
  `soak` and `homelab-namespace`. The census credits D2.6b. `homelab-cluster-vars` feeds
  each Kustomization that substitutes, the per-directory unit. The value rule excludes `{{`
  and names the refusing tool.
- *C6.* The wait ends on a `DEPLOY_HOLD` other than `none`.
- *Requested amendments.* Grouped by contract. Rows added and corrected as X2-11 lists, and
  H6's withdrawn row dropped.

### Cross-slice follow-up (2026-10-12)

- Cross-slice note from D6, sent twice: *Requests on C1*, *The commit input*, is now gated
  before D6.1 or R4's PR 1, whichever merges first, because D6.1's `--moved-tags` mode passes
  the head's SHA to both renders and R4 waits on D6.
- Cross-slice note from D6: C3 *Shared kinds* says that each member needs a
  `--moved-tags` rule from D6.1, and that a census test enforces it.
- Cross-slice note from H1 to D5: the D5 index row adds H1.7b as a gate of the flux PR,
  following D5's header.
