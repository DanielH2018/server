# Docs Site and Generated Reference — Implementation Plan (1 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up an MkDocs Material site behind Authelia whose `docs/reference/` pages regenerate from the Ansible tree by cron.

**Architecture:** A new `ansible/roles/k8s/docs` role runs nginx over a hostPath on `daniel-box`, copying the shape of `roles/k8s/artifacts`. The static site is built on the host by `scripts/build_docs.py`, which runs the reference generators and then `mkdocs build`. A cron regenerates, commits, and pushes under `/var/lock/server-git-tree.lock`.

**Tech Stack:** MkDocs Material, nginx-unprivileged, Ansible, k3s, Python 3.14 + uv, pytest.

**Spec:** `docs/docs-ui-and-adrs-design.md`

**Follow-on plan:** `docs/docs-ui-and-adrs-plan-2.md` covers ADRs, Vale, and D2. Do not start it until this plan is merged.

## Global Constraints

- **Python is 3.14 via uv.** Never invoke bare `python3`/`pytest`. Every command is `uv run …`. A PreToolUse hook rewrites bare invocations, but write the commands correctly anyway.
- **Generators parse statically.** They must never shell out to `ansible` or `kubectl` to read *repo* facts. A fresh worktree has no Ansible collections, and `ansible/inventory/*.yml` holds SOPS lookups that do not render outside a deploy. Read YAML with `yaml.safe_load` and template text with regex. Live *cluster* state is the sole exception, and it degrades to declared-only when unreachable.
- **An underivable fact prints its reason, never a guess.** Follow the `"unknown"` precedent in `scripts/service_catalog.py`.
- **`ansible/roles/k8s/<name>/templates/` holds manifests only.** `validate_k8s_manifests.py` renders every `*.j2` there and parses it as YAML. Non-manifest config goes in `templates/config/`, static assets in `files/`.
- **Volume names are descriptive.** `docs-site`, never `site`. ENFORCED by `ansible/tests/test_volume_names_descriptive.py`.
- **Image tags are pinned to a real version** — a version tag or `latest@sha256:…`. Never a bare `:latest`.
- **Commits are signed and hooks run.** Never `--no-verify`, `--no-gpg-sign`, or `core.hooksPath=/dev/null`.
- **Deploy through `./scripts/deploy.sh`**, which takes the git-tree lock. Exit 75 means the lock was busy and nothing deployed; exit 4 means the tree is behind master.
- **New role position in `containers_list`:** after `traefik` and `authelia`. That play has no toposort and runs in list order.

---

### Task 1: MkDocs scaffold and navigation over the existing docs

Builds the site locally from the 19 documents already in `docs/`. Nothing is generated and nothing is deployed yet — this task's deliverable is `mkdocs build --strict` succeeding.

