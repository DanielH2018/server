---
id: "0037"
title: The renderer keeps Jinja after Ansible, and sheds the Ansible model instead
status: Accepted
date: 2026-11-19
governs: []
---

# ADR-0037: The renderer keeps Jinja after Ansible, and sheds the Ansible model instead

## Status

Accepted.

## Context

Jinja came into this repo with Ansible. Once ADR-0029 retired Ansible and the two cluster
nodes stopped running Docker, the obvious question was whether the template language should
go too. At the time of the review (2026-11-18), the k8s roles held 405
templates in 72 roles, about 30,900 lines. The host packages held 242 more, all systemd units,
shell, conf, env and JSON.

The review found that Jinja itself costs little. Most of the cost in the renderer came from
three things Ansible had brought with it, none of which Jinja forces:

- **Ansible's variable model.** The model has five layers of precedence and lazy `{{ }}`
  values in vars files, and ADR-0028 has to specify both.
- **Secret values entering templates as variables.** Because a template can transform a
  variable, the renderer has to prove that no template does. Rule 5 of the secret checks
  holds that proof (`docs/homelab-render.md`).
- **Hand-indented file embeds.** A ConfigMap that embeds a file calls `lookup('file')` and
  then `| indent`.

Four replacements were weighed:

- **Helm or Kustomize, run by Flux.** Either one moves rendering out of the repo. That breaks
  rendering from the git tree alone (ADR-0020), placeholder mode, the Flux round trip and the
  render-diff gates.
- **A typed configuration language (CUE, Jsonnet, KCL or Pkl).** The switch means a rewrite of
  about 31,000 lines behind a shadow render. The Dockerfiles, shell, CSS, Alloy, `.ini` and
  `.conf` templates still need a text engine, so the repo would run two template languages.
- **Python builders for the Deployments, in the cdk8s style.** This fits a uv and pytest repo
  better than a new language. But the 47 Deployments were mostly one-off (median about 110
  lines), and the shape they share already lives in macros and in the `service-default`,
  `ingressroute-default` and `claim-default` templates.
- **A different engine for the host packages.** Every host template is a text format, which is
  what a text engine is for.

## Decision

The renderer keeps Jinja for both the cluster render and the host packages, as the closed
subset ADR-0028 defines. The work goes into removing the Ansible-shaped parts the tree no
longer needs, one zero-diff render at a time. Milestone 15, *Renderer after Ansible*, did that:

- Flat Secrets are declared as `render_secrets` data and rendered from one shared template
  (#8977).
- A vars file may not name a secret key, and the include-var rank is folded into the role
  defaults (#8981, which ADR-0028's amendment records).
- `manifests_secret_files` is gone, so a role has one file list (#9068).

Two slices were measured and dropped. A ConfigMap generator for file embeds came out near
break-even, because only 8 ConfigMaps are pure embeds (#8979). Moving the payload templates
to env or file reads would not remove rule 5, because at least 13 templates name a key by
design (#8980).

## Consequences

- Rule 5's syntax-tree walk stays, and so do placeholder mode and the round trip, for as long
  as Flux substitutes anything.
- Vars files keep their lazy `{{ }}` values. Most of them read another layer, such as
  `k8s_namespace` or `hosts[...]`. Literal vars files would mean copying values into each file,
  where they drift, or moving each role's derived values into one central builder.
- A template is still text, so a wrong indent or a misplaced key surfaces at render time or
  in the OpenAPI validation of `scripts/validate/render_lib/k8s.py`, not at authoring time.
  A typed language would have caught that earlier, and this decision gives that up.
- Reversing this decision means a full shadow render of every role against a second engine.
  Python builders are the cheapest route if one is ever needed. A new kind of workload whose
  shape repeats across many roles is the signal to revisit them.

## Governs

No single line enforces this decision. ADR-0028's closed list in
`scripts/renderer/renderer_lib/dialect.py` is the nearest anchor, and it governs the subset,
not the choice of engine.
