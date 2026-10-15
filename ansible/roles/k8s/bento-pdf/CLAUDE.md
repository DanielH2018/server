# bento-pdf — browser-based PDF toolkit

BentoPDF, a stateless client-side PDF editor. See repo-root `CLAUDE.md` for shared
conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=bento-pdf` with exit 5
- **Image:** `ghcr.io/alam00000/bentopdf` (`bento_pdf_k8s_image`)
- **Route:** `bento-pdf.<domain>` · `bento-pdf.local.<domain>`, Authelia one_factor
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Digest-pinned image**, with the tag kept alongside the digest so Renovate's k8s-defaults
  manager still tracks it for digest-bump PRs.
- **Port:** 8080. Stateless RollingUpdate Deployment with a readinessProbe.
- **Flux applies it, not Ansible** (D2.7, the first Flux leaf). Its `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle.
  Its digest-bump PRs therefore draw no `land-wait` and no `probe.py health` run. To give it
  back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert
  of the flag leaves two writers on the objects.
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
