# CI pipeline + Renovate auto-test-deploy + archive cleanup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add server-side CI, tidy the `archive/` convention, and build an end-to-end flow that smoke-tests Renovate image bumps, auto-merges minor/patch, and pull-deploys them on the host with a health gate and automatic local rollback.

**Architecture:** Three GitHub-hosted workflows (lint/test, image smoke) gate `master`; Renovate auto-merges minor/patch on green. A host-side systemd timer (`gitops_deploy`) polls `origin/master` every 30 min, deploys changed services via the existing `ansible-playbook` path, health-gates the result, and on failure resets locally + redeploys the prior version + writes a hold marker + alerts a dedicated Discord webhook.

**Tech Stack:** GitHub Actions, `prek`, Renovate, Ansible, systemd timers, Python 3.12 (stdlib only, pytest), Docker, SOPS/age.

Spec: `docs/superpowers/specs/2026-06-07-renovate-cicd-pipeline-design.md`

---

## File structure

**Phase 1 — CI**
- Create: `.github/workflows/ci.yml` — runs `prek run --all-files` on PR + push.

**Phase 2 — Archive cleanup**
- Modify: `ansible/roles/containers/archive/<svc>/CLAUDE.md` (backfill `Intended:` lines).
- Move: `ansible/roles/containers/minecraft/` → `ansible/roles/containers/archive/minecraft/` (+ CLAUDE.md).
- Modify: `ansible/inventory/host_vars/daniel-server.yml` (delete 16 commented blocks).

**Phase 3 — Renovate smoke + auto-merge**
- Create: `scripts/smoke_extract.py` — pure: parse a unified diff → changed image refs.
- Create: `scripts/test_smoke_extract.py` — pytest.
- Create: `.github/workflows/image-smoke.yml` — pull + run + probe each changed image.
- Modify: `renovate.json` — `automerge` on the minor/patch rule.

**Phase 4 — GitOps deployer**
- Create: `ansible/roles/setup/gitops_deploy/files/deploy_logic.py` — pure: path→service map, next-action decision.
- Create: `ansible/roles/setup/gitops_deploy/files/test_deploy_logic.py` — pytest.
- Create: `ansible/roles/setup/gitops_deploy/files/gitops_deploy.py` — orchestrator (git/docker/ansible via subprocess).
- Create: `ansible/roles/setup/gitops_deploy/templates/gitops-deploy.service.j2`
- Create: `ansible/roles/setup/gitops_deploy/templates/gitops-deploy.timer.j2`
- Create: `ansible/roles/setup/gitops_deploy/templates/gitops-deploy-alert.service.j2` (OnFailure)
- Create: `ansible/roles/setup/gitops_deploy/templates/config.env.j2` (secrets onto host, 0600)
- Create: `ansible/roles/setup/gitops_deploy/tasks/main.yml`
- Create: `ansible/roles/setup/gitops_deploy/CLAUDE.md`
- Modify: `ansible/initial_setup.yml` (register role, daniel-server only)
- Modify: `ansible/roles/containers/monitor-bridge/templates/docker-compose.yml.j2` (add `gitops-deploy` push monitor label)
- Modify: `prek.toml` (add the two new test dirs to the pytest hook `args`/`files`)
- Modify: `ansible/vars/secrets.yml` (add `gitops_deploy_discord_webhook`, `gitops_deploy_kuma_push_token` — via `sops`, manual)

---

## Conventions for every task

- The repo enforces `prek`. Before any commit, the pre-commit hooks run automatically; a task is "done" only when its commit succeeds (hooks green).
- Python is **stdlib-only** (matches `monitor-bridge`). Tests are `pytest`, colocated as `test_*.py`.
- Commit messages end with the trailer:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- Work directly on `master` (no feature branches — repo convention).

---

## Phase 1 — CI workflow

### Task 1: Add the prek CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Create the workflow**

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [master]

permissions:
  contents: read

jobs:
  prek:
    name: prek (lint + validate + tests + secrets)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # gitleaks needs full history

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install prek
        run: pip install prek

      - name: Run all hooks
        run: prek run --all-files --color always
```

- [ ] **Step 2: Validate locally that the same command is green**

Run: `prek run --all-files`
Expected: all hooks `Passed` or `Skipped` (no `Failed`). This is the exact command CI runs.

- [ ] **Step 3: Lint the workflow YAML**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))" && echo OK`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run prek (lint + validate + tests + gitleaks) on PR and master

Single source of truth with the local pre-commit config; no host access,
static validation only.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 5: After push, enable branch protection (manual, document only)**

In GitHub → Settings → Branches → add a rule for `master` requiring the **CI / prek**
check (and later **image-smoke**, Task 7). Auto-merge (Phase 3) depends on this. Note this
in the PR description; it cannot be done from the repo.

---

## Phase 2 — Archive cleanup

### Task 2: Backfill archive details, archive minecraft, drop commented blocks

**Files:**
- Modify: each `ansible/roles/containers/archive/<svc>/CLAUDE.md` missing an `Intended:` line
- Move: `ansible/roles/containers/minecraft/` → `ansible/roles/containers/archive/minecraft/`
- Create: `ansible/roles/containers/archive/minecraft/CLAUDE.md`
- Modify: `ansible/inventory/host_vars/daniel-server.yml`

