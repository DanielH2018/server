# flux — the Flux controllers and the objects they follow (k3s, daniel-box)

Flux's source-controller, kustomize-controller and notification-controller, in the
`flux-system` namespace. They follow the promoted artifact `prod`, and Flux applies this role
too, as `flux bootstrap` would arrange (D5 step 29): the `flux` Kustomization applies the
components, and `flux-deferred` applies the sync objects, the root among them. The root
applies `flux-deferred` in turn. `sops-age` stays operator-created bootstrap material. No
Ansible path runs `tasks/main.yml`, and a rebuilt cluster gets Flux from
`docs/flux-deploy-plane.md`'s *Bootstrapping Flux on a rebuilt cluster*. That page is the
operator's view; the slice plans are `docs/target-architecture/deploy/d1-flux-installed.md`
and `d5-bulk-move.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses `--tags=flux`
  with exit 5
- **Image:** `python` (`flux_job_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

## The vendored install

`files/gotk-components.yaml` is the output of the pipeline on its first line: `flux install
--export` for the three controllers, a gawk filter that drops three documents, and a perl
step that pins each controller image by digest with `crane digest`. Nothing edits it by hand,
so a regeneration diffs as the pipeline plus upstream's change.

- **The filter drops three documents by name.** The `critical-pods-flux-system` ResourceQuota
  exists only so that `system-cluster-critical` pods admit outside `kube-system`, and no pod
  here runs at that class. It is dropped by name because the quota guard greps the tree for
  the kind, header line included. The NetworkPolicies `allow-scraping` and `allow-webhooks` admit every namespace (see
  *The `flux-system` fence*).
- **The images are pinned by digest**, as `<repo>:<tag>@sha256:<digest>`. The tag stays for
  the reader, and the kubelet pulls the digest. The fleet digest rule reads `*_image` defaults
  only, so `ansible/tests/k8s/test_flux_role.py` checks the rendered images instead.
