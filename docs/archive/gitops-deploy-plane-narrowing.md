# The tick's deploy-plane narrowing, as it ran until D6.12

Status: historical. Slice D6 deleted this path, and Flux applies every k8s change through `promote`.
The text below moved from [The GitOps pipeline](gitops-pipeline.md), *A deploy-plane change
applies nothing on the tick*, unedited except for the links.

Until D6.12 the tick narrowed and applied the deploy plane. `handle_broad` asked
`deploy_tags.py narrow` which services the range reached and ran `ansible/deploy.yml --tags`
over them. A refusal deferred the plane as `k8s_unapplied` lines (#4333), and a failed narrowed
apply held the plane as `ansible/deploy.yml <tags>`. A `narrow-shadow:` line compared each
service's applied digest with a render record.

D6.15 removed the `narrow` subcommand, and D6.18b deleted the rules it applied.

- **A play file whose parsed YAML is unchanged maps to no tags.** A comment or layout edit to
  `ansible/deploy.yml`, or to a file under `ansible/pre_tasks/`, `ansible/tasks/` or
  `ansible/post_tasks/`, records `narrowed-to-nothing`. Any other edit to a play file refuses,
  and so does an edit to a play-level file that is not YAML, such as `ansible.cfg`.

The two rules that follow it in the live page, for a shared template and for a filter plugin,
stay there because `scripts/deploy_tools/narrow_lib/templates.py` and `filters.py` still apply
them to the setup plane and to a landing's tag list.

## Why the decision changed

[ADR-0020](../adr/0020-flux-applies-the-cluster-from-a-promoted-artifact.md) records the move to
Flux, and [ADR-0010](../adr/0010-pull-based-gitops-over-argo-and-flux.md) records the deployer
that this narrowing belonged to. The plan that deleted the path is
[D6](../target-architecture/deploy/d6-delete-old-path.md).