**Files:**
- Create: `mkdocs.yml`
- Create: `docs/index.md`
- Modify: `pyproject.toml` (add `mkdocs-material` to the `dev` group)
- Modify: `.gitignore` (ignore `site/`)
- Test: `scripts/test_mkdocs_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `mkdocs.yml` with a `nav:` list. Task 3 and Task 9 append entries to its `Reference` section.

- [ ] **Step 1: Write the failing test**

The test that matters is not "does MkDocs run" but "does every navigation entry point at a file that exists". A `nav:` pointing at a moved or misspelled document is the failure mode this catches, and `--strict` catches it only at build time.

Create `scripts/test_mkdocs_config.py`:

```python
"""mkdocs.yml navigation must point only at documents that exist.

A nav entry naming a missing file makes `mkdocs build --strict` fail, but that
failure arrives late and reads as a build error rather than a broken link. This
asserts the same property directly against the tree.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
MKDOCS = REPO / "mkdocs.yml"


def _nav_paths(nav: object) -> list[str]:
    """Every document path in a mkdocs `nav:` tree, at any nesting depth."""
    found: list[str] = []
    if isinstance(nav, str):
        found.append(nav)
    elif isinstance(nav, list):
        for entry in nav:
            found.extend(_nav_paths(entry))
    elif isinstance(nav, dict):
        for value in nav.values():
            found.extend(_nav_paths(value))
    return found


def _load_config() -> dict:
    # MkDocs uses `!!python/name:` tags for extensions, which safe_load rejects.
    # The nav needs none of them, so unknown tags are read as plain scalars.
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor(
        "tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix
    )
    _Loader.add_multi_constructor("!", lambda loader, suffix, node: suffix)
    return yaml.load(MKDOCS.read_text(), Loader=_Loader)


def test_every_nav_entry_resolves_to_a_file():
    config = _load_config()
    docs_dir = REPO / config.get("docs_dir", "docs")
    missing = [p for p in _nav_paths(config["nav"]) if not (docs_dir / p).is_file()]
    assert not missing, f"nav entries with no file: {missing}"


def test_nav_covers_every_toplevel_doc():
    """Every docs/*.md is reachable from the nav.

    A document absent from the nav is still built and still served, but nothing
    links to it — which is indistinguishable from it not existing.
    """
    config = _load_config()
    docs_dir = REPO / config.get("docs_dir", "docs")
    listed = set(_nav_paths(config["nav"]))
    on_disk = {p.name for p in docs_dir.glob("*.md")}
    assert not (on_disk - listed), f"docs/*.md missing from nav: {sorted(on_disk - listed)}"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest scripts/test_mkdocs_config.py -v`
Expected: FAIL — `FileNotFoundError` on `mkdocs.yml`, which does not exist yet.

- [ ] **Step 3: Add the dependency**

Add `mkdocs-material` to the `dev` dependency group in `pyproject.toml`, alongside the existing test dependencies. Then:

Run: `uv lock && uv sync`

The `uv.lock in sync with pyproject.toml` prek hook fails the commit if you skip the lock step.

- [ ] **Step 4: Write `mkdocs.yml`**

`docs_dir: docs` points at the existing tree. The nav names the existing files in place — none of them move.

```yaml
---
site_name: Homelab Docs
site_description: Reference and runbooks for the daniel-box / daniel-server / daniel-pi homelab
docs_dir: docs
site_dir: site
use_directory_urls: true

theme:
  name: material
  features:
    - navigation.sections
    - navigation.top
    - navigation.indexes
    - search.highlight
    - content.code.copy
  palette:
    - media: "(prefers-color-scheme: light)"
      scheme: default
      toggle:
        icon: material/brightness-7
        name: Switch to dark mode
    - media: "(prefers-color-scheme: dark)"
      scheme: slate
      toggle:
        icon: material/brightness-4
        name: Switch to light mode

markdown_extensions:
  - admonition
  - attr_list
  - md_in_html
  - tables
  - toc:
      permalink: true
  - pymdownx.details
  - pymdownx.superfences
  - pymdownx.highlight:
      anchor_linenums: true

plugins:
  - search

nav:
  - Home: index.md
  - Runbooks:
      - Secret rotation: secret-rotation.md
      - Longhorn disaster recovery: longhorn-disaster-recovery.md
      - Longhorn upgrade: longhorn-upgrade.md
      - k3s etcd restore: k3s-etcd-restore.md
      - Kopia disaster recovery: kopia-disaster-recovery.md
      - Ubuntu 24.04 upgrade: ubuntu-24.04-upgrade.md
  - Design:
      - Longhorn backup tiering: longhorn-backup-tiering.md
      - NetworkPolicy default-deny: networkpolicy-default-deny.md
      - Zero-downtime deploys: zero-downtime-deploys-design.md
      - Zero-downtime baseline: zero-downtime-baseline.md
      - Zero-downtime plan 2: zero-downtime-deploys-plan-2.md
      - GitOps — Argo and Flux evaluation: gitops-argo-flux-evaluation.md
      - Docs UI and ADRs: docs-ui-and-adrs-design.md
      - Docs UI and ADRs — plan 1: docs-ui-and-adrs-plan-1.md
      - Docs UI and ADRs — plan 2: docs-ui-and-adrs-plan-2.md
  - Operations:
      - Claude shell permissions: claude-shell-permissions.md
      - Security tools: security-tools.md
      - WireGuard private access: wireguard-private-homelab-access.md
      - Healthchecks.io deadman: healthchecks-io-deadman.md
      # Label deliberately omits the word this document's title carries before
      # "drain". With it, gitleaks' generic-api-key rule reads the label plus the
      # high-entropy filename after the colon as a credential and fails the commit.
      - B2 drain scoping: b2-api-drain-scoping.md
      - B2 transaction cap gaps: b2-transaction-cap-monitoring-gaps.md
      - Email to RSS: email-to-rss.md
```

**If `test_nav_covers_every_toplevel_doc` fails**, a document exists that this nav does not name. Add it to whichever section fits; do not delete the assertion.

- [ ] **Step 5: Write `docs/index.md`**

```markdown
# Homelab docs

Reference and runbooks for a three-host homelab: `daniel-box` (k3s server), `daniel-server`
(k3s agent), and `daniel-pi` (the one remaining Docker host).

## Where to start

- **Reference** pages are generated from the Ansible tree. They carry the timestamp and commit
  they were built from, and a cron regenerates them. Do not edit them by hand.
- **Runbooks** are procedures for recovering or upgrading a subsystem. They are hand-written.
- **Design** documents record how a subsystem was built and why.

## What generates what

The reference pages read `containers_list` in `ansible/inventory/host_vars/`, the per-role
IngressRoute macro calls, the Longhorn backup-tier volume lists, and each role's
`k8s_autodeploy` declaration. A fact none of those sources carries prints its reason instead of
a value.
```

- [ ] **Step 6: Add `site/` to `.gitignore`**

Append `site/` to `.gitignore`. The built output is deployed from a host directory, never committed.

- [ ] **Step 7: Run the tests and the strict build**

Run: `uv run pytest scripts/test_mkdocs_config.py -v`
Expected: PASS, both tests.

Run: `uv run mkdocs build --strict`
Expected: exits 0. `--strict` turns broken internal links into errors, which is the point of running it here rather than only in the cron.

Cross-document links in the existing 19 files may break the strict build. If one does, fix the link — do not drop `--strict`.

- [ ] **Step 8: Commit**

```bash
git add mkdocs.yml docs/index.md pyproject.toml uv.lock .gitignore scripts/test_mkdocs_config.py
git commit -m "Add MkDocs Material scaffold over the existing docs

The nav points at docs/*.md where they already are. Moving them to fit a
navigation tree would break every reference to docs/secret-rotation.md and
its siblings across CLAUDE.md, the role docs and the skills.

test_mkdocs_config.py asserts both directions: every nav entry resolves to
a file, and every docs/*.md is reachable from the nav. A document absent
from the nav is still served but nothing links to it, which is
indistinguishable from it not existing."
```

---

### Task 2: The `docs` k3s role — one visible page behind Authelia

The first vertical slice ends here: a browser, an Authelia login, and the site from Task 1. Nothing is generated yet, and the build still runs by hand.

**Files:**
- Create: `ansible/roles/k8s/docs/defaults/main.yml`
- Create: `ansible/roles/k8s/docs/tasks/main.yml`
- Create: `ansible/roles/k8s/docs/templates/deployment.yaml.j2`
- Create: `ansible/roles/k8s/docs/templates/service.yaml.j2`
- Create: `ansible/roles/k8s/docs/templates/ingressroute.yaml.j2`
- Create: `ansible/roles/k8s/docs/templates/configmap.yaml.j2`
- Create: `ansible/roles/k8s/docs/CLAUDE.md`
- Modify: `ansible/inventory/host_vars/daniel-box.yml` (add the `docs` entry to `containers_list`)

**Interfaces:**
- Consumes: `mkdocs.yml` and the built `site/` from Task 1.
- Produces: `docs_host_site_dir` (default `/home/ubuntu/docs-site`) — the host path the pod serves and Task 6's build script writes into.

- [ ] **Step 1: Write `defaults/main.yml`**

```yaml
---
# nginx-unprivileged rather than stock nginx: it listens on 8080 as a non-root uid out of the
# box, so the pod needs neither a root user nor NET_BIND_SERVICE. Stock nginx binds 80, which
# under `capabilities: drop: [ALL]` fails at startup rather than at request time.
docs_k8s_image: nginxinc/nginx-unprivileged:1.29-alpine

# Auto-deploy stance — read by gitops_deploy. Stateless Deployment, RollingUpdate, no PVC,
# HTTP readinessProbe, and the tag above is a real version rather than a mutable :latest.
# Deliberately unprefixed: one constant key name every role uses, so the completeness guard
# can check for it without knowing the role's own prefix.
k8s_autodeploy: true  # noqa var-naming[no-role-prefix]
k8s_autodeploy_reason: "stateless RollingUpdate Deployment, no PVC, readinessProbe gates the rollout, pinned image tag"  # noqa var-naming[no-role-prefix]

# hostPath pins the pod to ONE node. daniel-box holds the repo checkout, uv, and the cron that
# builds the site, so the built tree only ever exists there. Scheduling elsewhere would mount
# an empty directory and serve a 404 index rather than fail visibly.
docs_k8s_node: daniel-box

# Where scripts/build_docs.py writes `mkdocs build` output, and what the pod serves.
# NOT inside the repo checkout: a GitOps tick rewrites that tree with `git pull`, and
# replacing a served directory mid-request is a failure mode with no upside.
docs_host_site_dir: /home/ubuntu/docs-site

# The site is static files served from page cache; nginx is idle between loads.
docs_k8s_cpu_limit: "500m"
docs_k8s_mem_limit: 128Mi
docs_k8s_cpu_request: "10m"
docs_k8s_mem_request: 32Mi
```

- [ ] **Step 2: Write `templates/configmap.yaml.j2`**

nginx needs a config that serves the static tree and answers `/healthz` without touching disk. The probe must not depend on the site being built — an unbuilt site should read as an empty page, not as a crashlooping pod.

```yaml
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: docs-nginx
  namespace: {{ k8s_namespace }}
data:
  default.conf: |
    server {
        listen {{ container_item.port }};
        server_name _;
        root /usr/share/nginx/html;
        index index.html;

        # Answered from nginx itself, never from the mounted tree. A build that has not run
        # yet leaves the tree empty; the pod must still pass its probes and serve a 404,
        # because "the site is not built" is an operator problem, not a rollout failure.
        location = /healthz {
            access_log off;
            add_header Content-Type text/plain;
            return 200 'ok';
        }

        # MkDocs `use_directory_urls: true` emits page/index.html, so a request for /page/
        # resolves by directory index. try_files keeps a missing page a 404 rather than a
        # directory listing.
        location / {
            try_files $uri $uri/ $uri/index.html =404;
        }
    }
```

- [ ] **Step 3: Write `templates/deployment.yaml.j2`**

```yaml
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: docs
  namespace: {{ k8s_namespace }}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: docs
  template:
    metadata:
      labels:
        app: docs
        netpol-baseline: enforced
    spec:
      enableServiceLinks: false
      # hostPath is node-local, so the pod MUST land where the built site is. Unpinned, it
      # schedules onto the other node and serves an empty tree — which reads as "the build
      # is broken" rather than as a scheduling mistake.
      nodeSelector:
        kubernetes.io/hostname: {{ docs_k8s_node }}
      securityContext:
        runAsUser: {{ puid }}
        runAsGroup: {{ pgid }}
        fsGroup: {{ pgid }}
        runAsNonRoot: true
      automountServiceAccountToken: false
      # best-effort: a read-only documentation viewer should lose an eviction race to every
      # workload that serves state. Set explicitly — omitting it leaves the pod at Kubernetes'
      # default priority of 0, which is BELOW homelab-best-effort (1000).
      priorityClassName: homelab-best-effort
      containers:
        - name: docs
          image: {{ docs_k8s_image }}
          env:
            - name: TZ
              value: {{ tz }}
          securityContext:
            readOnlyRootFilesystem: true
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
          ports:
            - containerPort: {{ container_item.port }}
          readinessProbe:
            httpGet:
              path: /healthz
              port: {{ container_item.port }}
            initialDelaySeconds: 3
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /healthz
              port: {{ container_item.port }}
            initialDelaySeconds: 10
            periodSeconds: 30
            failureThreshold: 3
          volumeMounts:
            - name: docs-nginx-conf
              mountPath: /etc/nginx/conf.d
              readOnly: true
            - name: docs-site
              mountPath: /usr/share/nginx/html
              readOnly: true
            # readOnlyRootFilesystem blocks nginx's cache and temp paths, which it writes on
            # any buffered request. emptyDir rather than a relaxed root filesystem.
            - name: docs-nginx-cache
              mountPath: /var/cache/nginx
            - name: docs-nginx-tmp
              mountPath: /tmp
          resources:
            requests:
              cpu: {{ docs_k8s_cpu_request }}
              memory: {{ docs_k8s_mem_request }}
            limits:
              cpu: {{ docs_k8s_cpu_limit }}
              memory: {{ docs_k8s_mem_limit }}
      volumes:
        - name: docs-nginx-conf
          configMap:
            name: docs-nginx
        # type: Directory, not DirectoryOrCreate — kubelet creates a missing path as root,
        # and the build script (running as ubuntu) could then never write into it. The role's
        # own tasks create it with the right ownership first.
        - name: docs-site
          hostPath:
            path: {{ docs_host_site_dir }}
            type: Directory
        - name: docs-nginx-cache
          emptyDir: {}
        - name: docs-nginx-tmp
          emptyDir: {}
```

- [ ] **Step 4: Write `templates/service.yaml.j2` and `templates/ingressroute.yaml.j2`**

`service.yaml.j2` — copy the two-line form `roles/k8s/artifacts/templates/service.yaml.j2` uses: import the `service` macro from `service.yml.j2` with context, then call it with `container_item.name` and `container_item.port`.

`ingressroute.yaml.j2` — same shape as the artifacts role's, importing the `ingressroute` macro from `ingressroute.yml.j2` with context and calling it with `container_item.name`, `container_item.hostname | default(container_item.name)`, `container_item.port`, and `container_item.use_authelia`.

**This route is LAN-only**, unlike the artifacts browser's. Add the macro's LAN-only argument and this comment above the call:

```
LAN-only. The reference pages list every service, its hostname, its auth tier and its
backup posture — a complete map of what runs here and what fronts it. The artifacts
browser is public because a session emits links that must resolve wherever the reader
is; nothing here does. The `authelia` middleware gates it either way.
```

**Read `ansible/templates/ingressroute.yml.j2` before writing this** and use the macro's real parameter name for LAN-only. Do not pass an argument the macro does not accept — the manifest validator will not catch a silently ignored keyword.

- [ ] **Step 5: Write `tasks/main.yml`**

```yaml
---
# ── host side: the tree the pod mounts ─────────────────────────────────────────────────
# The hostPath volume is `type: Directory`, so this must exist before the Deployment starts.
# A missing path leaves the pod in ContainerCreating rather than serving an empty site.
- name: Ensure the built docs site directory exists
  tags: [config]
  ansible.builtin.file:
    path: "{{ docs_host_site_dir }}"
    state: directory
    owner: "{{ sys_user }}"
    group: "{{ sys_user }}"
    mode: "0755"
  when: not k8s_dry_run | bool

# ── cluster side ───────────────────────────────────────────────────────────────────────
- name: Deploy the docs site to the cluster
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_service: docs
    manifests_files:
      - configmap.yaml
      - deployment.yaml
      - service.yaml
      - ingressroute.yaml
```

- [ ] **Step 6: Add the inventory entry**

In `ansible/inventory/host_vars/daniel-box.yml`, add this to `containers_list`. **Position it after the `traefik` and `authelia` entries** — the play runs in list order with no toposort, and the IngressRoute needs Traefik's CRDs and Authelia's middleware to exist already.

```yaml
  # MkDocs Material site over docs/, built on this host by scripts/build_docs.py and served
  # from a hostPath. LAN-only: the reference pages map every service, route and auth tier.
  - name: docs
    platform: k8s
    hostname: "docs"
    port: 8080
    use_authelia: true
```

Grep `port:` in that file first and confirm 8080 is free. If it is taken, pick an unused port and change `defaults/main.yml` and the ConfigMap to match.

- [ ] **Step 7: Validate the manifests without deploying**

Run: `uv run prek run --all-files`
Expected: `Validate rendered k8s manifest templates` passes. It renders every `*.j2` under the role's `templates/` and schema-checks the result, catching Jinja indent bugs and wrong field types.

Run: `./scripts/deploy.sh --tags docs --dry-run`
Expected: exit 0. This applies against the live API server with `--dry-run=server`, the only check that sees CRD schemas — the IngressRoute among them.

If `deploy.yml` fails fast naming `docs` in `k8s_dry_run_unsupported`, something in the role mutates outside `roles/k8s/manifests`. It should not. Find what and remove it rather than adding the exemption.

- [ ] **Step 8: Build the site by hand, then deploy**

```bash
uv run mkdocs build --strict --site-dir /home/ubuntu/docs-site
./scripts/deploy.sh --tags docs
```

Exit 75 means the git-tree lock was busy and nothing deployed — retry. Exit 4 means the tree is behind `origin/master` — pull first, never `--skip-staleness-check`.

- [ ] **Step 9: Verify the rollout, then verify the page**

Run: `uv run python scripts/probe.py health docs`
Expected: exit 0. This gates on the Deployment being fully rolled out *and* on no container restart in the last 180s.

Then load `https://docs.local.<domain>` in a browser. **The health probe cannot see whether the site rendered.** An Authelia 302 fires in the middleware before nginx is reached, so a green probe plus a working redirect proves nothing about content. Confirm the page body is the MkDocs index, and that the search box returns a hit for a word taken from an existing runbook.

- [ ] **Step 10: Write `ansible/roles/k8s/docs/CLAUDE.md`**

Cover, in this order: what the role serves and from where; why the pod is pinned to `daniel-box` and what breaks if that pin is removed; that the site is built on the host and never in the pod, and why — no repo checkout and no git credential belongs in a pod; why `docs_host_site_dir` sits outside the repo checkout, since a GitOps tick rewrites that tree; and that the reference pages are generated, pointing at `scripts/build_docs.py` once Task 6 lands.

- [ ] **Step 11: Commit**

Stage `ansible/roles/k8s/docs` and `ansible/inventory/host_vars/daniel-box.yml`. Commit with this message:

```
Add the docs role: MkDocs Material behind Authelia

nginx-unprivileged over a hostPath on daniel-box, in the shape
roles/k8s/artifacts already uses. The build runs on the host, so no repo
checkout and no git credential enters a pod.

The site directory sits outside the repo checkout deliberately: a GitOps
tick rewrites that tree with git pull, and replacing a served directory
mid-request is a failure mode with no upside.

LAN-only, unlike artifacts. The reference pages map every service, route
and auth tier, and nothing here emits a link that must resolve off-LAN.
```

---

### Task 3: The provenance header every generated page carries

A generated page must say when it was built and from which commit. This is the whole staleness mechanism: no Healthchecks ping, no alert, no monitor. A cron that stops working shows up as a visibly old date on the page itself, which is the reasoning `crons.yml` already records for the infra-map cron.

One module, because four generators will need it and a copy in each drifts.

**Files:**
- Create: `scripts/docs_provenance.py`
- Test: `scripts/test_docs_provenance.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `generated_banner(source: str, *, when: datetime | None = None, sha: str | None = None) -> str` — the full Markdown preamble: YAML frontmatter plus a "do not edit" admonition. `source` is the generator's own path, e.g. `scripts/service_catalog.py`.
  - `head_sha(repo: Path | None = None) -> str` — the short commit SHA, or `"unknown"` when git is unavailable.
  - `write_if_body_changed(path: Path, content: str) -> bool` — writes only when the body below the frontmatter differs. Returns whether it wrote. Tasks 4, 5 and 9 call this instead of `Path.write_text`.

**The timestamp has two jobs that pull against each other, and this task resolves the conflict.**

A stamp regenerated on every run proves the cron is alive. A stamp that changes only with content keeps diffs meaningful. Left unresolved, the first wins by default and the Task 7 cron commits on *every* run — the page is rewritten, `git diff --cached` is never empty, and twice a day becomes roughly 730 commits a year, each triggering master CI and a tick fast-forward. `generated_sha` does not escape this either: HEAD moves whenever anyone merges anything.

The split:

| Signal | Where it lives | What it means |
|---|---|---|
| `generated_at`, `generated_sha` | committed frontmatter | when the **content** last changed |
| build time | the **served page only**, never committed | when the cron last ran |

`write_if_body_changed` is what makes the first half true. The second half is Task 6, Step 6.

- [ ] **Step 1: Write the failing test**

Create `scripts/test_docs_provenance.py`:

```python
"""The provenance banner is the only staleness signal generated docs carry.

There is no monitor and no deadman on the docs cron. A stopped cron surfaces as an
old date on the page, so these assertions are load-bearing rather than cosmetic.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

import pytest

from docs_provenance import generated_banner, head_sha

FIXED = dt.datetime(2026, 8, 24, 14, 30, tzinfo=dt.timezone.utc)


def test_banner_carries_the_timestamp():
    banner = generated_banner("scripts/service_catalog.py", when=FIXED, sha="abc1234")
    assert "2026-08-24" in banner
    assert "14:30" in banner


def test_banner_carries_the_source_and_sha():
    banner = generated_banner("scripts/service_catalog.py", when=FIXED, sha="abc1234")
    assert "scripts/service_catalog.py" in banner
    assert "abc1234" in banner


def test_banner_warns_against_hand_editing():
    """The hook is what enforces this, but the page must say so too.

    Someone reading the rendered page has no view of the prek config.
    """
    banner = generated_banner("scripts/service_catalog.py", when=FIXED, sha="abc1234")
    assert "do not edit" in banner.lower()


def test_banner_opens_with_yaml_frontmatter():
    banner = generated_banner("scripts/service_catalog.py", when=FIXED, sha="abc1234")
    lines = banner.splitlines()
    assert lines[0] == "---"
    assert "---" in lines[1:], "frontmatter block is never closed"


def test_banner_frontmatter_parses_as_yaml():
    import yaml

    banner = generated_banner("scripts/service_catalog.py", when=FIXED, sha="abc1234")
    body = banner.split("---")[1]
    meta = yaml.safe_load(body)
    assert meta["generated_from"] == "scripts/service_catalog.py"
    assert meta["generated_sha"] == "abc1234"


def test_head_sha_falls_back_when_git_is_unavailable(tmp_path):
    """A non-repo directory yields 'unknown', never a traceback.

    The cron runs unattended. A generator that dies because git moved is a worse
    failure than a page whose provenance line reads 'unknown'.
    """
    assert head_sha(tmp_path) == "unknown"


def test_head_sha_reads_the_real_repo():
    sha = head_sha(Path(__file__).resolve().parent.parent)
    assert sha != "unknown"
    assert len(sha) >= 7
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest scripts/test_docs_provenance.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'docs_provenance'`.

- [ ] **Step 3: Write `scripts/docs_provenance.py`**

```python
#!/usr/bin/env python3
"""The provenance banner every generated documentation page opens with.

WHY THIS EXISTS. The generated pages under docs/reference/ carry no monitor, no
Healthchecks ping and no alert. That is deliberate, and it matches what the
infra-map cron already does: a failed run leaves the previous page in place rather
than corrupting anything, so the useful signal is not "the run failed" but "this
page is old". The banner is that signal, and it is the only one.

Every generator calls generated_banner() and writes its return value as the first
bytes of its output file.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# A generated page names the hook that protects it, because a reader of the rendered
# page cannot see prek.toml.
_DO_NOT_EDIT = (
    "!!! warning \"Generated file — do not edit\"\n"
    "    This page is rendered from the Ansible tree by `{source}`. Hand edits are\n"
    "    overwritten by the next run, and a prek hook rejects them at commit time.\n"
    "    To change what appears here, change the generator or the source it reads.\n"
)


def head_sha(repo: Path | None = None) -> str:
    """The short HEAD SHA, or "unknown" when git cannot answer.

    Never raises. The cron runs unattended, and a generator that dies because git
    is missing or the directory is not a repo is a worse outcome than a page whose
    provenance line reads "unknown".
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo or REPO,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip() or "unknown"


def generated_banner(
    source: str,
    *,
    when: dt.datetime | None = None,
    sha: str | None = None,
) -> str:
    """YAML frontmatter plus a do-not-edit admonition, for the top of a page.

    `source` is the generator's repo-relative path. `when` and `sha` are injectable
    so tests do not depend on the clock or on the checkout.
    """
    stamp = when or dt.datetime.now(dt.timezone.utc)
    commit = sha if sha is not None else head_sha()
    iso = stamp.strftime("%Y-%m-%d %H:%M UTC")
    return (
        "---\n"
        f"generated_from: {source}\n"
        f"generated_at: {iso}\n"
        f"generated_sha: {commit}\n"
        "---\n\n"
        + _DO_NOT_EDIT.format(source=source)
        + "\n"
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest scripts/test_docs_provenance.py -v`
Expected: PASS, all seven.

If `test_banner_frontmatter_parses_as_yaml` fails on a `:` inside the source path, the frontmatter needs quoting. Quote the value rather than dropping the assertion.

- [ ] **Step 5: Write the failing tests for `write_if_body_changed`**

Append to `scripts/test_docs_provenance.py`:

```python
from docs_provenance import write_if_body_changed


def _page(body: str, stamp: str) -> str:
    return f"---\ngenerated_at: {stamp}\n---\n\n{body}"


def test_writes_when_the_file_does_not_exist(tmp_path):
    target = tmp_path / "new.md"
    assert write_if_body_changed(target, _page("hello", "A")) is True
    assert target.is_file()


def test_writes_when_the_body_changed(tmp_path):
    target = tmp_path / "p.md"
    target.write_text(_page("old", "A"))
    assert write_if_body_changed(target, _page("new", "B")) is True
    assert "new" in target.read_text()


def test_does_not_write_when_only_the_stamp_changed(tmp_path):
    """The assertion the whole cron depends on.

    Without it every run rewrites every page, the cron's `git diff --cached` is
    never empty, and twice a day becomes ~730 commits a year -- each one a master
    CI run and a tick fast-forward, for no content change at all.
    """
    target = tmp_path / "p.md"
    target.write_text(_page("same", "A"))
    before = target.read_text()
    assert write_if_body_changed(target, _page("same", "B")) is False
    assert target.read_text() == before, "file was rewritten despite an identical body"


def test_creates_parent_directories(tmp_path):
    target = tmp_path / "deep" / "nested" / "p.md"
    assert write_if_body_changed(target, _page("x", "A")) is True
```

- [ ] **Step 6: Implement `write_if_body_changed`**

```python
def _body(text: str) -> str:
    """Everything after the frontmatter block.

    Splitting on the first two '---' delimiters, so a '---' inside the body (a
    Markdown horizontal rule, which several pages use) does not truncate it.
    """
    if not text.startswith("---\n"):
        return text
    parts = text.split("---", 2)
    return parts[2] if len(parts) == 3 else text


def write_if_body_changed(path: Path, content: str) -> bool:
    """Write `content` to `path` only if the body below the frontmatter differs.

    Returns True when it wrote.

    WHY. generated_at and generated_sha change on every run -- the clock moves, and
    HEAD moves whenever anyone merges anything. Writing unconditionally would make
    the docs-refresh cron commit on every run for no content change, which is the
    commit noise this design accepted only in exchange for reviewable diffs. A diff
    that is always a timestamp bump is not reviewable.

    The freshness signal is not lost, it is relocated: the frontmatter stamp now
    means "when the content last changed", and "when the cron last ran" is rendered
    into the served page by build_docs (Task 6) without being committed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and _body(path.read_text()) == _body(content):
        return False
    path.write_text(content)
    return True
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest scripts/test_docs_provenance.py -v`
Expected: PASS, all eleven.

- [ ] **Step 8: Commit**

Stage `scripts/docs_provenance.py` and `scripts/test_docs_provenance.py`. Commit with:

```
Add the provenance banner for generated docs pages

The generated pages carry no monitor and no deadman, matching what the
infra-map cron already does: a failed run leaves the previous page in
place, so the useful signal is not "the run failed" but "this page is
old". The banner is that signal and the only one, which is why its
timestamp and SHA assertions are load-bearing rather than cosmetic.

head_sha() never raises. The cron runs unattended, and a generator dying
because git moved is worse than a page reading "unknown".
```

---

### Task 4: `service_catalog.py` emits Markdown

`build_rows()` already returns `list[ServiceRow]` and `render_html()` is a pure function of that list. The Markdown emitter is a sibling of `render_html()`, not a rewrite.

**Files:**
- Modify: `scripts/service_catalog.py` (add `render_markdown()`, add `--format`)
- Modify: `scripts/test_service_catalog.py` (add the Markdown cases)
- Modify: `mkdocs.yml` (add the `Reference` nav section)
- Create: `docs/reference/services.md` (generated, committed)

**Interfaces:**
- Consumes: `generated_banner()` from Task 3; `ServiceRow` and `build_rows()`, which already exist.
- Produces: `render_markdown(rows: list[ServiceRow]) -> str`. Task 6 calls `service_catalog.py --format markdown --out docs/reference/services.md`.

- [ ] **Step 1: Read the existing renderer and row type**

Read `scripts/service_catalog.py` around `class ServiceRow` (line 79) and `render_html` (line 397). `ServiceRow` has seven fields: `name`, `host`, `platform`, `route`, `auth_tier`, `backup_tier`, `autodeploy`. The Markdown table carries the same seven — do not invent columns, and do not drop any.

- [ ] **Step 2: Write the failing tests**

Append to `scripts/test_service_catalog.py`:

```python
def test_markdown_has_one_row_per_service():
    rows = [
        ServiceRow("sonarr", "daniel-box", "k8s", "sonarr", "authelia", "weekly", "yes"),
        ServiceRow("wg-easy", "daniel-pi", "docker", "LAN-direct", "none", "none", "no"),
    ]
    out = render_markdown(rows)
    assert out.count("| sonarr |") == 1
    assert out.count("| wg-easy |") == 1


def test_markdown_groups_by_host():
    """Grouped by host, matching render_html. A flat 59-row table is unreadable."""
    rows = [
        ServiceRow("sonarr", "daniel-box", "k8s", "sonarr", "authelia", "weekly", "yes"),
        ServiceRow("wg-easy", "daniel-pi", "docker", "LAN-direct", "none", "none", "no"),
    ]
    out = render_markdown(rows)
    assert "## daniel-box" in out
    assert "## daniel-pi" in out
    assert out.index("## daniel-box") < out.index("| sonarr |")


def test_markdown_opens_with_the_provenance_banner():
    out = render_markdown([])
    assert out.startswith("---\n")
    assert "generated_from: scripts/service_catalog.py" in out
    assert "do not edit" in out.lower()


def test_markdown_escapes_pipes_in_values():
    """A literal | in a cell splits the row into extra columns silently.

    No current value contains one, but route and backup_tier are derived from
    template text and nothing stops one appearing.
    """
    rows = [ServiceRow("odd", "daniel-box", "k8s", "a|b", "authelia", "none", "no")]
    out = render_markdown(rows)
    row_line = next(ln for ln in out.splitlines() if ln.startswith("| odd |"))
    assert row_line.count("|") == 8, f"pipe count wrong, row split: {row_line}"


def test_markdown_counts_unknown_fields():
    """The existing HTML renderer surfaces an unknown count; Markdown must too.

    An underivable fact silently rendered as a blank cell is the failure the
    'unknown' convention exists to prevent.
    """
    rows = [ServiceRow("x", "daniel-box", "k8s", "unknown", "authelia", "none", "no")]
    out = render_markdown(rows)
    assert "unknown" in out.lower()
```

Add `render_markdown` to the module's import line at the top of the test file.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest scripts/test_service_catalog.py -v -k markdown`
Expected: FAIL — `ImportError: cannot import name 'render_markdown'`.

- [ ] **Step 4: Implement `render_markdown()`**

Add to `scripts/service_catalog.py`, next to `render_html()`:

```python
def _md_cell(value: str) -> str:
    """A table cell that cannot split its own row.

    A literal pipe in a value adds a column silently — the table still renders,
    just wrong, which is worse than failing.
    """
    return value.replace("|", "\\|")


def render_markdown(rows: list[ServiceRow]) -> str:
    """The service catalogue as a MkDocs page, grouped by host.

    Same seven facts as render_html and the same "unknown" convention: a fact this
    cannot derive prints its reason rather than a blank cell.
    """
    from docs_provenance import generated_banner

    parts = [generated_banner("scripts/service_catalog.py")]
    parts.append("# Services\n")
    parts.append(
        f"{len(rows)} service(s) declared across "
        f"{len({r.host for r in rows})} host(s).\n"
    )

    header = "| Service | Platform | Route | Auth | Backup tier | Auto-deploy |"
    divider = "|---|---|---|---|---|---|"

    for host in sorted({r.host for r in rows}):
        host_rows = sorted((r for r in rows if r.host == host), key=lambda r: r.name)
        parts.append(f"\n## {host}\n")
        parts.append(f"{len(host_rows)} service(s).\n")
        parts.append(header)
        parts.append(divider)
        for row in host_rows:
            parts.append(
                "| "
                + " | ".join(
                    _md_cell(v)
                    for v in (
                        row.name,
                        row.platform,
                        row.route,
                        row.auth_tier,
                        row.backup_tier,
                        row.autodeploy,
                    )
                )
                + " |"
            )

    unknowns = sum(
        1
        for r in rows
        for f in (r.route, r.auth_tier, r.backup_tier, r.autodeploy)
        if f == "unknown"
    )
    parts.append(
        f"\n## Underivable facts\n\n{unknowns} field(s) read `unknown`. "
        "A fact with no machine-readable source prints its reason rather than a guess — "
        "see the FIELD NOTES section of `scripts/service_catalog.py` for which facts those "
        "are and why.\n"
    )
    return "\n".join(parts) + "\n"
```

- [ ] **Step 5: Add the `--format` flag**

In `main()`, change `--out`'s help text to drop "HTML", and add:

```python
    parser.add_argument(
        "--format",
        choices=("html", "markdown"),
        default="html",
        help="output format (default: html, for the standalone artifact page)",
    )
```

Then replace the write line:

```python
    from docs_provenance import write_if_body_changed

    if args.format == "markdown":
        # Not write_text: an unconditional write changes the timestamp on every run,
        # which makes the docs-refresh cron commit on every run for no content change.
        wrote = write_if_body_changed(args.out, render_markdown(rows))
        print(f"service_catalog: {len(rows)} service(s), "
              f"{'wrote' if wrote else 'unchanged'} {args.out}")
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(render_html(rows))
        print(f"service_catalog: wrote {len(rows)} service(s) to {args.out}")
```

The default stays `html` so the existing standalone artifact behaviour is unchanged. The HTML path keeps the unconditional write — it targets `~/.claude/artifacts/`, which is not committed and has no diff to protect.

- [ ] **Step 6: Run the full suite for this script**

Run: `uv run pytest scripts/test_service_catalog.py -v`
Expected: PASS, including the pre-existing HTML tests. If an HTML test broke, the `--format` change altered a shared path — fix it rather than adjusting the test.

- [ ] **Step 7: Generate the page and wire the nav**

```bash
uv run python scripts/service_catalog.py --format markdown --out docs/reference/services.md
```

Add a `Reference` section to `mkdocs.yml`'s nav, immediately after `Home`:

```yaml
  - Reference:
      - Services: reference/services.md
```

Run: `uv run mkdocs build --strict`
Expected: exits 0.

Open `docs/reference/services.md` and confirm the row count matches `grep -c '^  - name:' ansible/inventory/host_vars/*.yml` summed across hosts. A mismatch means `build_rows` dropped a service; find out which before continuing.

- [ ] **Step 8: Commit**

Stage `scripts/service_catalog.py`, `scripts/test_service_catalog.py`, `docs/reference/services.md`, and `mkdocs.yml`. Commit with:

```
service_catalog: emit Markdown for the docs site

build_rows() already returned a list of ServiceRow and render_html() was
already a pure function of it, so this is a sibling renderer rather than a
rewrite. --format defaults to html, leaving the standalone artifact page
unchanged.

_md_cell escapes pipes. A literal pipe in a derived value adds a column
silently -- the table still renders, just wrong, which is worse than
failing. Route and backup_tier come from template text, so nothing stops
one appearing.
```

---

### Task 5: `gen_infra_map.py` emits a standalone SVG

`_diagram_view()` at `scripts/infra_map_render.py:361` already returns a complete `<svg class="dg" viewBox="0 0 1140 900">…</svg>`. Its colours do not travel with it: every fill and stroke comes from the module-level `STYLE` string, injected into the page's `<style>` element at line 687. Embedded in Markdown, that SVG renders as unstyled black shapes.

The fix is to inline the stylesheet as a `<style>` child of the `<svg>` element. This is the whole task — the drawing code does not change.

**Files:**
- Modify: `scripts/infra_map_render.py` (add `render_svg()`)
- Modify: `scripts/gen_infra_map.py` (add `--format`, re-export `render_svg`)
- Modify: `scripts/test_gen_infra_map.py` (add the SVG cases)
- Create: `docs/reference/topology.md` (generated, committed)
- Create: `docs/assets/generated/infra-map.svg` (generated, committed)

**Interfaces:**
- Consumes: `_diagram_view(model)` and `STYLE`, both already in `infra_map_render`.
- Produces: `render_svg(model: dict) -> str` — a standalone SVG document, valid on its own. Task 6 calls `gen_infra_map.py --format svg --out docs/assets/generated/infra-map.svg`.

- [ ] **Step 1: Read the two pieces you are joining**

Read `scripts/infra_map_render.py:361` (`_diagram_view`), its return at lines 595-596, and the `STYLE` constant used at line 687. Confirm for yourself that the CSS selectors `_diagram_view` relies on (`.box`, `.edge`, `.t-title`, `.t-sub`, `.t-edge`, and the `s-<status>` classes) are all defined in `STYLE`. If some live in a different constant, inline that one too.

- [ ] **Step 2: Write the failing tests**

Append to `scripts/test_gen_infra_map.py`, reusing whatever model fixture the existing tests build:

```python
def test_svg_is_a_standalone_document(sample_model):
    out = render_svg(sample_model)
    assert out.lstrip().startswith("<svg")
    assert out.rstrip().endswith("</svg>")
    assert "<html" not in out.lower()


def test_svg_carries_its_own_styles(sample_model):
    """The whole point of the task.

    _diagram_view's colours come from the page-level STYLE block. Embedded in
    Markdown there is no page, so an SVG without an inline <style> renders as
    unstyled black boxes -- which looks like a broken diagram, not a missing
    stylesheet.
    """
    out = render_svg(sample_model)
    assert "<style" in out
    style_block = out.split("<style")[1].split("</style>")[0]
    assert ".box" in style_block
    assert ".edge" in style_block


def test_svg_keeps_the_status_tinting(sample_model):
    """Live status is the reason this diagram beats a static one."""
    out = render_svg(sample_model)
    assert "s-" in out, "no status classes on any node"


def test_svg_declares_a_viewbox(sample_model):
    """Without a viewBox an embedded SVG does not scale to its container."""
    out = render_svg(sample_model)
    assert 'viewBox="' in out


def test_svg_parses_as_xml(sample_model):
    """An SVG that does not parse renders as nothing, with no error anywhere."""
    import xml.etree.ElementTree as ET

    ET.fromstring(render_svg(sample_model))
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest scripts/test_gen_infra_map.py -v -k svg`
Expected: FAIL — `render_svg` is not defined.

- [ ] **Step 4: Implement `render_svg()`**

Add to `scripts/infra_map_render.py`, next to `render_html()`:

```python
def render_svg(model: dict) -> str:
    """The architecture diagram as a standalone SVG document.

    _diagram_view already emits a complete <svg> element, but its fills and strokes
    resolve against the page-level STYLE block. Embedded in a Markdown page there is
    no such block, and the diagram renders as unstyled black shapes -- which reads as
    a broken diagram rather than as a missing stylesheet. Inlining the CSS as a
    <style> child of the <svg> makes the element carry its own appearance.

    xmlns is required: a bare <svg> works inside HTML, but a .svg file served on its
    own is parsed as XML and needs the namespace declared.
    """
    svg = _diagram_view(model)
    styled = svg.replace(
        ">", f'><style><![CDATA[{STYLE}]]></style>', 1
    )
    return styled.replace(
        "<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1
    )
```

**If `_diagram_view` returns more than the `<svg>` element** — a wrapping `<figure>` or a caption `<div>` — extract just the `<svg>…</svg>` span before styling it. Read the function's return rather than assuming.

- [ ] **Step 5: Add the `--format` flag to the CLI**

In `scripts/gen_infra_map.py`, re-export `render_svg` alongside the other public names it already re-exports, then add to its argument parser:

```python
    parser.add_argument(
        "--format",
        choices=("html", "svg"),
        default="html",
        help="output format (default: html, for the standalone artifact page)",
    )
```

and select the renderer at the write site, exactly as Task 4 did for `service_catalog.py` — including `write_if_body_changed` for the SVG path. The SVG carries no frontmatter, so the helper falls back to comparing the whole text, which is the behaviour wanted here.

**Check whether `_diagram_view` stamps a generation time into the drawing.** If it does, the SVG differs on every run and the helper cannot suppress the write. Move that stamp out of the SVG and into `docs/reference/topology.md`'s prose, which is hand-written and therefore not rewritten by the generator at all.

Default stays `html`, so the cron that writes `~/.claude/artifacts/homelab-infra-map.html` keeps working unchanged.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest scripts/test_gen_infra_map.py -v`
Expected: PASS, including the pre-existing HTML tests.

- [ ] **Step 7: Generate the page and check it renders**

```bash
uv run python scripts/gen_infra_map.py --format svg --out docs/assets/generated/infra-map.svg
```

Write `docs/reference/topology.md` by hand — this page is prose around a generated image, so only the SVG is generated:

```markdown
# Topology

How a request reaches a workload, and what it runs on.

![Homelab infrastructure map](../assets/generated/infra-map.svg)

The diagram's *shape* is fixed — the request path, the two cluster nodes, the Longhorn
backup chain and the Pi's LAN-only plane live in role templates rather than in
`containers_list`, so they cannot be derived. Every name, address, count and status colour
on it is read from the inventory and the live cluster.

A node whose status colour reads unknown was not reachable when the diagram was generated.
The render degrades to declared-only rather than failing, so a partial map is expected after
a cluster restart and is not itself a fault.
```

Add to `mkdocs.yml`'s `Reference` nav section, after `Services`:

```yaml
      - Topology: reference/topology.md
```

Run: `uv run mkdocs build --strict`
Expected: exits 0.

**Then open the built page in a browser and look at the diagram.** `--strict` proves the image path resolves; it says nothing about whether the SVG rendered. Confirm the boxes are coloured, the arrows have their heads, and the labels are readable. Unstyled black shapes mean Step 4's CSS inlining did not take.

- [ ] **Step 8: Commit**

Stage the two scripts, the test file, `docs/reference/topology.md`, `docs/assets/generated/infra-map.svg`, and `mkdocs.yml`. Commit with:

```
gen_infra_map: emit a standalone SVG for embedding

_diagram_view already returned a complete <svg> element, but its fills and
strokes resolved against the page-level STYLE block. Embedded in Markdown
there is no such block, so the diagram rendered as unstyled black shapes --
which reads as a broken diagram rather than a missing stylesheet.

render_svg inlines the CSS as a <style> child and declares xmlns, which a
.svg served on its own needs and an inline <svg> does not. The drawing code
is untouched. --format defaults to html, so the artifact cron is unchanged.
```

---

### Task 6: `build_docs.py` — one command that regenerates and builds

Everything so far runs by hand. This task collapses it into one script, which Task 7 then schedules. Writing the script before the cron means the cron has nothing to get wrong.

**Files:**
- Create: `scripts/build_docs.py`
- Test: `scripts/test_build_docs.py`

**Interfaces:**
- Consumes: `service_catalog.py --format markdown`, `gen_infra_map.py --format svg`.
- Produces: `GENERATORS` — a list of `(argv, output_path)` pairs. Task 9 appends to it. `main(argv)` returns 0 on success and 1 when any generator fails.

- [ ] **Step 1: Write the failing test**

The behaviour worth testing is failure handling, not the happy path. A generator that dies must not take the site build down with it — a stale page is better than no page, which is the same reasoning the infra-map cron already records.

Create `scripts/test_build_docs.py`:

```python
"""build_docs must degrade rather than abort.

One failing generator leaves one stale page. One failing generator that aborts the
run leaves every page stale, and the site build never happens at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import build_docs


def test_a_failing_generator_does_not_stop_the_others(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        code = 1 if "service_catalog.py" in " ".join(argv) else 0
        return subprocess.CompletedProcess(argv, code, "", "boom")

    monkeypatch.setattr(build_docs.subprocess, "run", fake_run)
    build_docs.run_generators()

    ran = " ".join(" ".join(c) for c in calls)
    assert "gen_infra_map.py" in ran, "a later generator was skipped after an earlier failure"


def test_run_generators_reports_which_failed(monkeypatch):
    def fake_run(argv, **kwargs):
        code = 1 if "service_catalog.py" in " ".join(argv) else 0
        return subprocess.CompletedProcess(argv, code, "", "boom")

    monkeypatch.setattr(build_docs.subprocess, "run", fake_run)
    failed = build_docs.run_generators()
    assert len(failed) == 1
    assert "service_catalog.py" in failed[0]


def test_main_exits_nonzero_when_a_generator_failed(monkeypatch):
    monkeypatch.setattr(build_docs, "run_generators", lambda: ["scripts/service_catalog.py"])
    monkeypatch.setattr(build_docs, "build_site", lambda site_dir: True)
    assert build_docs.main(["--site-dir", "/tmp/x"]) == 1


def test_main_builds_the_site_even_when_a_generator_failed(monkeypatch):
    """The stale-page-beats-no-page rule, asserted."""
    built: list[str] = []
    monkeypatch.setattr(build_docs, "run_generators", lambda: ["scripts/service_catalog.py"])
    monkeypatch.setattr(build_docs, "build_site", lambda site_dir: built.append(site_dir) or True)
    build_docs.main(["--site-dir", "/tmp/x"])
    assert built == ["/tmp/x"]


def test_main_exits_nonzero_when_the_site_build_failed(monkeypatch):
    monkeypatch.setattr(build_docs, "run_generators", lambda: [])
    monkeypatch.setattr(build_docs, "build_site", lambda site_dir: False)
    assert build_docs.main(["--site-dir", "/tmp/x"]) == 1


def test_every_generator_output_lands_under_docs():
    """A generator writing outside docs/ would escape the hand-edit hook."""
    for _argv, out in build_docs.GENERATORS:
        assert out.startswith("docs/"), f"{out} is outside docs/"


def test_a_failed_build_leaves_the_previous_site_in_place(tmp_path, monkeypatch):
    """The pod serves this directory. A failed build must not empty it.

    mkdocs cleans its --site-dir before writing, so building straight into the served
    path would blank the site on every failure and for several seconds on every
    success.
    """
    final = tmp_path / "site"
    final.mkdir()
    (final / "index.html").write_text("previous")

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "build error")

    monkeypatch.setattr(build_docs.subprocess, "run", fake_run)
    assert build_docs.build_site(str(final)) is False
    assert (final / "index.html").read_text() == "previous"


def test_the_build_stamp_is_written_into_the_site_not_the_repo(tmp_path):
    """The cron-liveness signal must never be committed.

    Committing it would rewrite a tracked file on every run, which is exactly the
    commit-per-run problem write_if_body_changed exists to prevent.
    """
    site = tmp_path / "site"
    site.mkdir()
    build_docs._write_build_stamp(site)
    assert (site / "build-info.json").is_file()
    assert not (build_docs.REPO / "build-info.json").exists()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest scripts/test_build_docs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'build_docs'`.

- [ ] **Step 3: Write `scripts/build_docs.py`**

```python
#!/usr/bin/env python3
"""Regenerate the reference pages, then build the MkDocs site.

WHY ONE SCRIPT. The cron in roles/setup/initial_setup/tasks/crons.yml calls this and
nothing else. Every decision about what to generate, in what order, and what to do when
one fails lives here in Python, where it is testable -- not in a cron job line, where it
is not.

FAILURE POLICY. A generator that fails is logged and skipped, and the site is built
anyway. This is the same reasoning the infra-map cron already records: a failed run
leaves the previous page in place rather than corrupting anything, and the page carries
its own generation timestamp, so a generator that stops working surfaces as one visibly
stale page. Aborting the run instead would make every page stale to hide that one page
is.

The exit code still reports the failure, so the caller can alert on it if it ever wants
to. The site build is not conditional on it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (argv, output path relative to the repo root). Order is not significant -- no generator
# reads another's output. Every output must sit under docs/, which is what the hand-edit
# hook protects; test_build_docs.py asserts that.
GENERATORS: list[tuple[list[str], str]] = [
    (
        ["scripts/service_catalog.py", "--format", "markdown", "--out",
         "docs/reference/services.md"],
        "docs/reference/services.md",
    ),
    (
        ["scripts/gen_infra_map.py", "--format", "svg", "--out",
         "docs/assets/generated/infra-map.svg"],
        "docs/assets/generated/infra-map.svg",
    ),
]


def run_generators() -> list[str]:
    """Run every generator. Returns the scripts that failed, never raises."""
    failed: list[str] = []
    for argv, out in GENERATORS:
        script = argv[0]
        (REPO / out).parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["uv", "run", "python", *argv],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if result.returncode != 0:
            failed.append(script)
            print(f"build_docs: {script} FAILED rc={result.returncode}", file=sys.stderr)
            print(result.stderr.strip()[:2000], file=sys.stderr)
        else:
            print(f"build_docs: {script} ok -> {out}")
    return failed


def build_site(site_dir: str) -> bool:
    """`mkdocs build --strict`, swapped into place atomically. Returns True on success.

    --strict turns a broken internal link into a build failure. A docs site that
    silently serves dead links is the failure this whole system exists to prevent, so
    the strictness is not negotiable here.

    WHY NOT BUILD STRAIGHT INTO site_dir. mkdocs cleans its --site-dir first, so a
    direct build empties the tree the docs pod is serving and refills it over several
    seconds. The role's own defaults already record the principle -- "replacing a
    served directory mid-request is a failure mode with no upside" -- which is why
    site_dir sits outside the repo checkout. Building to a sibling and renaming into
    place applies the same principle to the build itself. os.replace is atomic within
    a filesystem, and the sibling is deliberately in the same parent so it is one.

    A failed build leaves the previous site serving, untouched.
    """
    final = Path(site_dir)
    staging = final.parent / f"{final.name}.new"
    previous = final.parent / f"{final.name}.old"

    result = subprocess.run(
        ["uv", "run", "mkdocs", "build", "--strict", "--site-dir", str(staging)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        print("build_docs: mkdocs build FAILED (previous site left serving)",
              file=sys.stderr)
        print(result.stderr.strip()[:4000], file=sys.stderr)
        shutil.rmtree(staging, ignore_errors=True)
        return False

    _write_build_stamp(staging)

    shutil.rmtree(previous, ignore_errors=True)
    if final.exists():
        os.replace(final, previous)
    os.replace(staging, final)
    shutil.rmtree(previous, ignore_errors=True)
    print(f"build_docs: site built -> {final}")
    return True


def _write_build_stamp(site: Path) -> None:
    """When the cron last ran, written into the SERVED site and never committed.

    This is the other half of the split Task 3 makes. The committed frontmatter says
    when a page's content last changed; this says when the build last ran, which is
    what proves the cron is alive. Keeping it out of the repo is what stops every run
    producing a commit.
    """
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    (site / "build-info.json").write_text(
        json.dumps({"built_at": stamp}, indent=2) + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--site-dir",
        default="/home/ubuntu/docs-site",
        help="where mkdocs writes the built site (the hostPath the docs pod serves)",
    )
    parser.add_argument(
        "--skip-generators",
        action="store_true",
        help="build the site from the committed pages without regenerating them",
    )
    args = parser.parse_args(argv)

    failed = [] if args.skip_generators else run_generators()
    built = build_site(args.site_dir)

    if failed:
        print(f"build_docs: {len(failed)} generator(s) failed: {', '.join(failed)}",
              file=sys.stderr)
    return 0 if built and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest scripts/test_build_docs.py -v`
Expected: PASS, all six.

- [ ] **Step 5: Run it for real**

```bash
uv run python scripts/build_docs.py --site-dir /home/ubuntu/docs-site
```

Expected: exits 0, prints one `ok ->` line per generator and one `site built ->` line.

Then run it a **second** time and check `git status`.

Expected: completely clean, and each generator printing `unchanged`. This is the assertion the whole cron depends on — `write_if_body_changed` should have suppressed every write, because nothing about the tree changed between the two runs.

**A dirty tree here means the Task 7 cron will commit on every run.** Find the field that moved. The usual causes are a timestamp inside a generator's body rather than in its frontmatter, and dictionary or set iteration that is not sorted. Fix it before continuing; do not proceed and rely on catching it in Task 7.

Confirm `build-info.json` exists in `/home/ubuntu/docs-site/` and is **not** in `git status`.

- [ ] **Step 6: Surface the build stamp on the page**

`build-info.json` proves the cron ran, but only to someone who fetches it. Make it visible: add a small script to `docs/index.md` that fetches `/build-info.json` and writes the timestamp into the page, degrading silently when the fetch fails.

This is the reader-facing half of the staleness signal. A page's own frontmatter says when its content last changed; this says when the site was last built. Both are needed, and neither substitutes for the other — a page that has not changed in three months is fine, and a site that has not built in three months is not.

- [ ] **Step 7: Commit**

Stage `scripts/build_docs.py`, `scripts/test_build_docs.py` and `docs/index.md`. Commit with:

```
Add build_docs.py: regenerate the reference pages, then build the site

The cron calls this and nothing else, so every decision about what to
generate and what to do when one fails lives in Python where it is
testable, rather than in a cron job line where it is not.

A failing generator is logged and skipped, and the site builds anyway.
That is the infra-map cron's reasoning applied here: a failed run leaves
the previous page in place, and the page carries its own timestamp, so one
broken generator surfaces as one visibly stale page. Aborting would make
every page stale to hide that one page is. The exit code still reports it.
```

---

### Task 7: The cron that regenerates, commits, and pushes

This is the task that makes the site self-maintaining. It is also the one that touches the shared git tree, so it copies `secret-rotate.sh.j2`'s structure closely rather than inventing one.

**Read `ansible/roles/setup/initial_setup/templates/secret-rotate.sh.j2` in full before writing anything.** It is the only other cron here that commits and pushes, and it has been through the failure modes already.

**Files:**
- Create: `ansible/roles/setup/initial_setup/templates/docs-refresh.sh.j2`
- Modify: `ansible/roles/setup/initial_setup/tasks/crons.yml` (schedule it)

**Interfaces:**
- Consumes: `scripts/build_docs.py` from Task 6.
- Produces: nothing another task reads.

- [ ] **Step 1: Write the script**

Create `ansible/roles/setup/initial_setup/templates/docs-refresh.sh.j2`:

```bash
#!/usr/bin/env bash
# Regenerate docs/reference/, rebuild the MkDocs site, commit and push any change.
#
# THE LOCK IS THE POINT. This mutates the primary git tree, which gitops-deploy.service
# rewrites with `git pull` on a 30-minute timer and which every deploy renders its
# templates from. /var/lock/server-git-tree.lock is what makes those mutually exclusive.
# Without it a `git pull` mid-run would leave this committing a tree it never read.
#
# WHY IT COMMITS. The generated pages are reviewable in a diff and survive the pod. The
# alternative -- rendering at serve time -- is always current and completely invisible:
# a generator bug would reach the page with nothing to catch it.
#
# IT PUSHES DIRECTLY TO MASTER. Inherited rather than decided: secret-rotate.sh already
# does this, so the path and its permissions are proven. Worth seeing as a conscious choice
# -- a docs commit bypasses PR review, which is acceptable only because every byte it
# commits was produced by a generator whose output is itself tested.
#
# NO DEADMAN. Deliberate, and the same call the infra-map cron makes. Every generated page
# carries the timestamp and SHA it was built from, so a cron that stops working shows up as
# a visibly stale date on the page itself. A monitor would add an alert channel to maintain
# for a failure that is already self-evident where it matters.
set -uo pipefail

REPO="/home/{{ sys_user }}/server"
UV="/home/{{ sys_user }}/.local/bin/uv"
SITE_DIR="{{ docs_host_site_dir | default('/home/' + sys_user + '/docs-site') }}"

alert() { logger -t docs-refresh "$1"; }

cd "$REPO" || { alert "docs-refresh: $REPO missing"; exit 1; }

# fd 9 held for the script's life. -w 2700 matches secret-rotate: longer than a full
# gitops deploy plus rollback, so a legitimately busy tree waits rather than skipping.
exec 9>/var/lock/server-git-tree.lock
flock -w 2700 9 || { alert "docs-refresh: git-tree lock held >45m; skipped"; exit 1; }

# A dirty tree means a human or another job is mid-edit. Committing on top of that would
# mix their work into a docs commit. Build the site anyway -- serving current docs costs
# nothing and is the useful half -- but change no files.
if ! git diff --quiet || ! git diff --cached --quiet; then
  alert "docs-refresh: working tree dirty; rebuilt site only, no regeneration"
  $UV run --frozen python scripts/build_docs.py --skip-generators --site-dir "$SITE_DIR" \
    || alert "docs-refresh: site build failed on dirty tree"
  exit 0
fi

# DELIBERATELY NO `git pull`. gitops-deploy.service already fast-forwards this checkout
# every 30 minutes, and it CI-gates the range first: a commit whose master CI is red or
# pending is refused, and a failed health gate parks the SHA in hold_sha. A bare pull here
# would take the lock correctly and still skip all of that, fast-forwarding onto whatever
# origin/master holds — including a commit the tick would have refused. The next deploy
# then renders templates from that tree.
#
# This cron runs twice a day and the tick runs 48 times, so reading whatever the tick has
# already gated in costs at most one interval of freshness and removes the interaction
# entirely.

# --frozen: install from the committed uv.lock, never rewrite it on the host. A lock
# rewrite here would show up as an unrelated dirty file on the next run.
if ! $UV run --frozen python scripts/build_docs.py --site-dir "$SITE_DIR"; then
  alert "docs-refresh: one or more generators failed (site was still built)"
  # Deliberately NOT an exit. build_docs already skipped the failing generator and built
  # the site from what succeeded, so the pages that did regenerate are worth committing.
fi

# Only the generated trees. A generator that wrote elsewhere would otherwise be committed
# silently by a cron, which is exactly the class of change that must go through a PR.
git add docs/reference docs/assets/generated

if git diff --cached --quiet; then
  logger -t docs-refresh "no change"
  exit 0
fi

# Hooks run. Unlike secret-rotate, this commit has no live-but-uncommitted state to
# strand: if a hook rejects it, nothing has been deployed and the right outcome is a
# clean abort.
#
# A hook that can fail on GENERATED content wedges this cron permanently -- it would
# abort, alert and exit 1 on every run until someone fixed the generator. Plan 2's Vale
# hook is therefore scoped to exclude docs/reference/. Before adding any hook that
# matches a path staged above, check it cannot fail on generator output.
if ! git commit -m "docs: refresh generated reference pages" >/dev/null 2>&1; then
  git reset >/dev/null 2>&1
  alert "docs-refresh: commit failed (hook rejection?); unstaged, no change pushed"
  exit 1
fi

if git push >/dev/null 2>&1; then
  logger -t docs-refresh "regenerated, committed and pushed"
else
  alert "docs-refresh: committed but git push FAILED (commit is local)"
  exit 1
fi
```

- [ ] **Step 2: Check the shell template validator passes**

Run: `uv run prek run --all-files`
Expected: the `Validate rendered shell templates (bash -n + shellcheck)` hook passes. It renders the `.j2` and runs `bash -n` plus shellcheck on the result.

Fix anything shellcheck reports. Do not add a blanket `# shellcheck disable`.

- [ ] **Step 3: Schedule it**

Add to `ansible/roles/setup/initial_setup/tasks/crons.yml`, following the shape of the infra-map entry already there. Install the script with `ansible.builtin.template` first, then the cron entry.

Twice a day rather than every 30 minutes: the inputs are repo files, which only change when someone merges a PR, and every run that finds a change produces a commit and a CI run. Hourly would generate noise proportional to nothing.

```yaml
- name: Install the docs refresh script (box only)
  tags: [crons, docs]
  when: inventory_hostname == 'daniel-box'
  ansible.builtin.template:
    src: docs-refresh.sh.j2
    dest: /usr/local/bin/docs-refresh.sh
    owner: root
    group: root
    mode: "0755"
  become: true

- name: Schedule the docs refresh (box only)
  tags: [crons, docs]
  # Regenerates docs/reference/, rebuilds the MkDocs site into the hostPath the docs pod
  # serves, and commits any change. Runs on daniel-box because that is where the primary
  # checkout, uv, and the served site directory all are.
  #
  # No Healthchecks ping, matching the infra-map cron above: every generated page carries
  # the timestamp and SHA it was built from, so a run that stops working shows up as a
  # visibly stale date on the page rather than needing its own alert channel.
  #
  # Twice daily, not hourly. The inputs are repo files that change only when a PR merges,
  # and each run finding a change produces a commit and a CI run.
  when: inventory_hostname == 'daniel-box'
  ansible.builtin.cron:
    name: "Refresh generated docs"
    minute: "17"
    hour: "6,18"
    user: "{{ sys_user }}"
    job: "/usr/local/bin/docs-refresh.sh"
    cron_file: docs-refresh
  become: true
```

- [ ] **Step 4: Deploy and run it by hand once**

```bash
./scripts/deploy.sh --tags crons
sudo -u ubuntu /usr/local/bin/docs-refresh.sh
journalctl -t docs-refresh --since "5 min ago"
```

Expected: one `no change` line, because Tasks 4 through 6 already committed current output.

If it logs a change and commits instead, `write_if_body_changed` is not suppressing a write it should. That means some field inside a page **body** moves between runs — a timestamp rendered below the frontmatter rather than in it, or unsorted iteration over a dict or set. Find it and fix the generator.

Do not work around it by widening what the helper ignores. The helper compares bodies precisely so that a real content change is never missed; loosening it to swallow a moving field would hide real changes too.

- [ ] **Step 5: Prove the lock holds**

In one shell, hold the lock: `flock /var/lock/server-git-tree.lock sleep 60`.
In another, run `/usr/local/bin/docs-refresh.sh`.

Expected: the second blocks rather than proceeding, then runs once the first exits. A run that proceeds immediately means the `exec 9>` redirect or the `flock` fd number is wrong, and the whole safety argument for this cron is void.

- [ ] **Step 6: Commit**

Stage the template and `crons.yml`. Commit with:

```
Schedule the generated-docs refresh on daniel-box

Regenerates docs/reference/, rebuilds the site into the hostPath the docs
pod serves, and commits the change -- under /var/lock/server-git-tree.lock,
which is what keeps it from racing the 30-minute gitops timer or a deploy.

A dirty tree rebuilds the site but regenerates nothing: committing on top
of someone's in-progress edit would mix their work into a docs commit.

No deadman, matching the infra-map cron. Every page carries the timestamp
and SHA it was built from, so a stopped cron is visible where it matters
rather than needing its own alert channel.
```

---

### Task 8: The hook that rejects hand-edits to generated pages

The banner asks; the hook enforces. Without it the first person to fix a typo in `docs/reference/services.md` loses that fix at the next cron run, with nothing to tell them why.

**Files:**
- Create: `.claude/hooks/block-generated-docs-edits.sh`
- Modify: `.claude/settings.json` or the chezmoi source that generates it — **run `chezmoi source-path ~/.claude/settings.json` first**; the deployed file is generated from `home/.chezmoitemplates/settings.base.json` and a hand edit is reverted on the next `chezmoi apply`.
- Test: `.claude/hooks/test_block_generated_docs_edits.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a PreToolUse hook that exits non-zero for an `Edit`/`Write` whose path is under `docs/reference/` or `docs/assets/generated/`.

- [ ] **Step 1: Read the hook this copies**

Read `.claude/hooks/` and find `block-protected-edits`. It already denies edits under `containers/` and to SOPS files. Match its input parsing, its exit convention, and its message style exactly — a second hook with a different shape is a second thing to learn.

- [ ] **Step 2: Write the failing test**

Follow the test style already in `.claude/hooks/`. Assert four cases:

```python
def test_denies_edit_under_docs_reference():
    assert _hook_denies("docs/reference/services.md")


def test_denies_edit_under_docs_assets_generated():
    assert _hook_denies("docs/assets/generated/infra-map.svg")


def test_allows_edit_to_a_hand_written_doc():
    """docs/reference/ is generated; docs/ generally is not.

    A hook that denied all of docs/ would block every runbook edit, which is how
    a guard gets switched off.
    """
    assert not _hook_denies("docs/secret-rotation.md")


def test_allows_edit_to_the_generator_itself():
    """The message tells people to edit the generator. That path must not be blocked."""
    assert not _hook_denies("scripts/service_catalog.py")


def test_denies_an_absolute_path_into_the_generated_tree():
    """Path matching must not be defeated by an absolute path or a worktree prefix."""
    assert _hook_denies("/home/ubuntu/server/docs/reference/services.md")
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest .claude/hooks/test_block_generated_docs_edits.py -v`
Expected: FAIL — the hook script does not exist.

- [ ] **Step 4: Write the hook**

Match `block-protected-edits`'s structure. The denial message must name the generator and the source, because a message that only says "denied" sends the reader looking for the rule instead of the fix:

```
docs/reference/services.md is generated by scripts/service_catalog.py and is
overwritten by the docs-refresh cron. To change what appears here, change the
generator or the inventory it reads.
```

Match against the path suffix, not a prefix — the session may be in a worktree under `.claude/worktrees/<name>/`, so an absolute path check anchored at the repo root fails there. That is what `test_denies_an_absolute_path_into_the_generated_tree` covers.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest .claude/hooks/ -v`
Expected: PASS, including the pre-existing hook tests.

- [ ] **Step 6: Register the hook and verify it fires**

Add the `PreToolUse` entry matching `Edit|Write` to the chezmoi settings template, then `chezmoi apply ~/.claude/settings.json`.

Then try to edit `docs/reference/services.md` in a session. Expected: denied, with the message from Step 4. **A hook that is written but never registered passes its tests and protects nothing** — this step is the one that matters.

- [ ] **Step 7: Commit**

Stage the hook, its test, and note the chezmoi change in the message (the settings source lives in the other repo, so it is committed separately there).

```
Deny hand-edits to the generated docs tree

The banner on each page asks; this enforces. Without it the first person to
fix a typo in docs/reference/services.md loses the fix at the next cron run
with nothing to tell them why.

Scoped to docs/reference/ and docs/assets/generated/, never docs/ as a
whole -- a guard that blocked every runbook edit is a guard that gets
switched off. Matched by path suffix so it still fires inside a worktree.
```

---

### Task 9: The remaining reference pages

Four more generated pages. Each is the same shape as Task 4: a builder that parses the tree, a Markdown renderer, tests, an entry in `GENERATORS`, and a nav line.

**Do these one at a time, committing between them.** They are independent, and a single commit adding four generators is four times harder to review and to revert.

**Files, per page:**
- Create: `scripts/gen_reference_<topic>.py`
- Test: `scripts/test_gen_reference_<topic>.py`
- Create: `docs/reference/<topic>.md` (generated, committed)
- Modify: `scripts/build_docs.py` (append to `GENERATORS`)
- Modify: `mkdocs.yml` (append to the `Reference` nav section)

**Interfaces:**
- Consumes: `generated_banner()` from Task 3.
- Produces: nothing another task reads.

- [ ] **Step 1: `hosts.md` — the three hosts and what each is for**

Source: `ansible/inventory/hosts.ini` and `ansible/inventory/host_vars/*.yml`.

Per host: name, role (k3s server / k3s agent / Docker), `expose_mode`, its service count, and the hardware facts the inventory carries. Note in the page that `hosts.ini` pins both cluster nodes to `connection=local`, since that is the trap behind one-shot plays silently running on the wrong box.

- [ ] **Step 2: `secrets.md` — the rotation registry**

Source: `ansible/secret_rotation.yml`, which is the plaintext registry — names, dates, and tiers, with no values.

**Read only that file.** Never `ansible/vars/secrets.yml`, and never anything that shells out to `sops`. A generator that decrypts secrets writes plaintext into a committed, browsable page.

Per secret: name, tier, last rotated, next due. Flag the `pinned` DANGER entries explicitly and link to `docs/secret-rotation.md` for their procedures.

Add this assertion to the test file, because the failure it prevents is unrecoverable:

```python
def test_generator_never_reads_the_encrypted_secrets_file():
    """A generated page is committed and browsable. It must not be able to contain a secret."""
    source = Path("scripts/gen_reference_secrets.py").read_text()
    assert "vars/secrets.yml" not in source
    assert "sops" not in source.lower()
```

- [ ] **Step 3: `crons.md` — every scheduled job**

Source: `ansible/roles/setup/initial_setup/tasks/crons.yml`, plus any `ansible.builtin.cron` task in another role.

Per cron: name, schedule, host, the script it runs, and whether it changes state. The state-changing ones are the reason this page is worth having — a reader needs to know which jobs can commit, deploy, or delete without a human.

Parse with `yaml.safe_load` over the tasks file and pick out `ansible.builtin.cron` tasks. Where a `when:` restricts the host, carry that into the host column rather than dropping it.

- [ ] **Step 4: `networking.md` — routes and auth**

Source: the per-role `ingressroute.yml.j2` macro calls, and `containers_list`.

Per route: hostname label, whether it is LAN-only or also public, and which middleware chain fronts it. The domain suffix is SOPS-sourced with no static default, so print the hostname **label** and say so — do not construct an FQDN you cannot verify. `scripts/service_catalog.py`'s FIELD NOTES section already documents this constraint; follow it.

- [ ] **Step 5: Verify the whole set**

Run: `uv run pytest scripts/ -v`
Run: `uv run python scripts/build_docs.py --site-dir /home/ubuntu/docs-site`
Run: `uv run mkdocs build --strict`

Then open each of the six reference pages in a browser and read them. A generated page that is syntactically valid and factually wrong passes every check above.

- [ ] **Step 6: Retire the hand-maintained counts**

Now that `docs/reference/services.md` carries the service count, update the repo-root `CLAUDE.md` where it says to grep for it, pointing at the generated page instead. Do the same for the `k8s_dry_run_unsupported` count if `networking.md` or `services.md` now carries it.

This is the payoff for the whole plan — it is also the step most easily skipped, because nothing fails if you do.

- [ ] **Step 7: Final verification before opening the PR**

Run: `uv run prek run --all-files`
Run: `uv run pytest`
Run: `uv run python scripts/probe.py health docs`

Then open the PR. After merge, follow the repo's *After a PR Merges* procedure: record the pre-merge SHA, wait for master CI on the merge commit specifically, `./scripts/gitops_tick.sh`, then deploy from the primary checkout — not from this worktree, which is behind master after a squash merge.