- [ ] **Step 1: Inventory which archived services lack an `Intended:` line**

Run: `grep -L "Intended:" ansible/roles/containers/archive/*/CLAUDE.md`
Expected: a list (possibly empty) of CLAUDE.md files to backfill.

- [ ] **Step 2: For each file from Step 1, add an `Intended:` line**

Read the matching commented block in `ansible/inventory/host_vars/daniel-server.yml` for that
service and add a line to its archive CLAUDE.md in the existing house style, e.g. for `duplicati`
(commented block shows `port: 8200`, `apps`, `use_authelia: true`):

```markdown
- **Intended:** port 8200 · apps net · Authelia: yes
```

Match the exact `port`/`networks`/`use_authelia` from the commented block. (Services with no
commented block — e.g. ones never in `daniel-server.yml` — keep whatever they have.)

- [ ] **Step 3: Move the orphaned minecraft role into archive/**

Run:
```bash
git mv ansible/roles/containers/minecraft ansible/roles/containers/archive/minecraft
```
Expected: the role tree now lives under `archive/`.

- [ ] **Step 4: Add minecraft's archive CLAUDE.md**

Create `ansible/roles/containers/archive/minecraft/CLAUDE.md`:

```markdown
# minecraft — Minecraft server (ARCHIVED)

**Not deployed.** Parked in `archive/`; see `../CLAUDE.md` for how to reactivate.

- **Image:** `itzg/minecraft-server:latest`
- **Intended:** no web port · apps net · Authelia: no
- **Notable:** Was a top-level role commented out in `host_vars`, not a `containers_list`
  entry with port/Authelia (it's a TCP game server, fronted outside Traefik). Reactivating
  needs the game port published and EULA/server settings in the compose env.
```

(Adjust the "Notable" line to match the role's actual compose if it differs.)

- [ ] **Step 5: Delete the 16 commented service blocks from host_vars**

Edit `ansible/inventory/host_vars/daniel-server.yml`: remove every commented-out
`# - name: ...` block (duplicati, home-assistant, jellyseerr, readarr, calibre, calibre-web,
netdata, beszel, file-browser, change-detection, wallabag, minecraft, terraria, valheim,
foundry, calmerge). Keep all **active** (uncommented) entries and the section header comments
(`# Organization: ...`) untouched.

- [ ] **Step 6: Verify nothing active references the moved role or relies on the comments**

Run:
```bash
grep -rn "containers/minecraft/" ansible --include=*.yml | grep -v archive/ ; \
grep -cE "^\s*#\s*- name:" ansible/inventory/host_vars/daniel-server.yml
```
Expected: first grep prints nothing (no active reference to the old path); the count is `0`
(all commented blocks gone).

- [ ] **Step 7: Confirm the inventory still parses and toposort resolves**

Run: `python -m pytest ansible/tests -q`
Expected: PASS (toposort tests read the inventory shape; no regression).
Run: `python scripts/validate_compose_templates.py`
Expected: exits 0 (no template rendering broke).

- [ ] **Step 8: Commit**

```bash
git add ansible/roles/containers/archive ansible/inventory/host_vars/daniel-server.yml
git commit -m "containers: consolidate parked services into archive/, drop dead host_vars comments

Backfill intended inventory settings into archive CLAUDE.md, move orphaned
minecraft role to archive/, remove 16 inert commented containers_list blocks.
No behavioural change.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase 3 — Renovate smoke test + auto-merge

### Task 3: Pure image-extraction logic (TDD)

**Files:**
- Create: `scripts/smoke_extract.py`
- Test: `scripts/test_smoke_extract.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_smoke_extract.py
from smoke_extract import extract_changed_images


def test_extracts_added_image_line():
    diff = (
        "diff --git a/ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2 "
        "b/ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2\n"
        "--- a/...\n+++ b/...\n"
        "@@ -1 +1 @@\n"
        "-    image: ghcr.io/google/cadvisor:v0.53.0\n"
        "+    image: ghcr.io/google/cadvisor:v0.54.0\n"
    )
    assert extract_changed_images(diff) == ["ghcr.io/google/cadvisor:v0.54.0"]


def test_ignores_removed_and_context_lines():
    diff = (
        "-    image: foo:1\n"
        "     image: bar:2\n"  # context line (leading space), not added
        "+    image: foo:2\n"
    )
    assert extract_changed_images(diff) == ["foo:2"]


def test_strips_quotes():
    diff = '+    image: "louislam/uptime-kuma:2"\n'
    assert extract_changed_images(diff) == ["louislam/uptime-kuma:2"]


def test_ignores_non_image_additions():
    diff = "+    container_name: cadvisor\n+    restart: unless-stopped\n"
    assert extract_changed_images(diff) == []


def test_dedupes():
    diff = "+    image: foo:2\n+    image: foo:2\n"
    assert extract_changed_images(diff) == ["foo:2"]
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `cd scripts && python -m pytest test_smoke_extract.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'smoke_extract'`.

- [ ] **Step 3: Implement**

```python
# scripts/smoke_extract.py
"""Extract newly-added container image references from a unified git diff.

Used by the image-smoke workflow: a Renovate bump changes the literal
`image: name:tag` line in a docker-compose.yml.j2; we pull+run just the new ref.
"""
import re
import sys

# Added line (starts with a single '+', not '+++'), an `image:` key, capture the ref.
_IMAGE_RE = re.compile(r'^\+(?!\+\+)\s*image:\s*["\']?([^\s"\']+)["\']?\s*$')


def extract_changed_images(diff_text: str) -> list[str]:
    seen: list[str] = []
    for line in diff_text.splitlines():
        m = _IMAGE_RE.match(line)
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


if __name__ == "__main__":
    images = extract_changed_images(sys.stdin.read())
    for img in images:
        print(img)
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `cd scripts && python -m pytest test_smoke_extract.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke_extract.py scripts/test_smoke_extract.py
git commit -m "scripts: extract changed image refs from a diff (for image smoke test)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task 4: Image smoke-test workflow

**Files:**
- Create: `.github/workflows/image-smoke.yml`

- [ ] **Step 1: Create the workflow**

```yaml
name: image-smoke

on:
  pull_request:
    paths:
      - "ansible/roles/containers/**/templates/docker-compose.yml.j2"

