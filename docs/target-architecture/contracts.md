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
D4.0" says that one PR waits on one PR of another slice, and the slice's other PRs do not.
Sub-edges keep the graph acyclic where two slices interleave: D2 with D3 and D4, D5 with H2,
and D5 with M6. A sub-edge marked *live* waits until the earlier PR is deployed, not only
merged.

| ID | Slice | Depends on |
|---|---|---|
| R1 | Shadow render with zero diff | — |
| R2 | Secret placeholders and CI secret checks | R1 |
| R3 | Remove the pre-apply live reads | R1 |
| R4 | Delete the k8s Ansible path | D6, per PR. R4.1 needs only D6.12 and D5's tail, R4.5 also D6.18, and R4.7 the whole of D6 |
| D1 | Flux installed, owning nothing | —. D1.6b, the `flux` rule group, also M1 |
| D2 | One stateless leaf end to end on production | D1, R1, and R2's PRs 1 and 2 (placeholder mode and the five rules). D2.6d also M1. D2.7 also D4.0, M1, and PR 2b of R2's *Publish `domain`* item (C3) |
| D3 | Secrets through Flux | D2, R2. D3.0 to D3.3 need only D2.1 to D2.6c, which include D2.6b's empty `SHARED_KINDS` and census |
| D4 | Ordering (`dependsOn`, deferred applies) | D2. D4.0 needs nothing. D4.9 also D1.6b and M1 |
| D5 | Bulk move | D3, D4, R3, M1, M2. traefik's flip and the flux PR also H1.1a, and the flux PR also H1.7b. Wave D also H2.1, and the H2 family PR of each role H2 moves. littlelink's flux PR also M6's B-1e. monitor-bridge's flip also M6's A6-7a, live. No flip waits on M6's C-1: the operator ruled on 2026-10-21 that uptime-kuma flips with janitorr and jellyfin, and M6's Phase C works on flux-owned roles (#5821) |
| D6 | Delete the old deploy path; ADR superseding 0010 | D5, and M6's C-2b, live. D6.11 needs only D5.1a2 and D5's flux PR; #5425 dropped D6.10 |
| M1 | Alertmanager and its watcher | — |
| M2 | One bridge family to rules | M1 |
| M3 | One host timer to the textfile collector | M1. The cutover also M2's PR 3 |
| M4 | blackbox_exporter for the ingress probes | M1. PR 3 also M2's PR 3 |
| M5 | loki-homelab ruler | M1. The cutover also M2's PR 3 |
| M6a | Retire Kuma, Phase A: drain the producers | M2, M3, M4, M5. A7's shadow PR also M5's ruler, live; A7's Cutover-a also M2's PR 4a, live |
| M6b | Retire Kuma, Phase B: switch the readers | M6a's A6. B-1, B-1d and B-1e need only M1 and C7's health-gate selector |
| M6c | Retire Kuma, Phase C: delete Kuma | M6a, M6b. C-1a also D5's registry PR; C-4b also D6 |
| H1 | Hand the cluster content to Flux | D2, D4. H1.7b also D3; H1.9 also R4 |
| H2 | Shrink the host plane (crons → CronJobs) | —. H2.2a also H1.5a and H1.6; H2.3b also H1.5a; H2.3c also H2.3b, live; H2.8a also H1.7b and H6.4b |
| H3 | Pi pilot: builder, updater, versioning | R1. H3.5d, the host-rules PR, also M1, and H3.9b waits on it |
| H4 | Secrets unit; NUT on daniel-server first | H3 |
| H5 | Pi Compose | H3, H4 |
| H6 | system-upgrade-controller for k3s | D2, H1.1a to H1.8, H4, M1. H6.3e's verify Jobs also D5.1a1 (H6 Open question 14) |
| H7 | Remaining roles, then delete Ansible | H2, H5, H6, R4, per PR (H7's *Waits on* column). H7.21 to H7.23 need only H3, H4.3b2, D1 and D5.1h. The Pi PRs need H3, and H7.26 also H5.12. daniel-server's PRs need H4, daniel-box's need H6.2, and H7.24 needs all three. A PR that empties a `k8s_host_files` row also needs the D5 PR that added it. H7.34 also H6.1b2, H6.5b and H2; H7.35 also H6.4b and H2; H7.42 also H6.1b; H7.44 also R4 PR 7b and H6. H7.37 and H7.49 to H7.51 also D6. H7.43 and H7.45 to H7.57 wait on the whole of each slice they name |
| G1 | Diff notice alongside the lock | — |
| G2 | Retire the lock; ADR amending 0019 | G1 |
| G3 | (optional) Notice dispatches the freshness reviewer | G2 |
| G4 | Drop the CI size test | —. The optional PR 3 also G2 |
| G5 | Shrink root CLAUDE.md | G2 |

H2's edges are per PR (H2 Open question 1). H2.2a waits on H1.5a and H1.6, H2.3b on H1.5a,
and H2.8a on H1.7b and H6.4b, which reach D3 and H6.4a through those two. Every other H2 PR
waits on nothing outside H2. No H2 PR waits on H1.8 or H1.9. H1.9 waits on R4, R4 on D6, and
D6 on D5, whose wave D waits on H2.1 and H2's family PRs, so a dependency on H1.9 would be a
cycle. The H2 PRs that D5 waits on wait on nothing outside H2.

D5's edge to M6's B-1e keeps one Ansible-owned stateless leaf, littlelink, alive for M6's
live health-gate drill. The drill's tag must not be flux-owned, because D5's soak and
`probe.py health` share one selector (M6 Open question 4). The edge adds no cycle. B-1, B-1d
and B-1e wait on M1 and on C7's health-gate selector alone, and neither waits on D5. Only
M6's C-4b waits on D6, and B-1e does not wait on C-4b. The edge does put M6's B-1 on D5's
path to its exit when M6 finishes later. If M6 moves the drill to a tag the soak may own,
which needs a drill exclusion in C7 that no slice has requested, the edge goes.

D5's other edge to M6 keeps monitor-bridge Ansible-owned until A6-7a, an M6 step that needs
`land.sh`'s order, has deployed (M6 *When D5 flips a role M6 edits*). It adds no cycle either.
A6-7a waits on Phase A batches and on M2 to M5, and none of those waits on D5. M6's C-1a
waits on D5's registry PR, which waits on no M6 step.

The roles M6's Phase C orders, observability, netpol-baseline and uptime-kuma, flip before C-1
(#5821). The operator ruled on 2026-10-21 that holding uptime-kuma for C-1 put D5's last three
PRs behind Phase C. Flux orders C-1's steps instead: netpol-baseline's `dependsOn` reaches
uptime-kuma, and C-1 splits into C-1a and C-1b because observability and uptime-kuma share no
edge. D6's wait on C-2b was a consequence of uptime-kuma never flipping, and D6's row now
states it. It adds no cycle: C-2b waits on M6a, M6b and C-1, and none of those waits on D6.
C-4b waits on D6, but nothing from C-1 to C-7 waits on C-4b.

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
  - `homelab-render cluster --out DIR --shared-only`, from D3.3a. It writes the shared kinds
    and the root, and no tag, for a publish with no flux-owned tag. With `--all-images` it
    also writes every image context (#5593).
  - `homelab-render host --host NAME --out DIR`
  - `homelab-render package --name PKG --out DIR`, for the shared packages of C5 (H3).
- **Only placeholder mode publishes.** The publish job runs `cluster` without
  `--secret-stubs`, and its command says so. It refuses a bundle in which a Kustomization
  without `postBuild` has a directory that holds `${<key>}` for a key name of `secrets.yml`.
  That refusal is the check that stops an unsubstituted secret placeholder before `prod`
  moves.
- **Cluster output:**
  - `DIR/cluster/<tag>/` holds the role's manifests and a `kustomization.yaml` that lists them.
  - `DIR/cluster/<tag>-deferred/` holds the files the role passes as
    `manifests_deferred_files` (R1).
  - `DIR/flux/<tag>.yaml` holds the Kustomization `<tag>` and its suffixed kinds, see C3.
    The Kustomization `<tag>` carries the annotation `homelab/content-digest`, the tag's
    `own` digest from `digests.json`, which C6 exports and C7 reads (#5338).
  - `DIR/flux/<name>.yaml` and `DIR/cluster/<name>/` hold a shared kind, see C3.
  - `DIR/flux/kustomization.yaml` lists every file in `DIR/flux/`.
  - `DIR/README.md` at the bundle root, a static note on the layout. No Kustomization's path
    reaches it, and it counts for no name (see `_unclassified` below).
  - From D5.1a1, `DIR/cluster/<tag>-snapshot/` and `DIR/cluster/<tag>-verify/`. From #5525,
    `DIR/cluster/<tag>-prepull/`. From #5719, `DIR/cluster/<tag>-precheck/`.
  - From D2's first publishing PR, `DIR/digests.json` at the bundle root. No Kustomization's
    path reaches it. It maps each deploy tag and each member of `SHARED_KINDS` to an object.
    Every object holds two SHA-256 digests, and from D5.1a2 a deploy tag's object holds two
    more fields (D5):
      - `own` covers `cluster/<name>/` alone. D5 reads it.
      - `footprint` covers every bundle path that counts for the name. A tag's own
        directories count, with a reserved suffix stripped. So do its `flux/<tag>.yaml`, and
        the `cluster-vars` files when the tag substitutes. A shared kind's files count for
        the names its footprint rule lists (C3). `soak`'s footprint is normalised over the
        revision's SHA, so the commit alone never moves it.
      - `claims` lists the snapshot claims the tag declares.
      - `suspended` is the tag's rendered suspend flag (C3 *Suspension*).

    Three index paths count for no name and are left out of every footprint:
    `flux/kustomization.yaml`, `digests.json` itself, and `images/`. A tag that joins or
    leaves moves its own `flux/<tag>.yaml`, so the index adds nothing to the footprints.
    Any other path that counts for no name is recorded under the reserved key
    `_unclassified`, and a change there counts as every name changed. The renderer computes
    both digests in the render's own process, and no reader recomputes them. C2's changed
    set and C6's `land.sh` wait read `footprint`. These rules replace D6.12's `--moved-tags`
    mode. They apply the classification that D6's *What a footprint counts* defines.
  - From D5.4a1, `DIR/images/<name>/` holds each rendered image context, and
    `DIR/images/tags.json` maps each name to its content tag. A render of every tag, and a
    render with `--all-images`, holds all ten. A `--role` render holds the contexts of the
    named tags' roles alone, and no `DIR/images/` when none of them builds an image, so each
    named consumer still reads its own tag from it (#5374). No Kustomization's path reaches
    `DIR/images/`. The content tag is `sha-<12 hex>` over the context's `data`, as
    `image_builder_content_tag` computes it. A flux-owned consumer's image ref reads that tag
    from the same render, never from `render_guard.py:BUILT_IMAGE_TAG_STUBS` (D5).
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
- **Jobs that run repository code hold no write permission.**
    - The `render` job checks out the commit, renders it, and runs the publish checks: the
      census, the no-stub check, and gitleaks over `out/`. It holds read-only permissions,
      `contents: read` and `checks: read`, and passes the bundle on as a workflow artifact.
      `checks: read` serves the CI re-check of a dispatched `sha` (D2.5).
    - The `promote` and `verify` jobs run `promote.py` read-only and print a verdict (D1,
      D2). `verify` holds `contents: read` and `statuses: read`, for T's commit statuses.
    - Only jobs that check out no repository code hold `packages: write`. Those are
      `publish`, which pushes and signs; `tag`, which moves `prod`; and `settle`, which
      moves `last-good` or writes the hold and resets `prod`. Each runs only pinned
      binaries, either `flux` and `cosign` or `flux` and `gh`.
    - Only `publish` holds `id-token: write`.

  Code that runs during a render or a decision therefore cannot sign or move a tag (D1,
  D2).
- **Only master commits publish.** Before it renders, `render` checks that the commit
  descends from `origin/master`, using `git merge-base --is-ancestor`. The `sha` dispatch
  input passes the same check (D1).
- **Tags:**
  - `sha-<40-hex>` for every green master commit;
  - `prod`, the revision Flux follows;
  - `last-good`, the newest revision whose statuses all passed;
  - `held`, the revision that a hold without a reset left as `prod` (H1.1b).
- **Promotion target.** `promote` is a GitHub Actions workflow. Its `promote` and `tag` jobs
  share the job-level concurrency group `promote`, with `cancel-in-progress: false`. It runs
  after each green CI run on master. GitHub may evict a queued run, so a run is not tied
  to one commit. Each run promotes the newest published master commit: `promote.py decide`
  walks `origin/master` from its tip back to `prod`'s commit and takes the first commit T with
  a published `sha-<T>` tag, a signature that verifies and a green CI verdict. It prints
  `promote`, `held`, `current`, `deferred` or `error`, and moves `prod` to T only when:
  - the repo variable `DEPLOY_HOLD` reads `none`;
  - T descends from the current `prod`;
  - T passes *The forward-only gates* (H1); and
  - the run is outside *The reboot window* (D5).
- **The forward-only gates.** `prod` moves to T only when every commit in (`last-good`, T]
  that changes a forward-only pin maps to a PR whose head carries a successful
  `homelab/upgrade-gates` status. A commit with no PR fails that test, and a failed lookup
  ends the run as `error`. `prod` also stays when (`prod`, T] changes one forward-only pin in
  more than one commit, moves `k3s_longhorn_version` more than one minor, or carries a
  required gate status older than six hours. A tag that T drops counts as changed (H1).
- **The reboot window.** No promotion moves `prod` inside the derived reboot window. A run
  inside it ends `deferred` without waiting, and a derived `schedule:` run promotes at the
  window's end (D5).
- **A change to a suspended tag holds.** `decide` holds a revision R that changes the `own`
  digest of a tag suspended at both `last-good` and R. It writes `DEPLOY_HOLD` as
  `<R> suspended-changed=<tag>`, naming the tag (D5).
- **`DEPLOY_HOLD`'s values.** `none` means no hold. Any other non-empty value is a hold, so a
  mistyped hold still holds. An empty value or a missing variable is an error: `decide`
  prints `error`, exits non-zero and moves no tag. `gh` cannot set an empty value, so D1
  creates the variable as `none` (D1).
- **Verification.** `promote.py verify T` checks T, the commit `decide` printed, never the
  commit that triggered the run. It expects `flux/homelab` plus `flux/<name>` for every name
  in T's *changed set* (D2.5).
    - **The changed set** holds the names whose `footprint` digest (C1) differs between T's
      `digests.json` and the one on `prod`'s commit before the move. It also holds every
      Kustomization whose `dependsOn` chain reaches one of those names, and each per-tag
      kind a moved tag depends on (`-snapshot`, `-prepull`, `-precheck`), whose files count
      in that tag's footprint and which no chain reaches (#5719). Suspended
      Kustomizations are left out, and so is a name absent from T's `digests.json`, which
      T removes. Any change under `_unclassified` puts every name in the set.
    - **`soak` joins any set that holds a tag.** `soak` carries no `dependsOn` edges (D5
      Open question 19), so no chain puts it in the set. The set holds `soak` whenever it
      holds a tag, and the soak Job waits at run time for each Kustomization in its scope
      to read Ready at T by `status.lastAppliedOriginRevision` (C6).
    - **A dependent that was already failing** is reported, not expected. That is a
      dependent whose `flux/<name>` status on `prod`'s commit already read `failure`.
      `decide` records those statuses before it moves `prod` (H1.1i).
    - **An empty changed set** expects `flux/homelab` alone, and the soak (C7) passes it
      without waiting.
    - **Failures outside the set** never hold. An unchanged Kustomization that fails pages
      through the Flux rule group (C7).
    - **The observe-only set.** From D5.3b2 until D5.3b3, `verify`'s observe-only set leaves
      out `soak` and ignores a `flux/soak` `failure`, so the first live soaks neither hold nor
      reset. D5.3b3 deletes the set (D5).

  A missing expected status keeps `verify` waiting and never reads as success. When every
  expected status is `success`, `last-good` moves to `sha-<T>`.
- **On failure**, the `settle` job writes a hold to `DEPLOY_HOLD`, then resets `prod` to
  `last-good`, except under *A hold without a reset* below. The hold is written first, so a
  failed reset still stops the next promotion.
  The hold takes one of two forms (D2.5):
    - `<T> failed=<names>` when `verify` printed `failed`, meaning a `failure` status or
      `promote.py`'s own derived timeout. `<names>` comma-joins the expected names that read
      `failure` or had no status at the timeout.
    - `<T> unverified` when `verify` ended with no verdict: the job crashed, hit its
      `timeout-minutes`, or was cancelled. A `failed` verdict whose names are not a
      comma-joined list of lowercase names also holds in this form.

  `<T> unverified` is a hold under *`DEPLOY_HOLD`'s values*, because it is neither `none`
  nor empty. Other writers of `DEPLOY_HOLD` keep their own values (H1, D5, H6), and none of
  them uses the `failed=` or the `unverified` form. D5 writes `<T> stateful=<names>`,
  `<T> stateful=unverified` and `<R> suspended-changed=<tag>`, and H1's forward-only hold
  writes T's SHA alone.
- **A hold without a reset.** A failed revision that changes a forward-only tag (H1) or a
  snapshot role (D5) writes `DEPLOY_HOLD` and leaves `prod`. D5's hold reads
  `<T> stateful=<names>` or `<T> stateful=unverified`. D5 writes `<T> stateful=unverified`
  when `verify` left no verdict or its names were malformed; it is D5's own value, not C2's
  bare `<T> unverified`. The operator accepted the hold in principle on 2026-10-16, and
  confirmed its two co-batched cases and the `stateful=unverified` value on 2026-10-17 in
  D5.2b1's review (D5 Open question 1). Each of these holds
  clears only by hand. Its `settle` tags T as `held`, D5's stateful hold included, because
  `last-good` stays on the older revision while `prod` names T.
- **The hold post.** From D5.2b1, a hold posts to the deployer's Discord channel, with a
  command block per changed snapshot role (D5).
- **A verification in flight blocks the next promotion.** `verify` and `settle` join no
  concurrency group. GitHub keeps one pending job per group, and a newer one cancels it, so
  a pending `settle` in the group `promote` could be evicted with its hold unwritten. Two
  checks guard the gap instead (D2.5):
    - `prod` and `last-good` name the same digest except between a run's `tag` and its
      `settle`, and after a hold without a reset. While they differ, `decide` waits, polling
      every 30 seconds, and gives up as `current` after 40 minutes. A `prod` that names the
      `held` digest is not in flight, so `decide` does not wait on it once the operator
      clears the hold. That wait outlasts `verify`'s and `settle`'s
      `timeout-minutes` together, which D2.5 sizes for bento-pdf's 13-minute timeout. A
      longer derived timeout raises `verify`'s `timeout-minutes`, this wait and the `promote`
      job's `timeout-minutes` together, all inside GitHub's 6-hour job limit (D4 *The
      cap*).
    - `tag` moves `prod` only while `prod` still names the digest `decide` read. A second
      run that decided on the same `prod` finds it moved, and it moves and verifies nothing.
      The run that did move `prod` makes up for it (#5022). Once it applies its verdict, its
      `settle` dispatches one more `promote` run with the `sha` input, when CI's newest
      green push run on master is for a commit newer than its T. A dispatched run that
      promotes that commit finds no newer one, so the chain ends there.

  Unguarded, a second run could move `prod` past a T that `verify` still waits on. Flux
  would then never post T's statuses, and the timeout would write a false hold. A run that
  never settles stalls promotion as `current`, and D2.6d's `PromotionStalled` alert pages it.
  `scripts/deploy_tools/promote_lib/verify_promotion.py` carries the decision as the marker
  `DECIDED: a verification in flight is guarded by two checks`. Any other job that moves
  `prod` or `last-good`, such as D5's `reset_to_last_good` dispatch, must hold both checks
  true or state why it may break them. *The reset input* holds both.
- **The reset input.** A `reset_to_last_good` dispatch input moves `prod` to `last-good` and
  does nothing else. It runs in its own concurrency group, `promote-reset`, and ignores the
  reboot window. It refuses unless `DEPLOY_HOLD` holds a hold: `none`, an empty value and a
  missing variable each refuse. It holds both checks above true
  ([#5079](https://github.com/DanielH2018/server/issues/5079)):
    - It refuses while `prod` differs from `last-good`, unless `DEPLOY_HOLD`'s first word is
      a SHA X whose `sha-<X>` digest equals `prod`'s, which means `prod`'s run has settled.
    - It moves `prod` only while `prod` still names the digest it read, as `tag` does.

  D2's `settle` is unchanged. D5 *A reset input* has the argument and the one residual race
  under the revert rule (D5).
- **Clearing a hold.** Clearing `DEPLOY_HOLD` means setting it back to `none` with
  `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`. Clearing by hand is
  **operator-only**, and an agent hook denies that command (D1). *The revert rule* below is
  the one exception.
- **The revert rule.** The operator adopted this rule on 2026-10-16, with the `soak`
  exclusion below (D5 Open question 17).
    - While `DEPLOY_HOLD` reads `<T> failed=<names>`, `decide` may promote a candidate U that
      descends from T. For every name in `<names>`, U's `footprint` digest must equal
      `last-good`'s.
    - When U verifies, `settle` sets `DEPLOY_HOLD` to `none`. So a merged revert of the
      failing change clears its own hold, and the operator need not act.
    - A hold in any other form clears only by hand, `<T> unverified` included. So does a
      `failed=` hold whose names include `soak`. The soak's verdict covers every service in
      the changed set, so no single footprint can show that the failure was reverted.
    - Until `decide` implements the rule, every hold clears only by hand, and `decide`
      treats a `failed=` hold like any other hold.
- **A `prod` move outside `promote`.** Each time `promote` moves `prod`, it writes the new
  digest to the repo variable `PROD_DIGEST`. The deploy-pipeline exporter (C7) compares that
  value with `prod`'s live digest, and a mismatch pages (D2).
- **Break-glass promotion.** This path is for when GitHub, GHCR or Sigstore cannot be
  reached (**operator-only**).
    1. The operator renders from the break-glass repo bundle, or from a host checkout newer
       than the bundle, once its commit is checked to descend from the OCIRepository's
       revision.
    1. They push the bundle to the in-cluster registry.
    1. They point the OCIRepository at it, with verification against a break-glass cosign
       public key that the cluster already holds.

  D6 owns the runbook and drills it on kind. Normal promotion resumes once the
  OCIRepository points back at GHCR.
- **Commit statuses.** notification-controller posts one status per Kustomization, with context
  `flux/<kustomization>`, on the commit that the origin revision names (C6).

## C3. Flux objects

- **Namespace.** Everything lives in `flux-system`, with one `OCIRepository` named `homelab`.
  The one exception is `verify-drill`, a transient second `OCIRepository` in `flux-system`.
  It is present only while `flux_verify_drill` is true, and the flux role deletes it while
  the lever is off (D1).
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
  - D5.1a1 adds `<tag>-snapshot` for a role that declares snapshot claims. It inherits
    `<tag>`'s `dependsOn`, less the edge to itself (D5).
  - #5525 adds `<tag>-prepull` for a role that sets `flux_prepull_timeout`, the kind D5's
    Open question 21 asks for. Its Jobs pull the tag's images under its workloads' own
    placement, so a `Recreate` rollout pays only the container start. It inherits `<tag>`'s
    `dependsOn` the same way, `<tag>` depends on it, and it never depends on `<tag>-snapshot`
    or the reverse. Its `timeout` is the role's `flux_prepull_timeout`. Its document follows
    `<tag>-snapshot`'s and precedes `<tag>-verify`'s.
  - #5719 adds `<tag>-precheck` for a role that ships `templates/precheck/`, its own check
    Jobs that must pass before `<tag>` applies, such as pihole's sibling guard. It inherits
    `<tag>`'s `dependsOn` the same way, and `<tag>` depends on it. It also depends on the
    role that creates a namespace one of its documents names, so a precheck in a namespace
    its own tag creates is a cycle and fails the render. Its `timeout` sits 60 seconds above
    its Jobs' longest `activeDeadlineSeconds`, as `<tag>-verify`'s does. Its document follows
    `<tag>-prepull`'s and precedes `<tag>-verify`'s. The Job census admits a `precheck` Job
    there, with `backoffLimit: 0`.
- **Per-role fields.** Five fields of a role's own Kustomization vary by role. A role may set
  four of them from its defaults:
  - `prune: false`, for a plumbing tag (H1) or a tag whose render holds a
    CustomResourceDefinition (D5), through H1.1a's `flux_prune`. D5 sets it on
    `traefik-crds`, the tag that carries traefik's CRDs, and on `flux`.
  - its own `timeout` (H1, H6).
  - `healthCheckExprs` for its own custom resources (H6).
  - `suspend: true`, through `flux_suspend` (D5.1b). *Suspension* states how it renders.

  The fifth, `decryption: {provider: sops, secretRef: {name: sops-age}}`, is derived from a
  non-empty `render_sops_manifests` and never set by hand (D3.3c). The shared kind
  `cluster-vars` also sets it. No other field varies by role.
- **Suspension.** A role's `flux_suspend: true` renders `spec.suspend: true` on `<tag>` and
  on each of its non-tag Kustomizations (D5.1b). The renderer writes `spec.suspend` only when
  it is true, on every Flux object. The renderer drops every `dependsOn` edge that names a
  suspended Kustomization. A hand suspend or resume of a Flux object patches `spec.suspend`
  with the field manager `homelab-operator`, which kustomize-controller leaves alone. A hand
  apply of whole objects keeps kubectl's default manager, so Flux takes its fields back (D5).
- **Shared kinds.** A Kustomization that belongs to no deploy tag is a shared kind. The
  renderer names every shared kind in a closed literal set, `SHARED_KINDS`. D2.6b adds the
  set empty, D3.3a adds `cluster-vars`, D5.3b2 adds `soak`, and D5's namespace PR adds
  `homelab-namespace`. Each joins in the PR that renders it, because the census fails a
  member with no file. C1 says where a shared kind is published, and C2 verifies it through
  `flux/<name>` like a tag. Each member also needs a footprint rule in the renderer that
  names the deploy tags its files count for (C1 `digests.json`), and a census test fails the
  PR that adds a member without one (D2). `homelab-namespace` owns the `homelab` Namespace
  and its LimitRange (D5).
- **Reserved names.** The render fails on a deploy tag named `homelab`, the root
  Kustomization's name. It also fails on a deploy tag that equals a member of `SHARED_KINDS`
  or ends in `-deferred`. D5.1a1 reserves `-snapshot`, `-verify`, `soak` and
  `homelab-namespace` too, so no deploy tag takes `soak` or `homelab-namespace` before its
  kind joins the set. #5525 reserves `-prepull`, and #5719 `-precheck`.
- **Publish census.** The files under `flux/` must equal the flux-owned tags plus the members
  of `SHARED_KINDS`, and each tag's file holds exactly `<tag>` and the suffixed kinds its
  role's inputs call for. The census computes this allowlist from the renderer's inputs, never
  from its output, so a renderer bug cannot widen it (D2.6b). A mismatch fails the publish,
  and nothing is pushed.
- **Deletion policy.** Every Kustomization the bundle publishes sets `deletionPolicy: Orphan`.
  D6.20 switches the per-tag Kustomizations to the default policy, if the operator approves it
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
    `render_sops_manifests` entry, which kustomize-controller decrypts (R2, D3). Each SOPS
    manifest carries `kustomize.toolkit.fluxcd.io/substitute: disabled` in its plaintext
    metadata, written before encryption, and the renderer copies it byte for byte.

  Any step in either path that needs plaintext is **operator-only**. `sops-age` is the only
  Secret made by hand.

  **Standalone values.** A *standalone use* is a placeholder that is the whole scalar of a
  field. A key with a standalone use holds a value that both a YAML 1.1 resolver and the
  pinned Flux CLI's reader read as a string, never as an int, float, bool, null, timestamp
  or sexagesimal number. A YAML 1.1 resolver alone is not enough: PyYAML reads `1e3` as a
  string, and the pinned CLI returned the integer 1000. Flux types a standalone value by its
  text, whatever quotes the template wrote. R2's shared resolver holds the definition, and
  the corpus in R2's flux probe holds that resolver to the pinned CLI. The producer
  enforces the rule where it decrypts. Before D3 that producer is R2's guard in the Ansible deploy path.
  From D3 on, `cluster_vars.py write` enforces it too. Both fail closed and print key names
  only (R2, D3). A key whose real value breaks the rule leaves the placeholder path for a
  dedicated SOPS manifest, as `mullvad_account` does (R2 PR 7b).
- **The source of `domain`.** The operator ruled on 2026-10-14 that `domain` is published
  (*Operator ruling (2026-10-14)*). The renderer writes `domain` as a literal.
  - `domain` stays an Ansible variable, defined in `ansible/inventory/group_vars/all.yml`, so
    it can change in one place.
  - No render substitutes `domain`, so a route needs neither `homelab-cluster-vars` nor the
    `cluster-vars` edge.
  - D2.7 waits on PR 2b of R2's *Publish `domain`* item, which makes `domain` a published key.
    D2.7 waits on no PR of D3.
  - **Rejected:** `domain` from `homelab-cluster-vars`, the former default. It made D2.7 wait
    on D3.3 and put every routed tag behind decryption, for a value that is not secret.
  - **Rejected:** a Secret the operator creates by hand. D3 would have to adopt an object that
    no commit describes, and D3.5 shows that Flux removes keys only for the field managers it
    takes over.

## C4. Ownership handoff (the two-writer rule)

- **Ownership flag.** A `containers_list` entry gains `owner: flux` in the same PR that adds
  its Kustomization.
- **Values of `owner`.** `owner` takes `flux` or `released`. `released` leaves the tag out of
  the bundle while every Ansible path still skips it. A handback is two PRs: `released`, then
  the removal of `owner` once the tag's Kustomization reads `NotFound`.
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
- **Version.** `0.<git rev-list --count --first-parent <chosen>>+g<12-hex sha of <chosen>>`,
  with `Architecture: all`. `<chosen>` is the commit the builder builds: the newest commit on
  `origin/master`'s first-parent chain with a green CI verdict, which can be older than the
  tip. A count taken from the tip would give a commit and its successor the same number, and
  dpkg would then order the two by SHA.
- **Repository.**
  - Suite `homelab`, component `main`, keyring `/usr/share/keyrings/homelab-archive-keyring.gpg`.
  - Served by a plain systemd unit on daniel-box, outside k3s. H3 picks the port and the URL.
- **Architecture-specific tools.** `homelab-tools` is the one architecture-specific package,
  built for `amd64` and `arm64` by nfpm from pinned upstream binaries, `sops` and `chezmoi`
  among them. Every other package is `Architecture: all`. H3 builds it, and the Pi is its
  `arm64` pilot (H3, H4, H7).
- **Ubuntu releases.** No package depends on one Ubuntu release. H3's container suite runs on
  every release the fleet runs or is moving to, and H7 owns the release-upgrade procedure.
- **Repository freshness.** The signed `Release` file carries a `Valid-Until` seven days
  ahead, and the signer re-signs it daily, so a host cannot be held at an old index without
  noticing (H3).
- **Signing.**
    - Only the LAN builder signs. CI builds the same packages for diffs and tests but never
      signs.
    - The builder builds only a commit on `origin/master` that has a green CI verdict. That
      is the anchor the GitOps deployer trusts.
    - The builder requires no commit signature and no landing tag.
      [ADR-0021](../adr/0021-the-host-package-builder-trusts-master-plus-green-ci.md) records
      the operator's choice on H3 Open question 1.
    - The signer signs only `homelab-*` packages whose version names the commit the builder
      chose. It refuses anything else in the staging directory.
    - The builder runs under systemd sandboxing. Code that runs as the builder still decides
      what the signer is handed, and H3's *Risks* says so.
- **Staged rollout.** A change that republishes more than one host's package reaches H3's
  canary host first. The other hosts' packages publish once the canary reports
  `homelab_host_reconcile_ok 1` at that version, or when the operator overrides (H3).
- **Package rules.** No conffiles, and enable links ship as package files. The postinst calls
  only `systemctl`. Packages carry SOPS ciphertext only. Each host package carries one
  per-host SOPS file, and `homelab-secrets.service` decrypts it with the host's own age key
  into one of three places (H4):
    - `/run/credstore`;
    - `/run/homelab-secrets/<path>`;
    - for a persisted output, `/var/lib/homelab-secrets/<path>`.

  A consumer reaches a decrypted file through a symlink at the real path, or through a
  drop-in when another package owns that path. A persisted output is plaintext at rest, so a
  slice adds one only for a consumer that must start before the decrypt can run, and H4
  names each one. A lost host key means rotating every value that host held, not only
  re-keying the host.

## C6. Applied-commit reporting

- **Host plane.** `homelab-updater` writes a node-exporter textfile metric,
  `homelab_host_applied_info{host="<h>",sha="<12-hex>",version="<v>"} 1`.
- **Cluster plane.** The commit statuses from C2, plus Flux's `gotk_resource_info` series,
  whose `revision` label reads `status.lastAppliedOriginRevision` (D1.6). Every promotion
  adds one series per Kustomization through that label, and C7's series budget counts
  them. A Kustomization's `content_digest` label reads its `homelab/content-digest`
  annotation (C1). It moves only when the tag's own directory does, so it adds one
  short-lived series per change to the tag, not one per promotion. The root Kustomization
  `homelab` stamps the annotation and the tag's own Kustomization applies the content; no
  `dependsOn` orders the two.
- **Which revision field names a commit.** A Kustomization's `status.lastAppliedOriginRevision`
  holds `master@sha1:<sha>`, the `--revision` that `flux push artifact` wrote. Its
  `status.lastAppliedRevision` holds `<tag>@sha256:<digest>` and names no commit. Every reader
  that needs the applied commit reads the origin field. kustomize-controller's own `dependsOn`
  check compares `lastAppliedRevision` with the source's artifact revision, both in digest
  form, and a slice that describes that check uses the digest field.
- **`land.sh`.** After merging M, it waits until M is covered. It never waits on one `promote`
  run, because GitHub may evict a queued run (D2).
  - **Cluster.** Every Kustomization in M's changed set is Ready at an origin revision R
    that equals M or descends from it, and `flux/homelab` is too. M's changed set is the
    changed set of C2, taken between M's `digests.json` and the one on the newest published
    ancestor of M's first parent. `land.sh` reads both files from GHCR and derives no tags
    itself (D6). A `DEPLOY_HOLD` other than `none` (C2) ends the wait as `deploy-failed`,
    and twice the promote timeout ends it as `gave-up`. That deadline moves while a
    verification of an earlier commit is in flight, because M's `decide` waits on it (#5164).
    When every one of the PR's tags is `suspended` in M's `digests.json`, the wait ends as
    `suspended` at the first poll that reads that file and a `none` `DEPLOY_HOLD`, and `land.sh` prints `nothing-to-deploy` with exit 0. When only some
    are, the wait leaves those out and settles on the rest. A tag M changed while it was
    suspended at both ends does not end this way: `decide` holds it as `suspended-changed`,
    and the wait ends `deploy-failed` (D5.1d, #5327).
  - **The soak, from D5.3b3.** Before D5.3b3 the soak gates nothing, and `land.sh` does not
    read `flux/soak`. From D5.3b3, the cluster wait also needs `flux/soak` to read `success`
    on R. A `flux/soak` `failure` on R ends the wait as `deploy-failed` with the `Cause`
    `soak-failed`, before `settle` writes `DEPLOY_HOLD`. `land-wait` repeats the
    combined-status read on each poll until the soak has a verdict (D5).
  - **Hosts.** For each host the PR touched, `homelab_host_applied_info` shows M or a
    descendant of M, and `homelab_host_reconcile_ok` reads 1. H3 states how the touched hosts
    are found and when the wait ends `deferred`.

  It then runs `probe.py health` for the changed services.

## C7. Alert plane

- **Rule labels.** Every rule carries `service="<deploy tag or host name>"` and
  `severity="critical|warning|info"`. For an exporter's series, `service` is a target label
  that the scrape config sets, or a literal in the rule (M6). A rule that verifies a deploy
  also carries `verify="true"` (D5). One literal falls outside the rule:
  `service="deploy-pipeline"`, on the deploy-pipeline exporter's alerts. It names no deploy
  tag and no host, so no health gate reads it, and a standing hold cannot fail the `flux`
  tag's own gate (D2 Open question 12).
  Two more values are named outside the deploy-tag rule. `service="k3s"` is the setup role's
  tag, and it labels the cluster add-ons (coredns, metallb, longhorn and the like), whose
  workloads belong to no `deploy.sh` tag. `service="unmapped"` is the fallback when M2's
  workload-service mapping cannot resolve a workload. It is neither a deploy tag nor a host
  name, so `probe.py health <tag>` never matches it. For a workload in the mapping's domain,
  `WorkloadServiceUnmapped` pages with `service="observability"` at the same time. A
  restarting pod outside the domain pages on its own.
- **Watchdog.** A rule named `Watchdog` always fires. It routes to the Healthchecks.io ping.
- **Deploy-pipeline rules.** The deploy pipeline pages through Alertmanager like any other
  service, from D2.7, which waits on M1. notification-controller's Discord alerts stay as a
  second channel.
    - **The `flux` rule group** comes from D1.6b, which waits on M1. It fires on a controller that is down, on a
      Kustomization or the OCIRepository not Ready for 15 minutes, and on a Kustomization
      suspended for more than 24 hours. D4 adds a rule for saturated reconcile workers.
    - **The deploy-pipeline exporter** is a monitor-bridge family that D2 adds. It exports
      three things: `DEPLOY_HOLD`'s state and age; the age of the oldest green master commit
      that `prod` has not reached; and whether `prod`'s live digest matches `PROD_DIGEST`
      (C2).
    - **The exporter's rules** are four:
        - `DeployHoldActive`, a warning that repeats daily while a hold stands;
        - `PromotionStalled`, for an unpromoted green commit older than 2 hours with no
          hold;
        - `ProdMovedOutsidePromote`, which is critical;
        - `DeployPipelineExporterAbsent`, the `absent()` rule that M1 requires for every
          exporter.

      They land in D2.6d, which waits on M1.
- **Host rules.** H3 adds `HostReconcileFailed` and `HostUpdateStale` to M1's rules
  directory. `HostReconcileFailed` fires on `homelab_host_reconcile_ok` at 0 for 15 minutes.
  The PR that adds the two rules waits on M1. They survive M6, and H5 relies on them.
- **Series budget.** M1 sets a fleet-wide series budget. Each slice that adds a scrape, a
  probe target or a label states its series delta against that budget.
- **Traces.** No slice adds tracing. The deploy timeline comes from commit-status timestamps
  and from the deploy-pipeline exporter's ages. `design.md` records why.
- **Check producers.** A host producer follows M3's producer metric contract (M3 *The
  producer metric contract*). A producer that is a CronJob, such as M6's B7 or one of H2's
  moved crons, takes the kube-state-metrics recipe instead. Its rules read
  `kube_cronjob_status_last_successful_time` and `kube_job_status_failed`, and the first PR
  that uses the recipe adds `cronjobs,jobs` to kube-state-metrics' collectors (M6).
- **Health gate.** `probe.py health <tag>` reads
  `ALERTS{alertstate="firing",service="<tag>"}` and `probe_success{service="<tag>"}`, with six
  departures from that literal selector (M6):
    - `shadow!="true"`;
    - `alertname!="Watchdog"`, because the Watchdog fires forever with
      `service="observability"`;
    - `severity="critical"` or `verify="true"`, the soak's set, so a warning-level verify
      successor gates too;
    - only alerts that became active after the tag's reference time, read from
      `ALERTS_FOR_STATE`. The reference time is `applied_at` for an Ansible-owned tag, and for
      an `owner: flux` tag the time its Kustomization first reported its current
      `content_digest` (C6). Every per-tag Kustomization tracks the shared source, so its
      origin revision moves on every master commit; a reference read from it would let an
      unrelated commit move a failing tag's reference past its alert (#5338). A series
      without `content_digest` falls back to the first report of the current origin
      revision;
    - a `probe_success` sample newer than `applied_at`, so a sample taken mid-rollout cannot
      decide;
    - no probe half for a tag whose rendered entry has no route, even when an old
      `probe_success` series is still inside the lookback.

  D5's soak and `probe.py health` share one definition of this selector, except for the
  reference time. A drill exclusion therefore cannot apply to one reader only. The soak's
  reference is the arrival of its revision R (`flux_soak.py:anchor`), and it reads only the
  tags in R's changed set, so a later unrelated revision never moves it.
- **The soak.** From D5.3, a Job in the shared kind `soak` reads the alerts labelled
  `severity="critical"` or `verify="true"` for the services in the revision's changed set
  (C2), after M1's filter for shadow rules and the Watchdog. An empty changed set passes at
  once. The soak's window comes from those services' rules alone (D5). Its verdict reaches GitHub as the commit status `flux/soak`,
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
- **Decision markers.** Write a marker without its colon, as `DECIDED <text>`, both for a
  planned marker and when citing an existing one. `scripts/docs/reference/decisions.py`
  indexes every tracked line that carries the marker with its colon. With the colon, a
  planned marker would be published as a settled decision, and a pointer to an ADR that does
  not exist yet turns CI red. The slice that adds the marker writes it with the colon in
  code.
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

No request on the slice index is open.

### Requests on C1

- **The commit input** (R4 Open question 10). *Modes* names `homelab-render cluster --commit
  SHA`. The publish job passes the commit it publishes, and the render-diff job passes the PR
  head's SHA to both renders. Gate: before R4's PR 1 merges. D6.12 no longer renders twice
  once C1's footprint replaces `--moved-tags`, so D6 Open question 13 is withdrawn.
- **Host-mode placeholders** (H4 Open question 6, with H3's *Host mode renders no secret*).
  Host mode renders a secret reference as `${<key>}` in two kinds of file, a component's
  declared outputs and its compose files, and escapes those files by the cluster-mode rule.
  Every other host file keeps every `${` literal. Gate: before H4.1a3 merges.
- **The label that names a deploy tag** (M2 Open question 1). The renderer keeps stamping
  `homelab/role`, or C7 adopts `kustomize.toolkit.fluxcd.io/name` with `-deferred` stripped.
  Gate: before D6 merges.

### Requests on C2

- **The shape of `verify`** (D4 Open question 14). This row is conditional on D5's
  measured duration of `verify` on the first broad revision. A broad revision can hold one
  long `verify` job up to the 5.5-hour cap, and no other promotion runs meanwhile. If that
  measurement shows it, *Verification* changes `verify` into a short job that re-enters, or
  a scheduled poller. No gate is named until the measurement exists.
    - Under D2.5, `verify` runs in no group, so the next run's `decide` waits on it inside its
      own `promote` job and holds a second runner. D4.3 raises `verify`'s
      `timeout-minutes`, `decide`'s in-flight wait and the `promote` job's
      `timeout-minutes` with the derived timeout. Otherwise D2.5's 25-minute
      `timeout-minutes` stops a longer `verify` first, and `settle` writes a false
      `<T> unverified` hold (D4 *The cap*).

### Requests on C3

- **A sealed path for a secret that no file holds** (D3 Open question 8). *Secrets* gains a
  third set beside `SOPS_MANIFEST_KEYS`. It holds single-line secrets that `cluster_vars.py
  seal` writes as their own SOPS manifests from public recipients alone, so an agent can add
  one without the operator's `write`. R2's `sops-manifest-entries` check widens to match.
  Gate: before the `seal` PR merges, after D3.3c. Accepting that the two values agree only
  by construction is **operator-only**.
- **tdarr's pre-pull kind** (D5 Open question 21). *Per-tag Kustomizations* gains a
  predecessor kind for tdarr alone. It holds a pre-pull Job pinned to tdarr's node, so
  tdarr's 3000-second timeout can drop to the default. Its suffix joins *Reserved names* in
  D5.1a1's list. Gate: before tdarr's conversion PR merges.
- **Health checks off unchanged tags** (D4 Open question 13). This row is conditional: it
  is filed only if D4's or D5's measured worker occupancy trips `FluxWorkersSaturated`. If
  so, *Per-tag Kustomizations* gains `wait: false` plus generated `healthChecks`, or a
  shorter `timeout`, for a tag outside the changed set. No gate is named until the trigger
  fires.
- **Deferred applies by variable** (D4 Open question 7). *Per-tag Kustomizations* reads "a
  role that passes `manifests_deferred_files` gets `<tag>-deferred` once it is flux-owned",
  because D1 adds the flux role as a fourth caller, and *Reserved names* says that D4.2
  reserves the suffix. No gate is named.
- **Wider prune exemptions.**
    - PersistentVolumes and StorageClasses join PVCs and Namespaces (D6 Open question 12).
      Gate: before D6.19 merges. Without the amendment, D6.19 refuses to remove a tag whose
      render at `prod` holds a PersistentVolume, and the rename procedure excludes
      media-volume.
    - "and a ConfigMap that holds state no render restores, named by its slice", with H2's
      restore-drill stamp ConfigMap as its member (H2 Open question 12). Gate: before H2.3b
      merges.
- **The deletion policy's kept set** (D6 Open question 7). If D6.20 merges, it switches every
  `<tag>` except `flux`. `flux`, every `<tag>-deferred` and every shared kind keep `Orphan`.
  Gate: before D6.20 merges.

### Requests on C5

No request on C5 is open.

### Requests on C6

No request on C6 is open.

### Requests on C7

- **Log rules stay outside the health gate** (M5 Open question 1). A loki-homelab ruler
  alert never reaches Prometheus `ALERTS`, and the ruler-watching alerts cover the service
  there. Gate: before M5's PR 4b merges.
- **Where Alertmanager lives** (M5 Open question 2). Alertmanager lives in `observability`
  and is reached on its Service at port 9093. D5's Kustomizations for `loki-homelab`,
  `observability` and `netpol-baseline` keep that edge. Gate: before M5's PR 2a merges.

### Requests on the slice document format

- **Deletion PRs** (D6 Open question 6, R4 Open question 11). Exempt a pure deletion from the
  100–200 line guide, or split D6.12, D6.18 by module. R4's PRs 6 and 8 rely on the
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
  edges: D2 on R2, D2.7 on D4.0 and on D3.3 (the D3.3 edge left on 2026-10-14), D3.0 to D3.3 on D2.1 to D2.6 only, D3.3 on
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
      names D6.18 and R4's PRs 6 and 8.
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
  before D6.12 or R4's PR 1, whichever merges first, because D6.12's `--moved-tags` mode passes
  the head's SHA to both renders and R4 waits on D6.
- Cross-slice note from D6: C3 *Shared kinds* says that each member needs a
  `--moved-tags` rule from D6.12, and that a census test enforces it.
- Cross-slice note from H1 to D5: the D5 index row adds H1.7b as a gate of the flux PR,
  following D5's header.

### Pillar review fold (2026-10-12)

A review of the plan against eleven pillars ran across all 28 slices. The pillars were
scalability, reliability, availability, observability, maintainability, modularity,
testability, security, performance, efficiency and portability. This round applies what that
review found.

The changes are folded in but **not** adversarially reviewed. The pre-start review round
should cover every section that carries this heading, in every slice.

Applied to this page:

- **C1.** `digests.json` moves to D2's first publishing PR and gains a `footprint` digest.
  The footprint replaces D6.12's `--moved-tags` mode. The renderer classifies once, and no
  reader renders a second time. That supersedes the two `--moved-tags` notes in the
  cross-slice follow-up above.
- **C2.**
    - Render and sign are separate jobs, and only master commits publish.
    - `verify` expects the changed set instead of every published name. Before this, one
      unrelated unhealthy Kustomization held every promotion and reset `prod`.
    - The hold records the names that failed, as `<T> failed=<names>`.
    - The revert rule is proposed for an operator ruling, and it replaces D5 Open question
      17.
    - `PROD_DIGEST` detects a `prod` move made outside `promote`.
    - The break-glass promotion path is new. Before this, no deploy path existed while
      GitHub was down.
- **C3.** A shared kind's tag rule lives in the renderer's footprint rules.
- **C5.**
    - *Signing* stated an SSH-signature check that master commits cannot pass, as H3's
      *Baseline* shows. It now states the anchor the builder actually uses, which matches the
      GitOps deployer's `deploy_git.py:ci_walk_candidates`. The choice stays with H3 Open
      question 1.
    - The amendments on `homelab-tools` and on where decrypted files land are applied, with
      the rule on persisted plaintext.
    - Also added: `Valid-Until`, the signer's refusal rule, sandboxing and the staged
      rollout.
- **C6.** `land.sh` reads its changed set from `digests.json`. The page also notes the
  series cost of the `revision` label.
- **C7.**
    - Added the Flux rule group, the deploy-pipeline exporter and its three rules.
    - Added the host rules and the series budget.
    - Traces are declared out of scope.
- **Index.** D1.6b, D2.6d, D2.7, D4.9 and H3.5d wait on M1, and H3.9b waits through H3.5d.
  D4.9 also waits on D1.6b, and H6.3e's verify Jobs wait on D5.1a1. D3.0 to D3.3 need D2.1
  to D2.6c, so D3 does not inherit D2.6d's edge to M1.
- **Requests.**
    - The D5 request on C2 expects the changed set.
    - The H1 request's excuse rule is narrowed to a dependent that was already failing.
    - The two C5 requests that were applied leave the list.
    - *The commit input* gates on R4's PR 1 alone.
    - Added: two digest fields (D5 Open question 10), the sealed secret path (D3 Open
      question 8), and tdarr's pre-pull kind (D5 Open question 21).
    - Added as conditional requests: health checks off unchanged tags (D4 Open question 13)
      and the shape of `verify` (D4 Open question 14).
- **Further corrections from the slice owners, applied in the same round.**
    - C1 leaves the three index paths out of every footprint.
    - In C2, only jobs that check out no repository code hold `packages: write`.
    - C2 drops a name that T removes from the changed set.
    - C2 refuses the revert rule for a hold that names `soak`.
    - C2's break-glass render may come from a host checkout newer than the bundle.
    - C7 admits `service="deploy-pipeline"` and adds `DeployPipelineExporterAbsent`.
    - C7 scopes the soak to the changed set.

### Applied request (2026-10-12)

- **The verify drill's OCIRepository** (D1 Open question 7). C3 *Namespace* now names
  `verify-drill` as a transient second `OCIRepository` in `flux-system`. It is present only
  while `flux_verify_drill` is true, and the flux role deletes it while the lever is off. The
  request leaves *Requests on C3*, which clears the gate before D1.5 merges.

### Operator ruling (2026-10-13)

- **The standalone value rule** (R2 Open question 13,
  [#4892](https://github.com/DanielH2018/server/issues/4892)). On the pinned flux CLI, a value
  substituted alone into a field loses its string type, whatever quotes the template uses. The
  operator ruled on 2026-10-13: guard the values where they are decrypted, and move
  `mullvad_account`, the one key whose real value is all digits, to a dedicated SOPS Secret.
  C3 *Secrets* now states the rule and names its producers. The operator's ruling settles the
  wording, so the PR that records it applies it directly, and no row in *Requested
  amendments not yet applied* precedes it. R2's guard PR, PR 1c, has no contract gate left.

### Operator ruling (2026-10-14)

- **`domain` is published** (D2 Open question 5). The operator ruled on 2026-10-14: "domain is
  not secret, it's a var for flexibility just in case of future changes." C3 *The source of
  `domain`* adopts the alternative it listed. The renderer writes `domain` as a literal, and
  `domain` stays an Ansible variable in `ansible/inventory/group_vars/all.yml`, so it can
  change in one place.
    - D2.7 no longer waits on D3.3a or D3.3b. It waits on PR 2b of R2's *Publish `domain`*
      item, which makes `domain` a published key. The slice index row for D2 states the edge.
    - `homelab-cluster-vars` no longer holds `domain`, and no tag substitutes it.
    - R2's per-member prose is unchanged here. PR 2b changes it together with the code,
      because R2's documented-path test fails on names that do not exist yet.
    - Earlier *Review log* entries keep their wording as history. Where one names the D2.7
      to D3.3 edge, this ruling supersedes it.
- **D2's production drill is accepted** (D2 Open question 1). The operator accepts the drill
  on production, and the session may merge D2.8 itself. The ruling changes no contract row.
  D2.8's text records it.
- **The builder trusts master plus green CI** (H3 Open question 1). The operator chose option
  (a) on 2026-10-14:
  [ADR-0021](../adr/0021-the-host-package-builder-trusts-master-plus-green-ci.md) records it.
  The builder builds a commit on `origin/master` with a green CI verdict, and checks no
  signature and no landing tag. C5 *Signing* already named that anchor, so the ruling removes
  only the open question from it. The request leaves *Requests on C5*, which clears its gate
  before H3.5a merges.

### Applied requests (2026-10-14)

- **The flux directory's kustomization** (D2 Open question 6). C1 *Cluster output* now states
  that `DIR/flux/kustomization.yaml` lists every file in `DIR/flux/`. The request leaves
  *Requests on C1*, which clears the gate before D2.6b merges.
- **The root's name is reserved** (D2 Open question 6). C3 *Reserved names* now states that
  the render fails on a deploy tag named `homelab`, the root Kustomization's name. The
  request leaves *Requests on C3*, which clears the gate before D2.6b merges.
- **A second `owner` value for the handback** (D2 Open question 7). C4 *Values of `owner`*
  now states that `owner` takes `flux` or `released`. D2.1a, D2.1b, D2.1d and D2.2 already
  implement `released` in code, so the contract now matches the code. *Requests on C4*
  leaves the list, which clears the gate before D2.7 merges.
- **D2.5 and D2.6b own what the contract credited to D4.3a** (D2 Open question 6, D4 Open
  question 5). C2 *Verification* credits D2.5 for the name-based expected statuses. C3
  *Shared kinds* and *Publish census* credit D2.6b for the empty `SHARED_KINDS` and the
  allowlist census. The slice index has no D4.3a edge: D3's row waits on D2.1 to D2.6c, which
  include D2.6b. The round 2 entry for X2-11 made these edits, so this entry records that the
  request is applied, and the gates before D2.5 and D2.6b are clear.

### Applied requests (2026-10-15)

- **A shared-only mode** (D3 Open question 6). C1 *Modes* now states `homelab-render cluster
  --out DIR --shared-only`, which writes the shared kinds and the root and no tag, for a
  publish with no flux-owned tag. D3.3a (#5145) implements it and the promote workflow's
  no-tag branch calls it. The request leaves *Requests on C1*, which clears the gate before
  D3.3a merges.
- **SOPS manifests and the decryption field** (D3 Open question 6). D3.3c implements both
  rows below, and the request leaves *Requests on C3*, which clears its gates.
    - C3 *Per-role fields* now names a fourth field, `decryption: {provider: sops,
      secretRef: {name: sops-age}}`, derived from a non-empty `render_sops_manifests` and
      never set by hand. The shared kind `cluster-vars` also sets it. That clears the gate
      before D3.3c merges.
    - C3 *Secrets* now states that each SOPS manifest carries
      `kustomize.toolkit.fluxcd.io/substitute: disabled` in its plaintext metadata, written
      before encryption, and that the renderer copies it byte for byte. That clears the gate
      before D3.3c merges.
    - C3 *Secrets* now states that `sops-age` is the only Secret made by hand. Its gate was
      before D3.3a merges, and D3.3a (#5145) merged without applying it, so D3.3c applies it
      late.
- **D2.5's verification choices** (#5023). D2.5 (#5003) implemented *Verification* with
  three choices the contract did not record, and C2 now states each of them:
    - *On failure* and *The revert rule* name `settle` as the writer, and *On failure* adds the `<T> unverified` hold for a
      `verify` that ended with no verdict. The revert rule never passes that form.
    - The bullet *A verification in flight blocks the next promotion* records why `verify`
      and `settle` join no concurrency group, and the two checks that replace one.
      *Promotion target* now places the group `promote` on the `promote` and `tag` jobs, not
      on the workflow. The D5 row in *Requests on C2* gains the constraint on its reset, and
      the row on the shape of `verify` gains D4.3's three limits.
    - *Jobs that run repository code hold no write permission* gives `render` `checks: read`
      and `verify` `statuses: read`.

- **The version counts the built commit** (H3 Open question 7). C5 *Version* now reads
  `0.<git rev-list --count --first-parent <chosen>>+g<12-hex sha of <chosen>>`, where
  `<chosen>` is the green commit the builder builds, not `origin/master`'s tip. That matches
  H3's *Package rules*. The request leaves *Requests on C5*, which clears its gate before
  H3.5a merges.

### Applied requests (2026-10-16)

- **Two more digest fields** (D5 Open question 10). C1 *Cluster output* states that a deploy
  tag's object in `digests.json` holds `claims` and `suspended` beside `own` and
  `footprint`, from D5.1a2. It also states that `soak`'s footprint is normalised over the
  revision's SHA. The request leaves *Requests on C1*, which clears the gate before D5.1a2
  merges. The image contexts were applied later the same day (below).
- **Suspension and hand writes** (D5 Open question 5). C3 gains *Suspension*: `flux_suspend`
  renders `spec.suspend` only when true, the renderer drops every edge to a suspended
  Kustomization, and a hand suspend uses the field manager `homelab-operator`. C3 *Per-tag
  Kustomizations* states that `<tag>-snapshot` inherits `<tag>`'s `dependsOn`, and *Shared
  kinds* states that `homelab-namespace` owns the `homelab` Namespace. C3 *Deletion policy*
  already sets `Orphan` on every Kustomization the bundle publishes, so the non-tag kinds
  need no row. The request leaves *Requests on C3*, which clears the gate before D5.1a1
  merges.
- **When `soak` and `homelab-namespace` join `SHARED_KINDS`** (D5 Open question 16). The
  round 2 entry for X2-11 made the edit. C3 *Shared kinds* names D5.3b2 and the namespace
  PR, and *Reserved names* has D5.1a1 reserve both names. So this entry records that the
  request is applied, and the gate before D5.1a1 merges is clear.
- **`prune: false` for plumbing tags** (H1 Open question 2, with D5's traefik step). C3
  *Per-role fields* reads "`prune: false`, for a plumbing tag (H1) or a tag whose render
  holds a CustomResourceDefinition (D5)". H1 asked for one row that carries both halves
  under H1.1a's gate. traefik's flip waits on H1.1a, so D5 loses nothing. The row *`prune:
  false` beyond vendored CRDs* leaves *Requests on C3*, which clears the gates before H1.1a
  merges and before traefik's flip merges.
- **Vendored manifests in C1** (H1 Open question 2). The round 2 entry for X2-09 applied C1
  *Vendored files* and the per-directory unit in *Substitution and escaping*. So this entry
  records that the C1 half is applied, and the gate before H1.1a merges is clear.
- **The forward-only hold** (H1 Open question 3). C2 gains *The forward-only gates*, and *A
  hold without a reset* states that a failed revision that changes a forward-only tag holds
  and leaves `prod`. That clears the gate before H1.1b merges. The excused failures were
  applied later the same day (below).
- **D5's changes** (D5 Open question 4). C2 states all seven, and the request leaves
  *Requests on C2*, which clears the gate before D5.1b merges.
    - *A hold without a reset* covers snapshot roles. The operator accepted it in principle on
      2026-10-16 (D5 Open question 1), and confirms its two co-batched cases in D5.2b1's
      review.
    - *Verification* already left suspended Kustomizations out of the changed set. It gains
      the observe-only set, from D5.3b2 until D5.3b3.
    - *The reset input* states `promote-reset`, the hold check, and how the reset holds both
      checks of *A verification in flight blocks the next promotion*.
    - *The hold post*, *A change to a suspended tag holds* and *The reboot window* state the
      rest, and *Promotion target* gains the verdict `deferred`.
- **The revert rule is ruled** (D5 Open question 17). The operator adopted C2's revert rule
  on 2026-10-16, with its `soak` exclusion. C2 *The revert rule* and *Clearing a hold* no
  longer call it proposed. No row in *Requested amendments not yet applied* preceded it.
- **The `held` tag** ([#5277](https://github.com/DanielH2018/server/issues/5277)). A hold
  without a reset leaves `prod` on T while `last-good` stays on the older revision. Without
  an exception, every `decide` after the operator's clear would wait on that gap and end
  `current`, so one more gated bump could never promote. H1.1b's `settle` tags T as `held`,
  and `promote_lib/verify_promotion.py:wait_settled` treats a `prod` that names the `held`
  digest as settled. `prune` keeps the version, because it deletes only a version whose one
  tag is `sha-<C>`. C2 *Tags* gains `held`, *A verification in flight blocks the next
  promotion* gains the exception, and *A hold without a reset* names the tag, for D5's
  stateful hold too. No row in *Requested amendments not yet applied* preceded it.
- **The forward-only hold's excused failures** (H1 Open question 3). The pillar review fold
  narrowed the request to one rule: a dependent whose `flux/<name>` status on `prod`'s commit
  already read `failure` is reported, not expected. C2 *Verification* already stated it as
  *A dependent that was already failing*, and now also says that `decide` records those
  statuses before it moves `prod`. The request leaves *Requests on C2*, which clears the gate
  before H1.1i merges.
- **M6's phases** (M6 Open question 1, corrected by M6 Open question 12). The slice index
  splits M6 into M6a (Phase A, on M2 to M5), M6b (Phase B, on M6a's A6, except B-1, B-1d and
  B-1e, which need only M1 and C7's health-gate selector) and M6c (Phase C, on M6a and M6b,
  and on D6 for C-4b only). A7's two sub-edges move to M6a, and M6c gains C-1 on D5's
  registry PR. D5's row names littlelink's flux PR as the one that waits on B-1e, where it
  read "the PR that flips the last Ansible-owned stateless leaf". It also gains
  monitor-bridge's flip on A6-7a and the observability and netpol-baseline flips on C-1,
  each live, and states that D5 never flips uptime-kuma. A paragraph below the table states
  why the new D5 edges add no cycle. The request leaves *Requests on the slice index*. It
  named no gate.
  **Amended on 2026-10-22 (#5821).** The operator's 2026-10-21 ruling flipped uptime-kuma,
  and observability and netpol-baseline flipped before C-1. D5's row drops the C-1 edges and
  the uptime-kuma exclusion. D6's row gains M6's C-2b, live, which the uptime-kuma edge used
  to imply. M6c's registry edge moves to C-1a, the half of C-1 that holds the
  `monitor_status` gate. The paragraph below the table restates the no-cycle argument.
- **H2's per-PR edges** (H2 Open question 1). H2's row reads H2.2a on H1.5a and H1.6, H2.3b
  on H1.5a, H2.3c on H2.3b live, and H2.8a on H1.7b and H6.4b. Every other H2 PR waits on
  nothing outside H2. The row drops H1.8 and the H2.3b → D3 edge, and the paragraph below
  the table restates why no H2 PR waits on H1.9. The request leaves *Requests on the slice
  index*. It named no gate, because each change only loosens an order the row enforced.
- **Image contexts** (D5 Open question 10). C1 *Cluster output* states `DIR/images/<name>/`
  and `DIR/images/tags.json` from D5.4a1, the content tag `image_builder_content_tag`
  computes, and that a flux-owned consumer reads that tag from the same render, never from
  `render_guard.py:BUILT_IMAGE_TAG_STUBS`. The request leaves *Requests on C1*, which clears
  the gate before D5.4a1 merges.
- **The soak in `land.sh`'s wait** (D5 Open question 18). C6 *`land.sh`* gains *The soak,
  from D5.3b3*. It states that `land.sh` does not read `flux/soak` before D5.3b3, the
  `soak-failed` cause before `settle` writes `DEPLOY_HOLD`, and the repeated combined-status
  read. The cluster bullet no longer says "From D5 on". D5 Open question 18 names `verify`
  as the writer of `DEPLOY_HOLD`, and C2 *On failure* names `settle`, so C6 follows C2. The
  request leaves *Requests on C6*, which clears the gate before D5.3b3 merges.
- **CronJob checks and the health gate's selector** (M6 Open questions 3 and 4). C7 gains
  *Check producers*, which places the kube-state-metrics recipe beside M3's producer metric
  contract. C7 *Health gate* states the six departures from its literal selector, the
  reference time, and that D5's soak and `probe.py health` share one definition. The request
  leaves *Requests on C7*, which clears the gates before M6's B-1 merges and before B7's
  pi-peer-backup shadow PR.
- **The soak's channel and the `verify` label** (D5 Open question 6). Round 1 applied it for
  X-07. C7 states the `flux/soak` commit status in *The soak*, the `verify="true"` label in
  *Rule labels* and *Verify successors*, and M1's shadow and Watchdog filter in *The soak*.
  This entry records that the request is applied, and the gate before D5.3b1 merges is
  clear. No row in *Requested amendments not yet applied* preceded it.
- **The flux reference time reads the tag's content, not the revision** (#5338). C7 *Health
  gate* takes an `owner: flux` tag's reference time from the first report of its current
  `content_digest`, C6 states the label and C1 the annotation it reads. Live on 2026-10-16,
  peanut's reference moved from 21:04:54Z to 21:11:54Z between two `probe.py health peanut`
  runs with no change to peanut. D5's soak keeps its own anchor: it reads alerts only for the
  tags its revision changed, from that revision's arrival, so an unrelated revision never
  moves it. No row in *Requested amendments not yet applied* preceded it; the PR that
  applies it is where the contract owner rules on it. Accepted by the operator on 2026-10-17
  in #5369's review.
- **`soak` joins any changed set that holds a tag** (D5 Open question 19, ruled 2026-10-16).
  C2 *Verification* gains the rule beside *The changed set*, because the renderer drops
  `soak`'s `dependsOn` edges and no chain puts `soak` in the set any more. The request leaves
  *Requests on C2*, which clears the gate before D5.3b2 merges.

### Applied requests (2026-10-17)

- **`<T> stateful=unverified`** (D5 Open question 1). C2 *A hold without a reset* names a
  second form of D5's stateful hold, written when `verify` left no verdict or its names were
  malformed, and the list under *On failure* names it beside `<T> stateful=<names>`. It keeps
  the first word a SHA, as C2's `<T> unverified` does. The operator confirmed it on
  2026-10-17 in D5.2b1's review, with the two co-batched cases. No row in *Requested
  amendments not yet applied* preceded it.

### Applied requests (2026-10-21)

- **`service="k3s"` for cluster add-ons** (M2 Open question 3). C7 *Rule labels* confirms
  `k3s`, the setup role's tag, rather than naming the host. M2's workload-service rules and
  their promtool cases already emit it. This clears the gate before M2's PR 4b merges.
- **`service="unmapped"`** (M2 Open question 5). C7 *Rule labels* records `unmapped` as a
  value outside the deploy-tag rule and names `WorkloadServiceUnmapped`, which pages with
  `service="observability"` alongside it. This clears the gate before M2's PR 4b merges.
