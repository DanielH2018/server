# docs — the MkDocs site, and why the build does not happen here

Serves the built MkDocs Material site over `docs/`, behind Traefik and Authelia, so the repo's
runbooks, design documents and generated reference pages are readable in a browser.

## At a glance
- **Image:** `nginxinc/nginx-unprivileged:1.29-alpine` (`docs_k8s_image`) — serves static files
  and nothing else.
- **Host:** pinned to `daniel-box` (`docs_k8s_node`), because it bind-mounts that host's built
  site.
- **Serves:** `docs_host_site_dir` (`/home/<user>/docs-site`), read-only.
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list`,
  `platform: k8s`. **Public as well as LAN**, `use_authelia: true` — the ingressroute call
  passes no `public`, and the macro's default is `public=true`. This said LAN-only
  (`public=false`), which was true until 2026-08-24; `templates/ingressroute.yaml.j2` carries
  the reasoning for the flip. Everything under `docs/` is served, so what is excluded from
  the build is a publishing decision — see `mkdocs.yml`'s `exclude_docs`.
- **Built by:** `scripts/docs/build_docs.py`, run by the `docs-refresh` cron. **Not by this role** —
  a deploy renders manifests, it does not rebuild the site.

## The build runs on the host, never in the pod

The pod holds no repo checkout, no `uv`, and no git credential. `scripts/docs/build_docs.py` runs the
reference generators and `mkdocs build` on `daniel-box`, which already has all three.

Getting the repo into a pod would mean either baking it into an image on every commit or giving
a container a deploy key. Neither buys anything: the one host that must have the checkout already
has it, and it is the same host the hostPath pins the pod to.

**Consequence:** a deploy of this role never changes what the site says. If a page looks stale
after a deploy, the cron is what to check, not the rollout.

## `docs_host_site_dir` sits outside the repo checkout deliberately

`/home/<user>/docs-site`, not `<repo>/site`.

The GitOps tick rewrites the repo tree with `git pull` every 30 minutes. Serving a directory
inside that tree would mean a pull can replace files mid-request. The built site is derived
output with no reason to live under version control, so it lives beside it instead.

`scripts/docs/build_docs.py` also builds to a sibling directory and renames into place, because
`mkdocs build` cleans its `--site-dir` first — building straight into the served path would blank
the site for several seconds on every run and leave it blank after any failure.

## The node pin is load-bearing

`nodeSelector: kubernetes.io/hostname: daniel-box`.

hostPath is node-local. Unpinned, the pod can schedule onto `daniel-server`, mount a path that
does not exist there, and serve an empty tree. That failure reads as "the build is broken" rather
than as a scheduling mistake, which is the expensive kind of wrong.

The `hostPath` volume is `type: Directory`, not `DirectoryOrCreate`. kubelet would create a
missing path as root, and the build script runs as the unprivileged user, so it could then never
write into it. The role's own task creates the directory with the right ownership first.

## `/healthz` is answered by nginx, not from the mounted tree

The probe must not depend on the site being built. A cluster brought up before the first cron run
has an empty `docs_host_site_dir`, and the right behaviour there is a 404 on `/`, not a
crashlooping pod — "the site is not built" is an operator problem, not a rollout failure.

## Verifying a change actually landed

`uv run python scripts/probe/probe.py health docs` gates the rollout and the 180s restart window. It
cannot see whether the site rendered: the Authelia middleware answers with a 302 before Traefik
reaches nginx, so a green probe plus a working redirect proves nothing about content.

To check content, dial the Service directly and bypass the middleware:

```bash
IP=$(kubectl get svc docs -n homelab -o jsonpath='{.spec.clusterIP}')
curl -s "http://$IP:8080/" | grep -o "<title>[^<]*</title>"
```

## Born fenced

The pod template carries `netpol-baseline: enforced`, and `docs` is listed in
`BORN_FENCED_ROLES` in `ansible/tests/test_netpol_baseline_labels.py`. Traefik is its only
caller and the pod dials nothing at all — the site is built on the host, so the container never
fetches, clones or resolves. Adding the label without listing it there fails that test, and
`docs/networkpolicy-default-deny.md` records the same set in prose.