permissions:
  contents: read

jobs:
  smoke:
    name: pull + boot changed images
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Collect changed image refs
        id: images
        run: |
          base="origin/${{ github.base_ref }}"
          git fetch origin "${{ github.base_ref }}" --depth=1
          diff="$(git diff "$base"...HEAD -- 'ansible/roles/containers/**/templates/docker-compose.yml.j2')"
          images="$(printf '%s' "$diff" | python scripts/smoke_extract.py)"
          echo "list<<EOF" >> "$GITHUB_OUTPUT"
          echo "$images" >> "$GITHUB_OUTPUT"
          echo "EOF" >> "$GITHUB_OUTPUT"
          echo "Changed images:"; echo "$images"

      - name: Pull + boot each image
        if: steps.images.outputs.list != ''
        run: |
          set -euo pipefail
          fail=0
          while IFS= read -r img; do
            [ -z "$img" ] && continue
            echo "::group::smoke $img"
            docker pull "$img"
            cid="$(docker run -d --rm "$img" || true)"
            if [ -z "$cid" ]; then echo "FAIL: $img did not start"; fail=1; echo "::endgroup::"; continue; fi
            # If the image declares a HEALTHCHECK, wait up to 60s for healthy.
            has_hc="$(docker inspect -f '{{if .Config.Healthcheck}}yes{{end}}' "$cid")"
            ok=1
            if [ "$has_hc" = "yes" ]; then
              for _ in $(seq 1 30); do
                st="$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo gone)"
                [ "$st" = "healthy" ] && { ok=0; break; }
                [ "$st" = "gone" ] && break
                sleep 2
              done
            else
              # No healthcheck: assert it stays up 30s without crash-looping.
              sleep 30
              [ "$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null || echo false)" = "true" ] && ok=0
            fi
            [ "$ok" -ne 0 ] && { echo "FAIL: $img unhealthy / exited"; docker logs "$cid" 2>&1 | tail -n 30; fail=1; }
            docker rm -f "$cid" >/dev/null 2>&1 || true
            echo "::endgroup::"
          done <<< "${{ steps.images.outputs.list }}"
          exit "$fail"
```

- [ ] **Step 2: Lint the workflow YAML**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/image-smoke.yml'))" && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/image-smoke.yml
git commit -m "ci: smoke-test changed container images on PR (pull + boot probe)

Shallow boot check — pulls the new image ref(s) from the PR diff, runs each,
and waits for the image HEALTHCHECK (or 30s survival). Filters outright-broken
bumps before auto-merge; deeper validation is the host health gate.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task 5: Renovate auto-merge for minor/patch

**Files:**
- Modify: `renovate.json` (the minor/patch packageRule)

- [ ] **Step 1: Add automerge to the existing non-major rule**

In `renovate.json`, change the existing minor/patch packageRule to:

```json
    {
      "description": "Bundle minor + patch image bumps into a single weekly PR to cut noise; majors stay separate so they get a deliberate look. Auto-merge once CI (prek + image-smoke) is green; the host health gate is the post-merge safety net.",
      "matchManagers": ["custom.regex"],
      "matchUpdateTypes": ["minor", "patch"],
      "groupName": "container images (non-major)",
      "automerge": true,
      "platformAutomerge": true
    }
```

Leave the `latest` rule and the custom manager unchanged. Do **not** add `automerge` anywhere
that matches `major`.

- [ ] **Step 2: Validate JSON**

Run: `python -c "import json; json.load(open('renovate.json'))" && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add renovate.json
git commit -m "renovate: auto-merge minor/patch image bumps on green CI (majors stay manual)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase 4 — Host-side GitOps deployer

### Task 6: Deployer pure logic (TDD)

