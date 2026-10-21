# netpol-baseline — default-deny NetworkPolicies for the cluster

Renders the baseline NetworkPolicy for both `homelab` and `observability`, plus a
per-workload override under `templates/networkpolicy-*.yaml.j2` for a service that needs a
tighter or looser allow-list than the baseline. Deploys no workload of its own.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=netpol-baseline` with exit 5
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Renders nothing runnable** — NetworkPolicy objects plus one verify Job
  (`templates/verify/job.yaml.j2`, rendered as `netpol-baseline-verify`) that verifies the
  policies actually fence what they claim to. Its legs and its readiness gate both read the
  target table `netpol_baseline_probe_targets` in `defaults/main.yml`. Add a target as a row
  there, never as a second Job (#3813).
- **Pins no image.** The Job runs the image the renderer passes every verify Job
  (`flux_job_image`), so an image bump cannot change this role.
- **`netpol_baseline_scope: namespace`** — the baseline selects every pod in the namespace
  EXCEPT one carrying `netpol-baseline-exempt` (a workload with its own, tighter policy). Set
  to `label` to scope to opt-in pods only; `netpol_baseline_enforced: false` disables
  enforcement entirely without deleting the rendered policy.
- **Observability namespace has its own levers** (`netpol_baseline_obs_enforced`,
  `netpol_baseline_obs_scope`, own node-CIDR list) — rolled out and back independently of
  `homelab`'s.
- **The probe also tests the `flux-system` fence**, which the flux role owns. Its table rows
  carry plane `flux-system`, which has no lever, so they run whatever
  `netpol_baseline_enforced` and `netpol_baseline_obs_enforced` say. Each flux Service maps
  port 80 to a different pod port, so those rows set `service_port` for the leg to dial and
  keep `port` as the pod port the fence names. The table comment in `defaults/main.yml` has
  the columns.
- **The observability policies are deferred** (#3591). The role's prune is armed, and the
  armed apply's `-n homelab` refuses a document in another namespace (#3575). So
  `networkpolicy-observability`, `-prometheus`, `-loki` and `-alertmanager` render into
  `netpol-baseline-observability/`, which the renderer emits as `netpol-baseline-deferred`.
  A new template rendering outside `k8s_namespace` must join `manifests_deferred_files` in
  `tasks/main.yml`, and `ansible/tests/deploy/test_manifests_prune.py` fails when one is
  missing.

## Notable
- **Both boolean levers coerce oddly.** They go through `| bool` in the templates, so a typo
  like `fasle` is silently `False`: the allow-all body renders and the deploy reports green
  while nothing is fenced. A scope typo falls through to the label selector. The role's tasks
  asserted the values before they applied anything. Flux runs no task, so `LEVER_ASSERTS` in
  `tests/test_netpol_baseline_flux_gates.py` holds the same predicates and applies them to the
  render context, which CI runs before a typo can merge (#5685).
- **The exempt-workload list is read as an exact set, live** — a name absent from the cluster
  is a widening about to happen; a cluster workload missing from the list is fenced by
  nothing. The two disagreed for ~16 hours during slice 4.5. The `exempt` check of the verify
  Job's `gate` initContainer lists the `netpol-baseline-exempt` pods in each
  namespace-scoped baseline's namespace. It runs after the apply, so drift fails the Job and
  the revision's soak acts on it, where the Ansible task refused the apply.
  `ansible/tests/k8s/test_netpol_baseline_labels.py` pins the set against templates.
- **`netpol_baseline_node_cidrs` are `/32` host addresses on purpose** — a `/16` here would
  silently cancel the whole policy.
- **Flux orders the role last.** `K8S_APPLIES_LAST` in `ansible/filter_plugins/toposort.py`
  gives the role an edge onto every other k8s entry. The renderer's ordering turns that into
  a dependency of `netpol-baseline` on every other flux-owned tag and on every other tag's
  `-deferred` Kustomization. `netpol-baseline-deferred` depends on `netpol-baseline`, and
  `netpol-baseline-verify` on `netpol-baseline-deferred`. The Ansible tasks drained queued
  rollouts first, and Flux's `wait` and that ordering replace the drain. An edge onto an
  Ansible-owned role is dropped, which is why this role flips after every other.
- **A failed workload change leaves these policies unapplied.** The role depends on every
  other tag, so a revision whose workload change fails its `wait` never reaches
  `netpol-baseline` or its verify Job. The flip PR repeats D4's drill on this edge.
- **Flux applies it, not Ansible** (D5, Wave D's last flip). The `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. To
  give it back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain
  revert of the flag leaves two writers on the objects. `tasks/main.yml` holds one task, the
  include of `k8s/manifests` with the deferred file list, which the renderer reads.
