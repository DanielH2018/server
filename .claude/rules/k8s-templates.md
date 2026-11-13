---
paths:
  - "k8s/roles/**/templates/**"
  - "k8s/roles/**/files/**"
---

# Where a k8s role's non-manifest config goes

`k8s/roles/<name>/templates/` is for **manifests only**. App config a manifest embeds via
`lookup('template')` goes in **`templates/config/`** (CouchDB's `local.ini`, HA's
`secrets.yaml.j2`); a file it carries verbatim with `lookup('file')` goes in `files/` (HA's
`configuration.yaml`). `scripts/validate/k8s_manifests.py` enforces both directions — it fails
a `lookup('template')` naming a `templates/` path outside `config/` (a `*.sh.j2` script is the exception: the shell gate renders it), and a top-level template
that renders a document with no `kind` — and `is_manifest_template` in
`scripts/lib/k8s_roles.py` names the two shapes it exempts (`Dockerfile*`, `*.sh.j2`).

`docs/homelab-render.md`, *The file rules*, has the render contract every k8s role meets.

## Never print a secret value

A template, a script it ships or a Job it runs never echoes, logs or writes a secret's value to
stdout. A pod's output reaches Loki, and a render's output reaches the transcript.

## Removing a key from a Secret template

Removing a key from a `secret.yaml.j2` removes it live only when Flux owns the key. For a role
with a `cluster/services.yml` entry, Flux's server-side apply removes a key it
no longer applies, and it takes over the `kubectl-client-side-apply` and `kubectl-patch` field
managers. A key that any other field manager holds stays live, so patch it out and verify.
`docs/target-architecture/deploy/d3-secrets.md` has the evidence, at step D3.5.