**Files:**
- Create: `ansible/roles/setup/gitops_deploy/files/deploy_logic.py`
- Test: `ansible/roles/setup/gitops_deploy/files/test_deploy_logic.py`

- [ ] **Step 1: Write the failing tests**

```python
# ansible/roles/setup/gitops_deploy/files/test_deploy_logic.py
from deploy_logic import services_from_changed_paths, next_action


def test_single_service_template():
    paths = ["ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2"]
    cs = services_from_changed_paths(paths)
    assert cs.services == {"cadvisor"}
    assert cs.broad is False


def test_multiple_services():
    paths = [
        "ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2",
        "ansible/roles/containers/couchdb/templates/docker-compose.yml.j2",
    ]
    cs = services_from_changed_paths(paths)
    assert cs.services == {"cadvisor", "couchdb"}
    assert cs.broad is False


def test_archived_service_is_ignored():
    paths = ["ansible/roles/containers/archive/duplicati/templates/docker-compose.yml.j2"]
    cs = services_from_changed_paths(paths)
    assert cs.services == set()
    assert cs.broad is False


def test_shared_template_is_broad():
    paths = ["ansible/templates/resources.yml.j2"]
    cs = services_from_changed_paths(paths)
    assert cs.broad is True


def test_host_vars_is_broad():
    paths = ["ansible/inventory/host_vars/daniel-server.yml"]
    cs = services_from_changed_paths(paths)
    assert cs.broad is True


def test_unrelated_path_ignored():
    paths = ["docs/superpowers/specs/x.md", "README.md"]
    cs = services_from_changed_paths(paths)
    assert cs.services == set()
    assert cs.broad is False


def test_next_action_noop_when_in_sync():
    assert next_action("aaa", "aaa", None) == "noop"


def test_next_action_skip_when_origin_is_hold():
    assert next_action("aaa", "bad", "bad") == "skip_hold"


def test_next_action_deploy_when_origin_ahead():
    assert next_action("aaa", "bbb", None) == "deploy"


def test_next_action_deploy_when_hold_is_stale():
    # origin advanced past the held bad SHA (operator reverted) -> deploy again
    assert next_action("aaa", "ccc", "bad") == "deploy"
```

- [ ] **Step 2: Run to confirm failure**

Run: `cd ansible/roles/setup/gitops_deploy/files && python -m pytest test_deploy_logic.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'deploy_logic'`.

- [ ] **Step 3: Implement**

```python
# ansible/roles/setup/gitops_deploy/files/deploy_logic.py
"""Pure decision logic for the GitOps deployer (no I/O — unit-tested).

`services_from_changed_paths` maps a git-diff file list to the set of active
container services to redeploy, or flags a "broad" change (shared template /
inventory) that the deployer must defer to a manual full deploy.

`next_action` decides what a poll tick should do given the local/origin HEADs
and any recorded known-bad (hold) SHA.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Active container template: roles/containers/<svc>/templates/docker-compose.yml.j2
# (the negative lookahead excludes archive/<svc>/...).
_ACTIVE_TPL = re.compile(
    r"^ansible/roles/containers/(?!archive/)([^/]+)/templates/docker-compose\.yml\.j2$"
)
# Changes whose blast radius we don't try to scope automatically.
_BROAD_PREFIXES = (
    "ansible/templates/",                 # shared macros (traefik/networks/resources/...)
    "ansible/inventory/",                 # host_vars / group_vars
    "ansible/roles/containers/common/",   # shared deploy path
    "ansible/deploy.yml",
    "ansible/filter_plugins/",            # toposort
)


@dataclass
class ChangeSet:
    services: set[str] = field(default_factory=set)
    broad: bool = False


def services_from_changed_paths(paths: list[str]) -> ChangeSet:
    cs = ChangeSet()
    for p in paths:
        if any(p.startswith(prefix) for prefix in _BROAD_PREFIXES):
            cs.broad = True
            continue
        m = _ACTIVE_TPL.match(p)
        if m:
            cs.services.add(m.group(1))
    return cs


def next_action(local_head: str, origin_head: str, hold_sha: str | None) -> str:
    if origin_head == local_head:
        return "noop"
    if hold_sha is not None and origin_head == hold_sha:
        return "skip_hold"
    return "deploy"
```

- [ ] **Step 4: Run to confirm pass**

Run: `cd ansible/roles/setup/gitops_deploy/files && python -m pytest test_deploy_logic.py -q`
Expected: PASS (10 passed).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/setup/gitops_deploy/files/deploy_logic.py \
        ansible/roles/setup/gitops_deploy/files/test_deploy_logic.py
git commit -m "gitops_deploy: pure path->service + next-action logic with tests

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task 7: Deployer orchestrator script

**Files:**
- Create: `ansible/roles/setup/gitops_deploy/files/gitops_deploy.py`

- [ ] **Step 1: Implement the orchestrator**