- **To bump Flux**, set `flux_version`, then rerun line 1's pipeline at the new `--version`
  with that release's `flux` and crane v0.22.1 on `PATH`. Put the two header lines back above
  the output with the new version, and review the diff as upstream's change plus three digests.
  A release that changes `flux-view-flux-system`'s groups or verbs, or adds an aggregating
  ClusterRole, also fails `ansible/tests/k8s/test_aggregated_clusterroles.py`. Regenerating
  does not clear that failure: it widens `view` for every identity bound to it, so extend
  that module's lists only as a decision the PR states.
  Renovate cannot regenerate the file, so its PR is manual (`renovate.json`'s `fluxcd/flux2`
  rule) and the header test fails until the file is regenerated. The same PR moves the
  `version:` of all three `fluxcd/flux2/action` steps in `.github/workflows/promote.yml`, of the
  two in `.github/workflows/ci.yml`'s `flux_roundtrip` and `sops_manifest_type_proof` jobs, the
  one in `.github/workflows/bootstrap-drill.yml`, and the action's commit pin, so the CLI that pushes and tags the artifact and the CLI that proves
  the round trip move with the controllers. `sops_manifest_type_proof` also applies this file
  as it stands to a kind cluster, so a regeneration re-runs D3.3d's in-cluster proof on the
  new controller images. A `# renovate:` comment above each input is what Renovate reads, and
  `scripts/tests/test_renovate_flux_cli.py` fails a step that lost it (#4822). For a bump by
  hand, set all three yourself. `ansible/tests/repo/test_promote_workflow.py` fails a drift.

## How the render meets the fleet guards

DECIDED: the vendored Deployments meet each fleet guard by a render-time patch, never by an
exemption row, with one exception. From the target architecture's slice D5 on, these
controllers apply every service, and an exemption would be a lasting hole for that workload.
`templates/components.yaml.j2` passes the file through the `flux_components` filter
(`ansible/filter_plugins/flux_components.py`), which:

- merges `flux_pod_patch` into each Deployment: `revisionHistoryLimit`, `enableServiceLinks:
  false`, and a pod-level `runAsUser`/`runAsGroup` of 65534, the uid each image declares;
- states `RollingUpdate` where upstream leaves the strategy implicit. source-controller keeps
  upstream's `Recreate`, and its row in `ansible/tests/k8s/test_deploy_strategy.py:_RECREATE`
  is the one exemption;
- sets each pod's class from `containers_list | tier_priority_class('flux')`, so the `flux`
  entry's `tier: platform` gives `homelab-critical`, below the system classes like every
  homelab tier;
- renames `data`, `tmp` and `temp` to `<deployment>-<suffix>` (`flux_volume_suffixes`), in
  `volumes` and every `volumeMounts`. A merge patch cannot rename a list item. The filter
  refuses a volume the map does not name, so a regeneration that adds one fails the render;
- sets each flag `flux_controller_args` names on its controller, replacing upstream's value
  for that flag. kustomize-controller's `--concurrent` is `k8s_rollout_batch_width` plus one,
  so Flux reconciles as many tags at once as the deploy play rolls workloads (D5.1c), and the
  soak holds the extra worker from the start of a rollout (D5.3b2).
  `scripts/deploy_tools/chained_timeout.py:CONCURRENT` sizes `verify`'s queueing term from
  the width alone.

**This role reads `containers_list`.** The `tier_priority_class` call above makes it one, so
an added, removed or reordered entry reaches `flux` through the GitOps broad plane, whatever
`k8s_autodeploy` says. `docs/flux-deploy-plane.md` has what that means for a PR that changes
an entry.

## The `flux-system` fence

Upstream's `allow-egress` stays: it selects every pod in `flux-system` and admits ingress only
from pods there, so it is the namespace's default-deny, and its same-namespace rule carries
artifact fetches and controller events. `templates/networkpolicy.yaml.j2` adds
`flux-allow-prometheus`, which admits only the `app: prometheus` pods in `observability` to
the metrics port 8080. Egress is not enforced on this cluster (ADR-0009), so `allow-egress`'s
egress rule changes nothing. A change that adds a Receiver adds its fence with it.

## Rollouts

**Under Flux**, the `flux` Kustomization's `wait: true` health check on the three Deployments
and the soak replace the waits below, which ran only while Ansible applied this role. R4's
PR 1 maps each task in `tasks/main.yml` to that successor, and R4 deletes the file.

`manifests_rollout: ''` skips the shared wait. The role waits on each controller itself and
hands them to the end-of-play stabilisation gate, as observability does. Every
`flux_controllers` entry carries `restart_on: []`, because no task here restarts a controller
and no controller reads a Secret or ConfigMap through its pod spec; `defaults/main.yml` has the
full reason. `probe.py health flux` would otherwise fail all three as NOT ROLLED after a
NetworkPolicy edit, or after a rotation of either Secret that `templates/secret.yaml.j2`
renders.

**Memory, an idle floor.** On 2026-10-13, with no source configured, `kubectl top pod -n
flux-system` read source-controller at 16Mi, kustomize-controller at 18Mi and
notification-controller at 24Mi, beside a 64Mi request each. That is the floor for a root
that applies nothing. D5 re-measures it once Flux applies services.

**Memory, following `prod`.** Later on 2026-10-13, three hours after D1.5 pointed the root at
`prod`, the working set is higher. The three controllers request 192Mi in all, 64Mi each, and
each is limited to 1Gi. The peak is the largest
`container_memory_working_set_bytes{namespace="flux-system",container="manager"}` over those
three hours:

| Controller | `kubectl top` at 03:46Z | Three-hour peak |
|---|---|---|
| kustomize-controller | 63Mi | 70.1Mi |
| source-controller | 24Mi | 29.8Mi |
| notification-controller | 27Mi | 27.8Mi |

kustomize-controller runs above its 64Mi request while it applies an empty root. The node
has room, and the 1Gi limit is about fourteen times the peak, so D1 leaves the request alone. D5 sets
the requests from its own measurement once Flux applies services.

**Memory, under full load (D5 exit 7, step 31).** On 2026-10-23 the operator deleted the
kustomize-controller pod, and its replacement reconciled about 115 Kustomizations from cold. A
promote in the same hour applied a new bundle revision to every Kustomization. Over that hour
`probe.py metric` read a peak working set of 174002176 B, about 166 MiB. The steady working set
is 145 to 155 MiB. The pod it replaced read a 24-hour peak of 176783360 B. D1's 63Mi to 70.1Mi
figure above was an empty root, so it understates the load by more than half.

Exit 7 requires the peak to stay at or under the request, and 64Mi fails that by 2.3 times.
`flux_controller_resources` (`ansible/roles/k8s/flux/defaults/main.yml:flux_controller_resources`)
therefore raises kustomize-controller's request to 192Mi, which covers the peak with about 15
percent headroom. The limit stays at 1Gi. source-controller and notification-controller keep
64Mi, because this step measured only kustomize-controller.

## What Flux follows

`templates/sync.yaml.j2` renders the OCIRepository `homelab` on `prod`, the OCIRepository
`last-good`, the root Kustomization `homelab`, and the Providers and Alerts that post commit
statuses and Discord errors. `docs/flux-deploy-plane.md` describes each object.

- **It is a deferred file, applied after the CRDs.** The renderer writes it into
  `cluster/flux-deferred/`, and the Kustomization `flux-deferred` depends on `flux`, so the
  CRDs that `components.yaml` carries exist before Flux applies these objects.
- **`flux` renders `prune: false`, and `flux-deferred` renders `prune: true`.** The components
  carry Flux's own CRDs. A controller upgrade whose components drop a CRD would otherwise
  delete that CRD and every object of its kind, and `deletionPolicy: Orphan` covers only a
  delete of the Kustomization itself. The cost is traefik-crds': an object a later upgrade
  drops stays until the operator deletes it (**operator-only**).
  `scripts/renderer/tests/test_renderer_crd_census.py` names `flux` as a member. The lever
  reaches `flux` alone (`scripts/renderer/renderer_lib/bundle.py:flux_files`, its decision
  marker), so a sync object the render drops is pruned.
- **The source is pinned to D1's values.**
  `scripts/renderer/tests/test_renderer_bootstrap_documents.py` fails a render whose
  OCIRepositories move off `oci://ghcr.io/danielh2018/homelab-manifests`, `prod` and
  `last-good`, or the `promote` signer, because a broken source cuts Flux off from the commit
  that would fix it.
- **The signer identity is pinned twice.** `flux_signer_subject` and `flux_signer_issuer` are
  the regular expressions the OCIRepository matches, and `scripts/deploy_tools/forge.py` holds
  the same identity for `promote.py`'s `cosign verify`.
  `ansible/tests/k8s/test_flux_sync.py` fails a drift between the two.

## The suspend lever is gone

The renderer refuses `flux_sync_suspend: true` (`scripts/renderer/renderer_lib/suspension.py`),
and `sync.yaml.j2` renders no `spec.suspend`. Flux applies these objects itself, so a suspend
from the render would stop the OCIRepository that has to fetch the commit clearing it. An
explicit `false` would take the field back from a hand suspend at the next reconcile of
`flux-deferred`. To stop everything, the operator suspends the OCIRepository `homelab` by hand,
with its resume beside it: `docs/flux-deploy-plane.md`, *Recovering Flux by hand*.

## The verify drill

`flux_verify_drill: true` renders a second OCIRepository, `verify-drill`, which follows the
unsigned tag that a `verify_drill` dispatch of `promote` pushes. It must read Ready=False with
a verification error. The `deploy-errors` Alert does not name it, so it posts nothing to
Discord. The way out is the lever set back to `false`: the render drops the document, and
`flux-deferred`'s prune deletes `ocirepository/verify-drill` at its next reconcile after the
promotion. That prune succeeds the task in `tasks/main.yml` that deleted it by name while
Ansible applied this role.

**The drill ran on 2026-10-13.** The first dispatch was a false green: `verify-drill` read
Ready=True with its signature verified (#4820). `publish` pushes with `--reproducible`, and
the drill ran at the commit `prod` already carried, so its digest equalled `prod`'s signed
digest. #4826 fixed that. The drill push carries the annotation
`io.homelab.verify-drill=true`, so no signed push carries its digest, and the dispatch fails
unless `cosign verify` refuses that digest. The refusal alone proves nothing, because cosign
v3.1.3 reads a registry error on its bundle probe as "no bundle" (#4829). The dispatch
therefore also reads the registry itself, and fails unless it answers that the digest has no
referrer and no legacy `.sig` tag. GHCR serves no referrers API, so cosign keeps a digest's
bundle in the referrers tag `sha256-<hex>`. The re-dispatch
pushed `sha256:5bac820c…bb97` unsigned. At 04:45:39Z `verify-drill` read Ready=False with
"failed to verify the signature using provider 'cosign keyless': no signatures found", and
`homelab` stayed Ready. `deploy-errors` posted nothing to Discord, because it names only the
OCIRepository `homelab`. `FluxNotReady` stayed quiet for the same reason, and
`ALERTS{alertname=~"Flux.*"}` returned no data. The lever then went back to `false`.

## Tokens

- **No agent token ever carries `write:packages`.** Pushing an artifact, signing it and moving
  the `prod` tag happen only in the promote workflow's code-free jobs, under that job's
  `GITHUB_TOKEN`. An agent session's token never holds the scope.
- **`PROMOTE_TOKEN` rotation (operator-only).** The promote workflow reads `DEPLOY_HOLD` and
  writes `PROD_DIGEST` with the repository secret `PROMOTE_TOKEN`, a fine-grained token with
  Variables read and write on two repositories, `DanielH2018/server` and
  `DanielH2018/dotfiles`, and a 90-day expiry. To rotate it, mint the
  replacement, set it with `gh secret set PROMOTE_TOKEN -R DanielH2018/server`, store the copy
  with `sops set` as `promote_token`, run `uv run python
  scripts/secrets_mgmt/secret_rotation.py sync`, and commit `ansible/secret_rotation.yml`.
  The copy's registry row is on the `external-90d` tier with `source: record`, so the audit
  flags it before the token expires.
- **`flux_github_status_token` rotation (operator-only).** notification-controller posts
  commit statuses with this fine-grained token, which has Commit statuses read and write on
  two repositories, `DanielH2018/server` and `DanielH2018/dotfiles`. It has no expiration
  date. It lives only in SOPS, with no GitHub secret beside it, and
  `templates/secret.yaml.j2` renders it into the Secret `flux-github-status`. Its registry
  row is on the `external` tier, so the audit flags it 365 days after its last rotation,
  and that audit is the only rotation prompt. To rotate it, mint the
  replacement, store it with `sops set` as `flux_github_status_token`, run `uv run python
  scripts/secrets_mgmt/secret_rotation.py sync`, and write the cluster file with
  `cluster_vars.py write`, then commit. The `flux` Kustomization substitutes the value into
  the Secret once `promote` publishes the commit. notification-controller reads the Secret on
  each event, so no controller restarts.
