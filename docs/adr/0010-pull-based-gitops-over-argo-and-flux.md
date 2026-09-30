---
id: "0010"
title: The homelab keeps its own pull-based deployer instead of Argo CD or Flux
status: Accepted
date: 2026-08-21
governs: []
---

# ADR-0010: The homelab keeps its own pull-based deployer instead of Argo CD or Flux

## Status

Accepted. Evaluated 2026-08-21; no controller is installed and none is scheduled.

## Context

The homelab deploys by pulling: a timer on `daniel-box` fetches, gates on CI, fast-forwards
the checkout, deploys what is eligible, health-gates the result and rolls back on failure.
Ansible renders the Jinja templates and applies them. The obvious question was whether a
purpose-built GitOps controller should replace that.

The real choice turned out not to be Argo versus Flux. It is where the render boundary sits:
Ansible renders and applies today, and a controller only helps if the applying moves to the
cluster.

Argo CD was rejected on two measured grounds rather than on taste. It has no native SOPS
support — upstream points at Sealed Secrets, External Secrets or a vault plugin — and the
homelab's whole secret plane is SOPS with age. Its default footprint is a 256Mi request for
the repo-server plus 1Gi for the application controller before Redis, against `daniel-box`
already sitting at 65% memory, 18.8 of 28 GiB.

Flux is the better technical fit: its controllers run about 30 MiB each and it decrypts
SOPS/age natively. The evaluation recommends augmenting with Flux in narrow slices while
keeping Ansible as the renderer, the host-config plane and the secret plane.

What stopped a wholesale replacement is the deployer's own content: decision logic encoding
about ten recorded incidents, most of them with no controller-native equivalent.

**The line count in this premise does not match the tree, and has since grown further.** The
evaluation cited "roughly 784 lines" of `gitops_deploy.py`, which is narrower than any measured
figure and may have counted the decision functions rather than the whole file. The day after this
ADR was accepted, at `f542693ac` (2026-08-22), the deployer was 1,928 lines across two modules —
`gitops_deploy.py` (1,086) and `deploy_logic.py` (842). On 2026-09-30 it is 7,256 lines across
25 modules under `ansible/roles/setup/gitops_deploy/files/`, none of them test files. Measure
it with `cat ansible/roles/setup/gitops_deploy/files/*.py | wc -l` rather than reading a number
here.

The decision below is unchanged, and the growth does not weaken it: what grew is the incident
logic a controller has no equivalent for. It does raise a separate question — how much of that
logic is a proxy for something cheaper to derive — which issue #2810 asks about the five path
narrowings, and which is a refactor of this deployer rather than a reason to replace it.

## Decision

Keep the Ansible-rendered, pull-based deployer. Argo CD is rejected. Flux is evaluated and
deferred, not adopted.

The one new capability the Kustomize port would have bought — schema-checking the rendered
manifests — was obtained without it: `scripts/validate/k8s_manifests.py` learned to
schema-check the documents it already rendered, wired into the prek hook. That is what turned
the port from a recommendation into an option.

## Consequences

**A merge is not a deploy.** The deployer auto-deploys only an image-pin bump to a
non-denylisted service, so an ordinary manifest or template change is fast-forwarded and left
unapplied until someone deploys it. This is the single most surprising property of the
pipeline and the reason the repo documents a follow-through procedure.

**A broad change parks the whole range.** A change under the setup or shared plane makes the
deployer defer, alert and return *without fast-forwarding at all* — so an unrelated commit in
the same range never lands locally either. The symptom is a tick that exits 0, logs nothing
and records `behind_since`.

**The decision logic is ours to maintain.** That is the cost of not adopting a controller,
and it is accepted because the logic is what encodes the incidents.

**The evaluation is not a plan of record.** Its recommendation to add Flux stands unexecuted.
Anyone reading `docs/archive/gitops-argo-flux-evaluation.md` should treat it as an evaluation whose
recommendation was consciously deferred, not as a description of what runs.

## Governs

No single line. `governs:` is empty; the decision is expressed by the existence of
`ansible/roles/setup/gitops_deploy/` and the absence of any controller.