```python
#!/usr/bin/env python3
"""GitOps deployer — runs once per systemd-timer tick on daniel-server.

Flow: fetch origin/master; if it advanced, map changed templates to services;
ff-merge; deploy each via the existing ansible-playbook path; health-gate each
container. On failure: reset to the previous HEAD, redeploy the prior version,
record the bad SHA as a hold marker, and alert the dedicated Discord webhook.

Config comes from /etc/gitops-deploy/config.env (KEY=VALUE), written by Ansible:
  REPO_DIR, BRANCH, DISCORD_WEBHOOK, KUMA_PUSH_TOKEN, KUMA_PUSH_URL_BASE,
  HEALTH_TIMEOUT_S, MONITORING_NETWORK
Stdlib only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy_logic import services_from_changed_paths, next_action  # noqa: E402

HOLD_FILE = "/var/lib/gitops-deploy/hold_sha"


def cfg() -> dict[str, str]:
    out: dict[str, str] = {}
    with open("/etc/gitops-deploy/config.env") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    return out


C = cfg()
REPO = C["REPO_DIR"]
BRANCH = C.get("BRANCH", "master")
TIMEOUT = int(C.get("HEALTH_TIMEOUT_S", "300"))
NET = C.get("MONITORING_NETWORK", "monitoring")


def run(args: list[str], cwd: str | None = REPO, check: bool = True) -> str:
    r = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} -> {r.returncode}\n{r.stderr}")
    return r.stdout.strip()


def log(msg: str) -> None:
    print(msg, flush=True)


def read_hold() -> str | None:
    try:
        with open(HOLD_FILE) as fh:
            return fh.read().strip() or None
    except FileNotFoundError:
        return None


def write_hold(sha: str | None) -> None:
    os.makedirs(os.path.dirname(HOLD_FILE), exist_ok=True)
    if sha is None:
        try:
            os.remove(HOLD_FILE)
        except FileNotFoundError:
            pass
    else:
        with open(HOLD_FILE, "w") as fh:
            fh.write(sha)


def discord(content: str) -> None:
    url = C.get("DISCORD_WEBHOOK", "")
    if not url:
        return
    data = json.dumps({"content": content[:1900]}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # alerting must never crash the deployer
        log(f"discord alert failed: {e}")


def kuma_push(status: str, msg: str) -> None:
    token = C.get("KUMA_PUSH_TOKEN", "")
    base = C.get("KUMA_PUSH_URL_BASE", "")  # e.g. http://uptime-kuma:3001/api/push
    if not token or not base:
        return
    url = f"{base}/{token}?status={status}&msg={urllib.parse.quote(msg)}"
    # Host can't resolve the container DNS name; push from inside the monitoring net.
    subprocess.run(
        ["docker", "run", "--rm", "--network", NET, "curlimages/curl:latest",
         "-sf", "-m", "10", url],
        capture_output=True, text=True,
    )


def health_ok(service: str) -> bool:
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        st = run(["docker", "inspect", "-f", "{{.State.Health.Status}}", service],
                 cwd=None, check=False)
        if st == "healthy":
            return True
        if st == "":  # no healthcheck -> fall back to "running"
            run_st = run(["docker", "inspect", "-f", "{{.State.Running}}", service],
                         cwd=None, check=False)
            if run_st == "true":
                return True
        time.sleep(10)
    return False


def deploy(services: set[str]) -> None:
    tags = ",".join(sorted(services))
    run(["ansible-playbook", "ansible/deploy.yml", "--tags", tags])


def main() -> int:
    # Refuse to touch a dirty working tree (operator may be mid-edit).
    if run(["git", "status", "--porcelain"]):
        discord("⚠️ gitops-deploy: working tree dirty on daniel-server — skipping. "
                "Resolve manually.")
        return 0

    run(["git", "fetch", "origin", BRANCH])
    local = run(["git", "rev-parse", "HEAD"])
    origin = run(["git", "rev-parse", f"origin/{BRANCH}"])
    hold = read_hold()

    action = next_action(local, origin, hold)
    if action == "noop":
        kuma_push("up", "in sync")
        return 0
    if action == "skip_hold":
        log(f"origin at known-bad {origin[:8]}; holding")
        kuma_push("up", "holding known-bad commit")
        return 0

    paths = run(["git", "diff", "--name-only", f"{local}..{origin}"]).splitlines()
    cs = services_from_changed_paths(paths)

    if cs.broad:
        discord(f"⚠️ gitops-deploy: shared template / inventory changed in "
                f"`{origin[:8]}` — deferring to a manual full deploy "
                f"(`ansible-playbook ansible/deploy.yml`).")
        kuma_push("up", "broad change deferred")
        return 0
    if not cs.services:
        run(["git", "merge", "--ff-only", f"origin/{BRANCH}"])  # docs-only etc.
        kuma_push("up", "no service change")
        return 0

    run(["git", "merge", "--ff-only", f"origin/{BRANCH}"])
    deploy(cs.services)

    failed = [s for s in sorted(cs.services) if not health_ok(s)]
    if not failed:
        write_hold(None)
        kuma_push("up", f"deployed {','.join(sorted(cs.services))}")
        return 0

    # Rollback: reset to prior HEAD, redeploy the failed service(s) on old version.
    log(f"health gate failed for {failed}; rolling back to {local[:8]}")
    run(["git", "reset", "--hard", local])
    deploy(set(failed))
    write_hold(origin)
    kuma_push("down", f"rolled back {','.join(failed)}")
    discord(
        f"🚨 gitops-deploy: **rollback** on daniel-server.\n"
        f"Service(s) `{', '.join(failed)}` from commit `{origin[:8]}` failed the health "
        f"gate and were rolled back to `{local[:8]}`.\n"
        f"**Action:** revert the offending Renovate PR — the bad commit is held until you do."
    )
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        discord(f"🚨 gitops-deploy crashed: {e}")
        raise
```

