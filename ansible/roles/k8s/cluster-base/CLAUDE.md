# cluster-base — the PriorityClasses and the read-only cluster identity

The four workload-tier PriorityClasses, and the `homelab-readonly` ServiceAccount with its
ClusterRole, bindings and token Secret. Two objects here are needed before Flux can run. Flux's
own controllers name `homelab-critical`, and `setup/k3s`'s `kubeconfig.yml` reads the token
Secret.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=cluster-base` with exit 5
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it, not Ansible** (H1.2). Its `containers_list` entry carries `owner: flux`
  from its first commit. `tasks/main.yml` holds the include of `k8s/manifests` and no Flux path
  runs it. A rebuilt cluster applies it by hand before it installs Flux, as step 2 of
  `docs/longhorn-disaster-recovery.md`'s *Procedure (fresh host, total loss)*.
- **The tag renders `prune: false`** (`flux_prune: false`, with the `DECIDED:` marker beside it
  in `defaults/main.yml`). A render that drops a PriorityClass or the identity leaves it serving.
  The cost: an object removed from the render stays live until the operator deletes it
  (**operator-only**).
- **`NEVER_SHED` names the tag** in `ansible/filter_plugins/service_tier.py`. The role is
  untiered and renders no workload, so the lost-node break-glass has nothing to scale. The
  entry stays in the set because
  `ansible/tests/k8s/test_shed_set_is_derived_from_tier.py::test_never_shed_matches_the_untiered_roles_that_never_prune`
  holds the set equal to the untiered roles that set `flux_prune: false`.
- **The identity's name and namespace are a mirrored pair.** `setup/k3s` keeps
  `k3s_readonly_sa_name` and `k3s_readonly_sa_namespace` for the kubeconfig and the audit
  policy, and this role's template reads `cluster_base_readonly_sa_name` and
  `cluster_base_readonly_sa_namespace`.
  `ansible/tests/k8s/test_cluster_base_mirrors_the_k3s_identity.py::test_each_mirrored_pair_is_equal`
  holds each pair equal, so a rename changes both sides in one PR.
- **`k3s_readonly_crd_api_groups` lives in this role's defaults.** It keeps its `k3s_` name
  because the key's readers are in this role alone. `headlamp_k8s_crd_api_groups` mirrors it.
- **`templates/skip-probe-configmap.yaml.j2` is inert on purpose.** It proves that
  `kustomize.toolkit.fluxcd.io/reconcile=disabled` makes kustomize-controller skip an object,
  which the break-glass procedures of `docs/break-glass.md` rely on. The operator annotates it
  in the cluster, edits `data.value`, and reads the value survive two reconciles (**operator-only**).
- **Every `priorityClassName` in the `flux` render names a class this role renders**, or a
  Kubernetes built-in.
  `ansible/tests/k8s/test_flux_priority_classes_come_from_cluster_base.py::test_every_flux_priority_class_is_built_in_or_rendered_here`
  holds that, because the hand apply of step 2 is what lets Flux's pods admit.
- **The role's templates carry the `DECIDED:` markers** about `globalDefault` and
  PodDisruptionBudgets; they moved with `templates/priorityclass.yaml.j2` from `setup/k3s`.
