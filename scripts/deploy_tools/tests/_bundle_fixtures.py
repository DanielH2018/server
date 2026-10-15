"""Bundle fixtures for the publish gates and `decide`'s object-loss check.

`ROUTE` copies bento-pdf's two IngressRoutes as `homelab_render.py cluster --role bento-pdf`
renders them from `ansible/templates/ingressroute-default.yaml.j2`, with the domain made a
parameter. The first, `bento-pdf` with its `.local.` host, is the named member of the host
gate and of the object-loss check.
"""

from renderer.renderer_lib.bundle import flux_files, kustomization_text

ROUTE = """\
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: bento-pdf
  namespace: homelab
  labels:
    homelab/role: bento-pdf
spec:
  entryPoints:
  - https
  routes:
  - kind: Rule
    match: Host(`bento-pdf.local.{domain}`)
    middlewares:
    - name: rate-limit
    - name: authelia
    services:
    - name: bento-pdf
      port: 8080
  tls:
    certResolver: cloudflare
    options:
      name: modern
    domains:
    - main: {domain}
      sans:
      - '*.{domain}'
      - '*.local.{domain}'
---
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: bento-pdf-public
  namespace: homelab
  labels:
    homelab/role: bento-pdf
spec:
  entryPoints:
  - https
  routes:
  - kind: Rule
    match: Host(`bento-pdf.{domain}`)
    middlewares:
    - name: rate-limit-proxied
    - name: authelia
    services:
    - name: bento-pdf
      port: 8080
  tls:
    certResolver: cloudflare
    options:
      name: cloudflare-origin-pull
    domains:
    - main: {domain}
      sans:
      - '*.{domain}'
      - '*.local.{domain}'
"""

LOCAL_ROUTE = "IngressRoute/homelab/bento-pdf"


def route(domain: str) -> str:
    """bento-pdf's rendered routes for `domain`."""
    return ROUTE.replace("{domain}", domain)


def tag_files(tag: str, files: dict[str, str]) -> dict[str, str]:
    """`cluster/<tag>/`: `files` and the `kustomization.yaml` listing them."""
    found = {f"cluster/{tag}/{name}": text for name, text in files.items()}
    found[f"cluster/{tag}/kustomization.yaml"] = kustomization_text(files)
    return found


def bundle(
    cluster: dict[str, dict[str, str]], substitutes: frozenset[str] = frozenset()
) -> dict[str, str]:
    """A bundle publishing each tag in `cluster` with its files, by bundle path.

    Each tag in `substitutes` gets `postBuild`; the rest do not.
    """
    found = flux_files({tag: tag in substitutes for tag in cluster}, "600s")
    for tag, files in cluster.items():
        found |= tag_files(tag, files)
    return found