- [ ] **Step 2: Syntax + import check**

Run: `cd ansible/roles/setup/gitops_deploy/files && python -c "import ast; ast.parse(open('gitops_deploy.py').read()); print('OK')"`
Expected: `OK`
Run: `cd ansible/roles/setup/gitops_deploy/files && python -m pytest test_deploy_logic.py -q`
Expected: PASS (still green — the orchestrator imports the tested module).

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/setup/gitops_deploy/files/gitops_deploy.py
git commit -m "gitops_deploy: orchestrator (fetch/deploy/health-gate/rollback/alert)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task 8: Ansible role (systemd units, script install, host config)

**Files:**
- Create: `ansible/roles/setup/gitops_deploy/templates/config.env.j2`
- Create: `ansible/roles/setup/gitops_deploy/templates/gitops-deploy.service.j2`
- Create: `ansible/roles/setup/gitops_deploy/templates/gitops-deploy.timer.j2`
- Create: `ansible/roles/setup/gitops_deploy/templates/gitops-deploy-alert.service.j2`
- Create: `ansible/roles/setup/gitops_deploy/tasks/main.yml`

- [ ] **Step 1: Host config (secrets land here, 0600, never in git)**

`ansible/roles/setup/gitops_deploy/templates/config.env.j2`:

```ini
REPO_DIR=/home/{{ sys_user }}/server
BRANCH=master
HEALTH_TIMEOUT_S=300
MONITORING_NETWORK=monitoring
DISCORD_WEBHOOK={{ gitops_deploy_discord_webhook }}
KUMA_PUSH_TOKEN={{ gitops_deploy_kuma_push_token }}
KUMA_PUSH_URL_BASE=http://uptime-kuma:3001/api/push
```

- [ ] **Step 2: systemd service (oneshot, runs as the deploy user)**

`ansible/roles/setup/gitops_deploy/templates/gitops-deploy.service.j2`:

```ini
[Unit]
Description=GitOps deployer (pull origin/master, deploy changed services)
After=network-online.target docker.service
Wants=network-online.target
OnFailure=gitops-deploy-alert.service

[Service]
Type=oneshot
User={{ sys_user }}
WorkingDirectory=/home/{{ sys_user }}/server
ExecStart=/usr/bin/python3 /opt/gitops-deploy/gitops_deploy.py
# SOPS age key is already in the user's home; filter plugins auto-load next to
# ansible/deploy.yml, so no ANSIBLE_CONFIG is needed.
```

- [ ] **Step 3: systemd timer (every 30 min)**

`ansible/roles/setup/gitops_deploy/templates/gitops-deploy.timer.j2`:

```ini
[Unit]
Description=Run the GitOps deployer every 30 minutes

[Timer]
OnBootSec=10min
OnUnitActiveSec=30min
RandomizedDelaySec=2min
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: OnFailure alert unit (deployer crashed/non-zero exit)**

`ansible/roles/setup/gitops_deploy/templates/gitops-deploy-alert.service.j2`:

```ini
[Unit]
Description=Alert when gitops-deploy fails

[Service]
Type=oneshot
# Webhook comes from the 0600 config (not embedded — this unit is world-readable).
EnvironmentFile=/etc/gitops-deploy/config.env
ExecStart=/usr/bin/curl -fsS -m 10 -H "Content-Type: application/json" \
  -d '{"content":"🚨 gitops-deploy unit failed on daniel-server — check `journalctl -u gitops-deploy`."}' \
  "${DISCORD_WEBHOOK}"
```

> Note: this alert unit no longer contains the secret, so it is safe at the loop's `0644`.
> The OnFailure unit fires only on a non-zero/crash exit of `gitops-deploy.service`.

- [ ] **Step 5: Role tasks**

`ansible/roles/setup/gitops_deploy/tasks/main.yml`:

```yaml
---
- name: Create gitops-deploy directories
  ansible.builtin.file:
    path: "{{ item }}"
    state: directory
    owner: "{{ sys_user }}"
    group: "{{ sys_user }}"
    mode: "0750"
  loop:
    - /opt/gitops-deploy
    - /var/lib/gitops-deploy
  become: true

- name: Install deployer Python files
  ansible.builtin.copy:
    src: "{{ item }}"
    dest: "/opt/gitops-deploy/{{ item }}"
    owner: "{{ sys_user }}"
    group: "{{ sys_user }}"
    mode: "0755"
  loop:
    - gitops_deploy.py
    - deploy_logic.py
  become: true

- name: Write deployer config (secrets, host-only)
  ansible.builtin.template:
    src: config.env.j2
    dest: /etc/gitops-deploy/config.env
    owner: "{{ sys_user }}"
    group: "{{ sys_user }}"
    mode: "0600"
  become: true
  no_log: true