- **`templates/verify/` is the probe's Flux form.** The Job runs
  `files/netpol_baseline_verify.py` twice. Its `gate` initContainer reads the targets'
  EndpointSlices through the API server and runs the `exempt` check, and its main container
  dials the probe's legs through `flux_job_connect`. Both read the same
  `netpol_baseline_probe_active` table. `job.yaml.j2` carries the caller census and the
  `DECIDED:` notes on the digest and the initContainer.
  `tests/test_netpol_baseline_verify.py` runs the Job census over its render.

## Where a per-workload policy lives

A per-workload policy has three homes. What the policy reads, and what the workload's deploy
needs, decides which one.

- **In this role**, when the policy reads one of this role's variables: the
  `netpol_baseline_enforced` lever or `netpol_baseline_node_cidrs`. Role defaults are
  role-scoped, so neither resolves in another role. Every `templates/networkpolicy-*.yaml.j2`
  file here reads one of them.
  Each one but `networkpolicy-callers.yaml.j2` also opens with a `DECIDED:` marker naming the
  shape the callers model below cannot express: an ipBlock, a from-less open port, UDP, a
  second port, a cross-namespace peer or a non-`app:` peer (#3701). The
  `bespoke-netpol-says-why-it-is-not-a-callers-fence`
  row of `ansible/tests/repo/test_census_rows_roles.py` fails on one that does not.
- **As data on the `containers_list` entry**, when the fence is one port and a list of caller
  pods: `netpol_from: [apps]`, rendered through the `networkpolicy` macro in
  `ansible/templates/networkpolicy.yml.j2`. `templates/networkpolicy-callers.yaml.j2` renders
  the `netpol_caller_fences` filter's fence for every entry carrying the key, behind the `netpol_baseline_enforced` lever. An entry
  whose pod label is not its own name sets `netpol_app:` to the label (scrutiny's
  `scrutiny-web`). A sub-workload with no entry of its own is a `netpol_fences: [{app, port,
  from}]` item on its role's entry, carrying its own label and port (freshrss's feed cache).
  An entry whose role renders its own copy also sets `netpol_role_owned: true`, and the loop skips it.
  `homepage_widget: true` on an entry adds `homepage` to its callers, because homepage's
  dashboard widget dials that ClusterIP. Every renderer reads the callers through the
  `netpol_callers` filter in `ansible/filter_plugins/homepage_tiles.py`, never `netpol_from`
  alone, so the key reaches role-owned fences too.
  A `netpol_fences` item its role renders sets `role_owned: true`, and the loop skips that too.
  `ansible/tests/k8s/test_netpol_from.py` fails if a policy name renders from two roles.
- **In the workload's own role**, when the workload's deploy depends on the policy:
  - A workload in `netpol_baseline_exempt_workloads` keeps its complete fence beside it:
    headlamp, n8n, registry, prowlarr's flaresolverr and karakeep's chrome. Exempt, it is
    fenced by nothing else.
    headlamp's single-caller fence is `netpol_from` on its entry with `netpol_role_owned: true`,
    and flaresolverr's calls the macro directly because its port is a prowlarr role default.
  - A backend that the pod's startup waits on ships with the pod. The precedents are
    authelia's session store (#1609) and the scrutiny and karakeep backends (#1620).
    authelia's and karakeep's are `netpol_fences` items with `role_owned: true`, each rendered
    through the macro by a template in that role.
  - The additive caller rules of the *arr stack and qbittorrent live with the workload they
    admit callers to. sonarr's, radarr's, prowlarr's, bazarr's and qbittorrent's caller lists
    are `netpol_from` on their entries, with `netpol_role_owned: true`.

**A hand `--tags <svc>` deploy does not apply that service's policy when the policy lives
here.** When a change needs a policy change and a workload change together, deploy both
tags in one run, and `K8S_APPLIES_LAST` applies netpol-baseline after the workload role. In
two separate runs, deploy the workload role first. `docs/networkpolicy-slice-answers.md`
records why that order matters. A new backend the pod waits on goes in the workload's role
instead, so one tag carries both.
