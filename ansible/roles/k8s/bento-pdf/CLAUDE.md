# bento-pdf — browser-based PDF toolkit

BentoPDF, a stateless client-side PDF editor. See repo-root `CLAUDE.md` for shared
conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=bento-pdf` gates the rollout
- **Image:** `ghcr.io/alam00000/bentopdf` (`bento_pdf_k8s_image`)
- **Route:** `bento-pdf.<domain>` · `bento-pdf.local.<domain>`, Authelia one_factor
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Digest-pinned image**, with the tag kept alongside the digest so Renovate's k8s-defaults
  manager still tracks it for digest-bump PRs.
- **Port:** 8080. Stateless RollingUpdate Deployment with a readinessProbe.
- **Flux applies it, not Ansible** (D2.7, the first Flux leaf). Its `containers_list` entry
  is in `cluster/services.yml`, so nothing applies it by hand, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle.
  Its digest-bump PRs therefore draw no `land-wait` and no `probe.py health` run.
- **Hostname is single-label** (`bento-pdf`, not `bento-pdf.k8s`) deliberately: the
  `*.local.<domain>` wildcard cert covers it; a nested label would not.

## Notable
- `bento_pdf_k8s_uid: 101` matches the upstream nginx-unprivileged image's own UID, and is
  also used as `fsGroup` for nginx's `emptyDir` scratch dirs. An `emptyDir` has no mode
  setting and mounts `0755` root-owned, so without the matching `fsGroup` the container hits
  `EPERM` on write.
- Pinned the same image tag as the retired Docker role while both existed, so a behavior
  difference between the two was never the image. #2385 deleted that role from the tree.

## Editing
- Manifests: `templates/deployment.yaml.j2`. The Service and the IngressRoute come from the
  shared `ansible/templates/service-default.yaml.j2` and
  `ansible/templates/ingressroute-default.yaml.j2`, which this role ships no template for —
  the route's hostname, port and Authelia gate are the `containers_list` entry's.