- name: Install systemd units
  ansible.builtin.template:
    src: "{{ item }}.j2"
    dest: "/etc/systemd/system/{{ item }}"
    mode: "0644"
  loop:
    - gitops-deploy.service
    - gitops-deploy.timer
    - gitops-deploy-alert.service
  become: true
  notify: Reload systemd

- name: Enable and start the timer
  ansible.builtin.systemd:
    name: gitops-deploy.timer
    enabled: true
    state: started
    daemon_reload: true
  become: true
```

- [ ] **Step 6: Add the handler to the role**

Create `ansible/roles/setup/gitops_deploy/handlers/main.yml`:

```yaml
---
- name: Reload systemd
  ansible.builtin.systemd:
    daemon_reload: true
  become: true
```

- [ ] **Step 7: Lint**

Run: `ansible-lint ansible/roles/setup/gitops_deploy -c .ansible-lint`
Expected: no errors. (Fix any reported issues, e.g. FQCN/name rules.)

- [ ] **Step 8: Commit**

```bash
git add ansible/roles/setup/gitops_deploy/templates ansible/roles/setup/gitops_deploy/tasks \
        ansible/roles/setup/gitops_deploy/handlers
git commit -m "gitops_deploy: ansible role (systemd timer + units + host config)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task 9: Secrets + Uptime-Kuma push monitor

**Files:**
- Modify: `ansible/vars/secrets.yml` (via `sops`, manual)
- Modify: `ansible/roles/containers/monitor-bridge/templates/docker-compose.yml.j2`

- [ ] **Step 1: Add the two secrets (manual, encrypted)**

Run: `sops ansible/vars/secrets.yml`
Add:
```yaml
gitops_deploy_discord_webhook: "<the dedicated webhook URL provided by the operator>"
gitops_deploy_kuma_push_token: "<a fresh random token, e.g. `openssl rand -hex 16`>"
```
Save; SOPS re-encrypts on write. **Never** paste these into any tracked plaintext file.

- [ ] **Step 2: Provision the push monitor via AutoKuma label on monitor-bridge**

In `ansible/roles/containers/monitor-bridge/templates/docker-compose.yml.j2`, add one more
push-monitor label alongside the existing `monitor-bridge-*` ones (the deployer pushes to it
from the host via the `monitoring` network):

```jinja
      {{ kuma('gitops-deploy', monitor_type='push', name='GitOps Deploy', interval=2400, push_token=gitops_deploy_kuma_push_token) }}
```

(`interval=2400` = 40 min, a grace margin over the 30-min tick so a single missed push
doesn't false-alarm. AutoKuma links it to the `discord` notifier automatically.)

- [ ] **Step 3: Validate the rendered template**

Run: `python scripts/validate_compose_templates.py`
Expected: exits 0 (the new label renders to valid YAML).

- [ ] **Step 4: Commit (template only — secrets.yml stays encrypted, gitleaks-clean)**

```bash
git add ansible/roles/containers/monitor-bridge/templates/docker-compose.yml.j2 ansible/vars/secrets.yml
git commit -m "monitor-bridge: add GitOps Deploy push monitor (host deployer liveness)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task 10: Wire the role in + extend test coverage + docs

**Files:**
- Modify: `ansible/initial_setup.yml`
- Modify: `prek.toml`
- Create: `ansible/roles/setup/gitops_deploy/CLAUDE.md`

- [ ] **Step 1: Register the role (daniel-server only)**

In `ansible/initial_setup.yml`, add to the `roles:` list (after `docker_install`):

```yaml
    - { role: gitops_deploy, tags: ["gitops_deploy"], when: inventory_hostname == 'daniel-server' }
```

- [ ] **Step 2: Add the new test dirs to the prek pytest hook**

In `prek.toml`, extend the pytest hook's `args` and `files` to include the new test files:

`args` — add the two paths:
```toml
args = ["ansible/tests", "ansible/roles/containers/monitor-bridge/files", ".claude/hooks", "scripts", "ansible/roles/setup/gitops_deploy/files"]
```
`files` — add the new patterns (extend the existing alternation):
```toml
files = "^(ansible/filter_plugins/.*\\.py|ansible/tests/.*\\.py|ansible/roles/containers/monitor-bridge/files/.*\\.py|\\.claude/hooks/.*\\.py|scripts/.*\\.py|ansible/roles/setup/gitops_deploy/files/.*\\.py)$"
```

- [ ] **Step 3: Run the full test suite the way prek/CI will**

Run:
```bash
python -m pytest ansible/tests ansible/roles/containers/monitor-bridge/files .claude/hooks scripts ansible/roles/setup/gitops_deploy/files -q
```
Expected: PASS (all suites, including the two new ones).

- [ ] **Step 4: Write the role's CLAUDE.md**

Create `ansible/roles/setup/gitops_deploy/CLAUDE.md`:

```markdown
# gitops_deploy — pull-based deploy on master change (daniel-server only)

Installs a systemd **timer** (every 30 min) that runs `/opt/gitops-deploy/gitops_deploy.py`
as `{{ sys_user }}`. The script fetches `origin/master`; if it advanced, maps changed
`roles/containers/<svc>/templates/docker-compose.yml.j2` files to service tags, `--ff-only`
merges, and deploys each via `ansible-playbook ansible/deploy.yml --tags <svc>`.

## Health gate + rollback
After deploy it polls each container's health (`max(5min)` default, see HEALTH_TIMEOUT_S).
On failure it `git reset --hard`es to the previous HEAD, redeploys the prior version,
writes the bad SHA to `/var/lib/gitops-deploy/hold_sha` (so the next tick won't redeploy it),
and alerts the dedicated Discord webhook. Reverting the offending PR advances `origin` past
the held SHA and the hold clears automatically.

## Safety
- Read-only against the repo (no push); rollback is local-only + self-guarding.
- Refuses to run on a dirty working tree (the host clone is deploy-managed).
- **Broad changes** (shared `ansible/templates/*`, `inventory/`, `common/`, `deploy.yml`)
  are NOT auto-scoped — the deployer alerts and defers to a manual full deploy.

## Config / secrets
`/etc/gitops-deploy/config.env` (0600) is templated from SOPS vars
`gitops_deploy_discord_webhook` and `gitops_deploy_kuma_push_token`. Liveness pings the
`gitops-deploy` Uptime-Kuma push monitor (provisioned via an AutoKuma label on
`monitor-bridge`) by launching a throwaway curl container on the `monitoring` network — the
host can't resolve container DNS directly.

## Logic tests
`files/test_deploy_logic.py` covers path→service mapping and the next-action decision.
Run via the repo pytest hook.
```

- [ ] **Step 5: Lint + full prek**

Run: `prek run --all-files`
Expected: all `Passed`/`Skipped`.

- [ ] **Step 6: Commit**

```bash
git add ansible/initial_setup.yml prek.toml ansible/roles/setup/gitops_deploy/CLAUDE.md
git commit -m "gitops_deploy: register role (daniel-server), add tests to CI, document

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 7: Deploy + verify on the host (manual)**

Run: `ansible-playbook ansible/initial_setup.yml --tags gitops_deploy --limit daniel-server`
Then:
```bash
systemctl status gitops-deploy.timer
sudo systemctl start gitops-deploy.service && journalctl -u gitops-deploy -n 50 --no-pager
```
Expected: timer active; a manual run logs "in sync" (or deploys a pending change) and exits 0.

- [ ] **Step 8: End-to-end smoke (manual)**

Trigger a real minor bump (or a test commit changing one pinned service's tag to a known-good
newer tag), wait a tick, and confirm: service redeploys, health gate passes, kuma push "up".
Then simulate failure (a tag known to boot unhealthy), confirm: local rollback, `hold_sha`
written, Discord alert fired; revert and confirm the hold clears on the next tick.

---

## Self-review

**Spec coverage:**
- Spec §A (CI) → Task 1. ✓
- Spec §B (archive cleanup + preservation + minecraft) → Task 2. ✓
- Spec §C1 (Renovate auto-merge minor/patch, majors manual) → Task 5. ✓
- Spec §C2 (pre-merge smoke) → Tasks 3–4. ✓
- Spec §D happy path (fetch/diff/map/ff/deploy/health-gate) → Tasks 6–7. ✓
- Spec §D failure path (reset + redeploy + hold + alert) → Task 7 (`main`), tested via Task 6 `next_action`. ✓
- Spec §D observability (kuma liveness + dedicated webhook) → Task 7 + Task 9 + Task 8 OnFailure. ✓
- Spec §D secret (`gitops_deploy_discord_webhook`, SOPS) → Task 9. ✓
- Spec "Open detail" broad-change conservative default → Task 6 (`broad`) + Task 7 (defer + alert). ✓

**Deviations from spec (flag to operator):**
1. Liveness uses a throwaway `curl` container on the `monitoring` network (host can't resolve
   `uptime-kuma:3001` directly) + systemd `OnFailure` → webhook for crashes. The spec said
   "ping an Uptime-Kuma push monitor"; this is the host-reachable realization of it.
2. The deployer reads the webhook from `/etc/gitops-deploy/config.env` (templated from SOPS)
   rather than decrypting SOPS at runtime — simpler, and keeps the age key out of the timer.

**Placeholder scan:** none — all code/config blocks are complete; manual steps (branch
protection, `sops` edit, host deploy) are inherently out-of-repo and marked as such.

**Type/name consistency:** `services_from_changed_paths`/`next_action`/`ChangeSet(.services,
.broad)` are defined in Task 6 and used identically in Task 7. Config keys in `config.env.j2`
(Task 8) match `C[...]` reads in `gitops_deploy.py` (Task 7). Secret var names match between
Task 8 templates and Task 9 `sops` entries.

## Out of scope
- The ~41 `:latest`/Watchtower images and non-semver pinned tags (unchanged).
- Migrating any service off `:latest` to digest pins.
- Multi-bad-commit hold stacking (single-SHA hold is sufficient; revert is the normal path).
```
