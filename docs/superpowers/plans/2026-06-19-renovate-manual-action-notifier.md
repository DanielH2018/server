# Renovate Manual-Action Notifier — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daily, notify-on-change Discord nudge for Renovate PRs that need a human (manual-merge queue + stuck PRs), plus a monitor-bridge Kuma monitor that catches the notifier itself dying.

**Architecture:** A new `renovate_notify` Ansible *setup* role (systemd timer + stdlib Python, cloning `gitops_deploy`) queries the public GitHub REST API, classifies open Renovate PRs, and posts to Discord only when the actionable set changes. An 18th `monitor-bridge` check reads the notifier's `last_run` dead-man's-switch file. Pure decision logic is split from I/O and unit-tested.

**Tech Stack:** Python 3 stdlib only (`urllib`), Ansible, systemd, Uptime Kuma push monitors, pytest, SOPS.

## Global Constraints

- **Stdlib only** — `notify_logic.py`, `renovate_notify.py`, and `check.py` import nothing outside the standard library (the notifier runs under the host `/usr/bin/python3`; `check.py` runs on `python:3.12-alpine`).
- **No new GitHub secret** — the repo `DanielH2018/server` is public; all reads are unauthenticated. Every GitHub request MUST send `User-Agent: renovate-notify` and `Accept: application/vnd.github+json` (GitHub 403s requests with no User-Agent).
- **Reuse `gitops_deploy_discord_webhook`** for Discord; no new webhook secret.
- **Discord content ≤ 1900 chars** (truncate, mirroring `gitops_deploy`).
- **Renovate PR detection:** `pr["user"]["login"] == "renovate[bot]"` OR `pr["head"]["ref"]` startswith `"renovate/"`.
- **Fail toward surfacing:** an unrecognized/absent Automerge marker classifies as `manual`, never silent.
- **Kuma push monitor:** `interval=600`, `max_retries=0` (role convention).
- **Push token:** `monitor_bridge_renovate_alive_push_token` MUST be exactly 32 alphanumeric chars (`openssl rand -hex 16`).
- **Deploy order:** deploy `renovate_notify` BEFORE `monitor-bridge` (the bind-mount source `/var/lib/renovate-notify` must exist owned by `{{ sys_user }}` first, else Docker auto-creates it root-owned and the non-root container can't read it).
- **No Jinja `{# #}` block comments added to the compose template** — use plain `#` YAML comments in `environment:`; add the kuma label line with no new Jinja comment (avoids the macro-indent corruption hazard).
- All Ansible tasks idempotent; `ansible-lint` clean.

---

### Task 1: `notify_logic.py` — PR model, parsers, classification

**Files:**
- Create: `ansible/roles/setup/renovate_notify/files/notify_logic.py`
- Test: `ansible/roles/setup/renovate_notify/files/test_notify_logic.py`
- Modify: `pyproject.toml` (add the new files dir to `testpaths`)

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class PR: number:int, title:str, url:str, automerge:bool, ci:str, conflicting:bool` (`ci` ∈ `"success"|"pending"|"failure"`)
  - `parse_automerge(body: str) -> bool`
  - `ci_rollup(check_runs: list[dict], statuses: list[dict]) -> str`
  - `classify_pr(pr: PR) -> str` (`"stuck"|"manual"|"on-track"`)
  - `actionable(prs: list[PR]) -> list[tuple[PR, str]]`

- [ ] **Step 1: Write the failing tests**

```python
# ansible/roles/setup/renovate_notify/files/test_notify_logic.py
import notify_logic as nl


def _pr(number=1, title="t", url="u", automerge=True, ci="success", conflicting=False):
    return nl.PR(number=number, title=title, url=url, automerge=automerge,
                 ci=ci, conflicting=conflicting)


# --- parse_automerge ---
def test_parse_automerge_enabled():
    assert nl.parse_automerge("🚦 **Automerge**: Enabled.") is True


def test_parse_automerge_disabled():
    assert nl.parse_automerge("🚦 **Automerge**: Disabled.") is False


def test_parse_automerge_absent_defaults_false():
    assert nl.parse_automerge("no marker here") is False
    assert nl.parse_automerge("") is False


# --- ci_rollup ---
def test_ci_rollup_all_success():
    runs = [{"status": "completed", "conclusion": "success"}]
    statuses = [{"state": "success"}]
    assert nl.ci_rollup(runs, statuses) == "success"


def test_ci_rollup_failed_checkrun():
    runs = [{"status": "completed", "conclusion": "failure"}]
    assert nl.ci_rollup(runs, []) == "failure"


def test_ci_rollup_failed_legacy_status():
    # a failing commit-status (e.g. GitGuardian) with all check-runs green
    runs = [{"status": "completed", "conclusion": "success"}]
    statuses = [{"state": "failure"}]
    assert nl.ci_rollup(runs, statuses) == "failure"


def test_ci_rollup_pending_when_incomplete():
    runs = [{"status": "in_progress", "conclusion": None}]
    assert nl.ci_rollup(runs, []) == "pending"


def test_ci_rollup_pending_status_is_pending():
    # renovate/stability-days still soaking
    assert nl.ci_rollup([], [{"state": "pending"}]) == "pending"


def test_ci_rollup_failure_beats_pending():
    runs = [{"status": "in_progress", "conclusion": None},
            {"status": "completed", "conclusion": "failure"}]
    assert nl.ci_rollup(runs, []) == "failure"


def test_ci_rollup_neutral_and_skipped_are_ok():
    runs = [{"status": "completed", "conclusion": "neutral"},
            {"status": "completed", "conclusion": "skipped"}]
    assert nl.ci_rollup(runs, []) == "success"


# --- classify_pr ---
def test_classify_manual_when_automerge_disabled():
    assert nl.classify_pr(_pr(automerge=False, ci="success")) == "manual"


def test_classify_manual_even_if_failing():
    assert nl.classify_pr(_pr(automerge=False, ci="failure")) == "manual"


def test_classify_stuck_automerge_but_failing():
    assert nl.classify_pr(_pr(automerge=True, ci="failure")) == "stuck"


def test_classify_stuck_automerge_but_conflicting():
    assert nl.classify_pr(_pr(automerge=True, ci="success", conflicting=True)) == "stuck"


def test_classify_on_track_automerge_healthy():
    assert nl.classify_pr(_pr(automerge=True, ci="success")) == "on-track"


def test_classify_on_track_automerge_pending():
    assert nl.classify_pr(_pr(automerge=True, ci="pending")) == "on-track"


# --- actionable ---
def test_actionable_keeps_stuck_and_manual_drops_ontrack():
    prs = [
        _pr(number=8, automerge=True, ci="failure"),         # stuck
        _pr(number=9, automerge=False, ci="success"),        # manual
        _pr(number=12, automerge=True, ci="success"),        # on-track -> dropped
    ]
    out = nl.actionable(prs)
    assert [(pr.number, b) for pr, b in out] == [(8, "stuck"), (9, "manual")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest ansible/roles/setup/renovate_notify/files/test_notify_logic.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'notify_logic'`.

- [ ] **Step 3: Write `notify_logic.py` (this half)**

```python
# ansible/roles/setup/renovate_notify/files/notify_logic.py
"""Pure decision logic for the Renovate manual-action notifier (no I/O — unit-tested).

Maps open Renovate PRs to an actionable bucket and decides when to (re)notify, so
the I/O shell (renovate_notify.py) only fetches, persists, and posts.
"""
from __future__ import annotations

from dataclasses import dataclass

# check-run conclusions that mean "this will not merge" (besides success/neutral/skipped).
_FAIL_CONCLUSIONS = {
    "failure", "cancelled", "timed_out", "action_required", "stale", "startup_failure",
}


@dataclass(frozen=True)
class PR:
    number: int
    title: str
    url: str
    automerge: bool          # Renovate body says Automerge Enabled
    ci: str                  # "success" | "pending" | "failure"
    conflicting: bool


def parse_automerge(body: str) -> bool:
    """True only if Renovate's body explicitly says Automerge Enabled. Absent/unknown
    -> False, so classify_pr() surfaces it as `manual` (fail toward surfacing)."""
    return "Automerge**: Enabled" in (body or "")


def ci_rollup(check_runs: list[dict], statuses: list[dict]) -> str:
    """Fold the two disjoint GitHub CI sources — Checks API (check_runs) and the legacy
    Commit Status API (statuses) — into one verdict. Failure precedes pending precedes
    success: a failure in EITHER source counts."""
    failure = pending = False
    for c in check_runs:
        if c.get("status") != "completed":
            pending = True
        elif c.get("conclusion") in _FAIL_CONCLUSIONS:
            failure = True
    for s in statuses:
        st = s.get("state")
        if st in ("failure", "error"):
            failure = True
        elif st == "pending":
            pending = True
    if failure:
        return "failure"
    if pending:
        return "pending"
    return "success"


def classify_pr(pr: PR) -> str:
    if not pr.automerge:
        return "manual"
    if pr.ci == "failure" or pr.conflicting:
        return "stuck"
    return "on-track"


def actionable(prs: list[PR]) -> list[tuple[PR, str]]:
    """(pr, bucket) for every PR that needs a human — stuck or manual; on-track dropped."""
    out = []
    for pr in prs:
        bucket = classify_pr(pr)
        if bucket in ("stuck", "manual"):
            out.append((pr, bucket))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest ansible/roles/setup/renovate_notify/files/test_notify_logic.py -q`
Expected: PASS (17 tests).

- [ ] **Step 5: Register the suite in `pyproject.toml`**

In `pyproject.toml` `[tool.pytest.ini_options] testpaths`, add the line after the `gitops_deploy/files` entry (around line 26):

```toml
  "ansible/roles/setup/gitops_deploy/files",   # deploy_logic decision tests
  "ansible/roles/setup/renovate_notify/files", # notifier decision tests
```

- [ ] **Step 6: Commit**

```bash
git add ansible/roles/setup/renovate_notify/files/notify_logic.py \
        ansible/roles/setup/renovate_notify/files/test_notify_logic.py \
        pyproject.toml
git commit -m "renovate-notify: pure PR classification + CI rollup (TDD)"
```

---

### Task 2: `notify_logic.py` — fingerprint, notify decision, message rendering

**Files:**
- Modify: `ansible/roles/setup/renovate_notify/files/notify_logic.py`
- Modify: `ansible/roles/setup/renovate_notify/files/test_notify_logic.py`

**Interfaces:**
- Consumes: `PR`, `actionable` (Task 1)
- Produces:
  - `fingerprint(items: list[tuple[PR, str]]) -> str`
  - `should_notify(prev_fp: str, cur_fp: str) -> tuple[bool, str]` (kind ∈ `"digest"|"cleared"|"none"`)
  - `CLEARED_MSG: str`
  - `render_digest(items: list[tuple[PR, str]], limit: int = 1900) -> str`

- [ ] **Step 1: Write the failing tests** (append to `test_notify_logic.py`)

```python
# --- fingerprint ---
def test_fingerprint_is_sorted_and_stable():
    a = [(_pr(number=9), "manual"), (_pr(number=8), "stuck")]
    b = [(_pr(number=8), "stuck"), (_pr(number=9), "manual")]
    assert nl.fingerprint(a) == nl.fingerprint(b) == "#8:stuck,#9:manual"


def test_fingerprint_empty_is_blank():
    assert nl.fingerprint([]) == ""


# --- should_notify ---
def test_should_notify_unchanged_is_silent():
    assert nl.should_notify("#8:stuck", "#8:stuck") == (False, "none")


def test_should_notify_new_backlog_is_digest():
    assert nl.should_notify("", "#8:stuck") == (True, "digest")


def test_should_notify_changed_backlog_is_digest():
    assert nl.should_notify("#8:stuck", "#8:stuck,#9:manual") == (True, "digest")


def test_should_notify_cleared_when_now_empty():
    assert nl.should_notify("#8:stuck", "") == (True, "cleared")


def test_should_notify_empty_to_empty_is_silent():
    assert nl.should_notify("", "") == (False, "none")


# --- render_digest ---
def test_render_digest_groups_and_links():
    items = [
        (_pr(number=8, title="container images", url="http://x/8",
             automerge=True, ci="failure"), "stuck"),
        (_pr(number=9, title="community.sops", url="http://x/9",
             automerge=False, ci="success"), "manual"),
    ]
    msg = nl.render_digest(items)
    assert "2 PR(s) need attention" in msg
    assert "#8 container images" in msg
    assert "http://x/8" in msg
    assert "Awaiting your merge" in msg
    assert "#9 community.sops" in msg


def test_render_digest_truncates_and_counts_overflow():
    items = [(_pr(number=i, title="x" * 80, url="http://x/%d" % i,
                  automerge=False, ci="success"), "manual") for i in range(60)]
    msg = nl.render_digest(items, limit=600)
    assert len(msg) <= 600
    assert "more" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest ansible/roles/setup/renovate_notify/files/test_notify_logic.py -q -k "fingerprint or should_notify or render"`
Expected: FAIL — `AttributeError: module 'notify_logic' has no attribute 'fingerprint'`.

- [ ] **Step 3: Append to `notify_logic.py`**

```python
CLEARED_MSG = "✅ Renovate backlog cleared — nothing needs your attention."

_BUCKET_ORDER = ("stuck", "manual")
_BUCKET_HEADER = {
    "stuck": "🔧 Stuck (should auto-merge, can't):",
    "manual": "✋ Awaiting your merge (merging → auto-deploys, health-gated, ≤30 min):",
}


def fingerprint(items: list[tuple[PR, str]]) -> str:
    return ",".join(sorted("#%d:%s" % (pr.number, bucket) for pr, bucket in items))


def should_notify(prev_fp: str, cur_fp: str) -> tuple[bool, str]:
    if cur_fp == prev_fp:
        return False, "none"
    if cur_fp == "":
        return True, "cleared"
    return True, "digest"


def _pr_note(pr: PR) -> str:
    if pr.conflicting:
        return "⚠️ conflicting"
    if pr.ci == "failure":
        return "❌ CI failing"
    if pr.ci == "pending":
        return "⏳ CI pending"
    return "✅ green"


def render_digest(items: list[tuple[PR, str]], limit: int = 1900) -> str:
    total = len(items)
    head = "📦 Renovate — %d PR(s) need attention" % total
    # Build per-PR entries in bucket order; add as many as fit, count the remainder.
    entries: list[tuple[str, list[str]]] = []  # (bucket_header, [lines]) groups
    for bucket in _BUCKET_ORDER:
        group = [(pr) for pr, b in items if b == bucket]
        if not group:
            continue
        lines = []
        for pr in group:
            lines.append(" • #%d %s — %s" % (pr.number, pr.title, _pr_note(pr)))
            lines.append("   %s" % pr.url)
        entries.append((_BUCKET_HEADER[bucket], lines))

    out = [head, ""]
    shown = 0
    truncated = False
    for header, lines in entries:
        block = [header] + lines + [""]
        # +len for a possible "…and N more" tail keeps us safely under the limit.
        if len("\n".join(out + block)) > limit - 20:
            truncated = True
            break
        out += block
        shown += len(lines) // 2
    msg = "\n".join(out).rstrip()
    if truncated and shown < total:
        msg += "\n…and %d more" % (total - shown)
    return msg
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest ansible/roles/setup/renovate_notify/files/test_notify_logic.py -q`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/setup/renovate_notify/files/notify_logic.py \
        ansible/roles/setup/renovate_notify/files/test_notify_logic.py
git commit -m "renovate-notify: fingerprint, notify-on-change, digest rendering (TDD)"
```

---

### Task 3: `renovate_notify.py` — I/O shell

**Files:**
- Create: `ansible/roles/setup/renovate_notify/files/renovate_notify.py`

**Interfaces:**
- Consumes: all of `notify_logic` (Tasks 1–2)
- Produces: an executable script `main() -> int`; `--dry-run` prints the rendered message + fingerprint and posts/persists nothing.

- [ ] **Step 1: Write the script** (no unit test — thin I/O; verified by `--dry-run` smoke in Task 9)

```python
#!/usr/bin/env python3
"""Renovate manual-action notifier — runs once per daily systemd-timer tick.

Queries the public GitHub REST API (unauthenticated) for open Renovate PRs, classifies
each (notify_logic), and posts a Discord digest ONLY when the actionable set changes.
Writes a last_run timestamp for the monitor-bridge "Renovate Notifier — Alive" monitor.

Config from /etc/renovate-notify/config.env (KEY=VALUE): REPO, DISCORD_WEBHOOK, STATE_DIR.
Stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notify_logic import (  # noqa: E402
    PR, actionable, ci_rollup, fingerprint, parse_automerge,
    render_digest, should_notify, CLEARED_MSG,
)

CONFIG = "/etc/renovate-notify/config.env"
API = "https://api.github.com"
HEADERS = {"User-Agent": "renovate-notify", "Accept": "application/vnd.github+json"}


def cfg() -> dict[str, str]:
    out: dict[str, str] = {}
    with open(CONFIG) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    return out


def log(msg: str) -> None:
    print(msg, flush=True)


def get(url: str):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def is_renovate(pr: dict) -> bool:
    return ((pr.get("user") or {}).get("login") == "renovate[bot]"
            or (pr.get("head") or {}).get("ref", "").startswith("renovate/"))


def build_pr(repo: str, pr: dict) -> PR:
    n = pr["number"]
    detail = get("%s/repos/%s/pulls/%d" % (API, repo, n))
    # mergeable_state "dirty" = conflicting; mergeable False likewise. null = unknown -> not conflicting.
    conflicting = detail.get("mergeable_state") == "dirty" or detail.get("mergeable") is False
    sha = pr["head"]["sha"]
    runs = get("%s/repos/%s/commits/%s/check-runs" % (API, repo, sha)).get("check_runs", [])
    statuses = get("%s/repos/%s/commits/%s/status" % (API, repo, sha)).get("statuses", [])
    return PR(
        number=n,
        title=pr.get("title", "").strip(),
        url=pr.get("html_url", ""),
        automerge=parse_automerge(pr.get("body") or ""),
        ci=ci_rollup(runs, statuses),
        conflicting=conflicting,
    )


def discord(webhook: str, content: str) -> None:
    if not webhook:
        log("no DISCORD_WEBHOOK set; skipping post")
        return
    data = json.dumps({"content": content[:1900]}).encode()
    req = urllib.request.Request(
        webhook, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # alerting must never crash the notifier
        log("discord post failed: %s" % e)


def read_state(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return ""


def write_state(path: str, fp: str) -> None:
    with open(path, "w") as fh:
        fh.write(fp)


def main() -> int:
    dry = "--dry-run" in sys.argv
    c = cfg()
    repo = c["REPO"]
    state_dir = c.get("STATE_DIR", "/var/lib/renovate-notify")
    state_file = os.path.join(state_dir, "last_notified")

    pulls = get("%s/repos/%s/pulls?state=open&per_page=100" % (API, repo))
    prs = [build_pr(repo, p) for p in pulls if is_renovate(p)]
    items = actionable(prs)
    cur_fp = fingerprint(items)
    prev_fp = read_state(state_file)
    notify, kind = should_notify(prev_fp, cur_fp)
    log("actionable=%d fp=%r prev=%r -> %s" % (len(items), cur_fp, prev_fp, kind))

    if notify:
        content = CLEARED_MSG if kind == "cleared" else render_digest(items)
        if dry:
            log("--- DRY RUN, would post ---\n%s" % content)
        else:
            discord(c.get("DISCORD_WEBHOOK", ""), content)
            write_state(state_file, cur_fp)

    if not dry:
        # Liveness marker for monitor-bridge — only on a clean completion (a fetch
        # exception propagates and skips this, so a broken notifier goes stale).
        with open(os.path.join(state_dir, "last_run"), "w") as fh:
            fh.write(str(time.time()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Byte-compile sanity check**

Run: `python3 -m py_compile ansible/roles/setup/renovate_notify/files/renovate_notify.py`
Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/setup/renovate_notify/files/renovate_notify.py
git commit -m "renovate-notify: GitHub/Discord I/O shell with --dry-run"
```

---

### Task 4: `renovate_notify` Ansible role + wiring

**Files:**
- Create: `ansible/roles/setup/renovate_notify/tasks/main.yml`
- Create: `ansible/roles/setup/renovate_notify/handlers/main.yml`
- Create: `ansible/roles/setup/renovate_notify/templates/config.env.j2`
- Create: `ansible/roles/setup/renovate_notify/templates/renovate-notify.service.j2`
- Create: `ansible/roles/setup/renovate_notify/templates/renovate-notify.timer.j2`
- Modify: `ansible/initial_setup.yml` (add role after `gitops_deploy`)

- [ ] **Step 1: `tasks/main.yml`**

```yaml
---
- name: Create renovate-notify directories
  ansible.builtin.file:
    path: "{{ item }}"
    state: directory
    owner: "{{ sys_user }}"
    group: "{{ sys_user }}"
    mode: "0750"
  loop:
    - /opt/renovate-notify       # notifier scripts
    - /var/lib/renovate-notify   # state (last_notified, last_run) + monitor-bridge bind source
    - /etc/renovate-notify       # config.env (0600 secret)
  become: true

- name: Install notifier Python files
  ansible.builtin.copy:
    src: "{{ item }}"
    dest: "/opt/renovate-notify/{{ item }}"
    owner: "{{ sys_user }}"
    group: "{{ sys_user }}"
    mode: "0755"
  loop:
    - renovate_notify.py
    - notify_logic.py
  become: true
  notify: Run renovate-notify once

- name: Write notifier config (secret webhook, host-only)
  ansible.builtin.template:
    src: config.env.j2
    dest: /etc/renovate-notify/config.env
    owner: "{{ sys_user }}"
    group: "{{ sys_user }}"
    mode: "0600"
  become: true
  no_log: true
  notify: Run renovate-notify once

- name: Install systemd units
  ansible.builtin.template:
    src: "{{ item }}.j2"
    dest: "/etc/systemd/system/{{ item }}"
    mode: "0644"
  loop:
    - renovate-notify.service
    - renovate-notify.timer
  become: true
  notify:
    - Reload systemd
    - Run renovate-notify once

- name: Enable and start the timer
  ansible.builtin.systemd:
    name: renovate-notify.timer
    enabled: true
    state: started
    daemon_reload: true
  become: true
```

- [ ] **Step 2: `handlers/main.yml`**

```yaml
---
- name: Reload systemd
  ansible.builtin.systemd:
    daemon_reload: true
  become: true

# Kick one run on first install / whenever the script, config, or units change, so
# activation is fully IaC and posts the current backlog once. Steady-state is timer-driven.
- name: Run renovate-notify once
  ansible.builtin.systemd:
    name: renovate-notify.service
    state: started
  become: true
```

- [ ] **Step 3: Templates**

`templates/config.env.j2`:
```jinja
REPO=DanielH2018/server
DISCORD_WEBHOOK={{ gitops_deploy_discord_webhook }}
STATE_DIR=/var/lib/renovate-notify
```

`templates/renovate-notify.service.j2`:
```jinja
[Unit]
Description=Renovate manual-action notifier (query open PRs, alert on change)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User={{ sys_user }}
ExecStart=/usr/bin/python3 /opt/renovate-notify/renovate_notify.py
```

`templates/renovate-notify.timer.j2`:
```jinja
[Unit]
Description=Run the Renovate manual-action notifier daily

[Timer]
OnCalendar=*-*-* 13:00:00
RandomizedDelaySec=5min
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Wire into `initial_setup.yml`**

Add after the `gitops_deploy` role line (currently line 40):

```yaml
    - { role: gitops_deploy, tags: ["gitops_deploy"], when: inventory_hostname == 'daniel-server' }
    - { role: renovate_notify, tags: ["renovate_notify"], when: inventory_hostname == 'daniel-server' }
```

- [ ] **Step 5: Lint**

Run: `uv run ansible-lint ansible/roles/setup/renovate_notify/`
Expected: no errors (warnings about `become` are acceptable if pre-existing role pattern matches `gitops_deploy`).

- [ ] **Step 6: Commit**

```bash
git add ansible/roles/setup/renovate_notify/tasks ansible/roles/setup/renovate_notify/handlers \
        ansible/roles/setup/renovate_notify/templates ansible/initial_setup.yml
git commit -m "renovate-notify: Ansible setup role (timer + units + config) on daniel-server"
```

---

### Task 5: monitor-bridge — `renovate_alive` check (pure + reader, TDD)

**Files:**
- Modify: `ansible/roles/containers/monitor-bridge/files/check.py`
- Modify: `ansible/roles/containers/monitor-bridge/files/test_check.py`

**Interfaces:**
- Produces: `renovate_alive(age_s, max_age_s) -> (bool, str)`; `check_renovate_alive() -> (bool, str)`; new `CHECKS` entry `("renovate_alive", KUMA_PUSH_RENOVATE_ALIVE, check_renovate_alive)`.

- [ ] **Step 1: Write the failing tests** (append to `test_check.py`, after the gitops tests)

```python
# --- renovate_alive / check_renovate_alive ---------------------------------

def test_renovate_alive_fresh():
    ok, msg = check.renovate_alive(60, 129600)  # 36h = 129600s
    assert ok
    assert "1m ago" in msg


def test_renovate_alive_at_threshold_is_ok():
    ok, _ = check.renovate_alive(129600, 129600)
    assert ok


def test_renovate_alive_stale():
    ok, msg = check.renovate_alive(140000, 129600)
    assert not ok
    assert "ago" in msg


def test_check_renovate_alive_missing_marker_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "RENOVATE_STATE_DIR", str(tmp_path))
    ok, msg = check.check_renovate_alive()
    assert not ok
    assert "no last_run marker" in msg


def test_check_renovate_alive_fresh_file_is_up(tmp_path, monkeypatch):
    import time as _t
    monkeypatch.setattr(check, "RENOVATE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(check, "RENOVATE_MAX_AGE_S", 129600)
    (tmp_path / "last_run").write_text(str(_t.time()))
    ok, _ = check.check_renovate_alive()
    assert ok
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest ansible/roles/containers/monitor-bridge/files/test_check.py -q -k renovate`
Expected: FAIL — `AttributeError: module 'check' has no attribute 'renovate_alive'`.

- [ ] **Step 3: Add env + functions to `check.py`**

After the GitOps env lines (currently lines 54–55), add:

```python
RENOVATE_STATE_DIR = _env("RENOVATE_STATE_DIR", "/renovate-state")
RENOVATE_MAX_AGE_S = float(_env("RENOVATE_MAX_AGE_MIN", "2160")) * 60
```

After `check_gitops_status()` (currently line 476), add:

```python
def renovate_alive(age_s, max_age_s):
    """Pure: is the notifier's last completed run recent enough? Returns (ok, msg)."""
    if age_s <= max_age_s:
        return True, "notifier ran %.0fm ago" % (age_s / 60)
    return False, "notifier last ran %.0fm ago (> %.0fm)" % (age_s / 60, max_age_s / 60)


def check_renovate_alive():
    try:
        with open(os.path.join(RENOVATE_STATE_DIR, "last_run")) as fh:
            ts = float(fh.read().strip())
    except FileNotFoundError:
        return False, "no last_run marker (notifier never completed a run?)"
    except ValueError:
        return False, "last_run marker unparseable"
    return renovate_alive(time.time() - ts, RENOVATE_MAX_AGE_S)
```

In the `CHECKS` list, add after the `ha_heartbeat` entry (currently line 665):

```python
    ("renovate_alive", _env("KUMA_PUSH_RENOVATE_ALIVE", ""), check_renovate_alive),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest ansible/roles/containers/monitor-bridge/files/test_check.py -q`
Expected: PASS (existing + 5 new).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/monitor-bridge/files/check.py \
        ansible/roles/containers/monitor-bridge/files/test_check.py
git commit -m "monitor-bridge: Renovate Notifier — Alive check (last_run dead-man's switch)"
```

---

### Task 6: monitor-bridge — compose wiring + CLAUDE.md

**Files:**
- Modify: `ansible/roles/containers/monitor-bridge/templates/docker-compose.yml.j2`
- Modify: `ansible/roles/containers/monitor-bridge/CLAUDE.md`

- [ ] **Step 1: Add env vars** (in `environment:`, after the HA block — currently after line 109):

```yaml
      # Renovate manual-action notifier liveness: renovate_notify.py writes last_run on the
      # host (daily timer); bind-mounted read-only at /renovate-state. 36h = one missed run + slack.
      - RENOVATE_STATE_DIR=/renovate-state
      - RENOVATE_MAX_AGE_MIN=2160
      - KUMA_PUSH_RENOVATE_ALIVE={{ monitor_bridge_renovate_alive_push_token }}
```

- [ ] **Step 2: Add the bind-mount** (in `volumes:`, after line 112):

```yaml
      - /var/lib/renovate-notify:/renovate-state:ro
```

- [ ] **Step 3: Add the Kuma label** (in `labels:`, after the `ha` heartbeat kuma line near the end of the block — no new Jinja `{# #}` comment):

```jinja
      {{ kuma('renovate-notify-alive', monitor_type='push', name='Renovate Notifier — Alive', interval=600, max_retries=0, push_token=monitor_bridge_renovate_alive_push_token) }}
```

- [ ] **Step 4: Update `CLAUDE.md`**

- Change "runs **seventeen checks**" → "runs **eighteen checks**".
- Add a bullet after the **Home Assistant Automations** check:

```markdown
  - **Renovate Notifier — Alive** (reads `/renovate-state/last_run`, a bind-mounted host
    timestamp the `renovate_notify` daily timer rewrites each clean run; `down` once it's
    older than `RENOVATE_MAX_AGE_MIN` (2160 = 36 h, one missed daily run + slack) — i.e. the
    notifier stalled / host down. Same state-file dead-man's-switch pattern as the GitOps
    monitors. Spec: `docs/superpowers/specs/2026-06-19-renovate-manual-action-notifier-design.md`.)
```

- In the push-token roster line, add `renovate_alive` to the `monitor_bridge_{…}_push_token` brace list.
- In the "Operator prerequisites" count, change "seventeen push tokens" → "eighteen push tokens".
- Add a note near the GitOps bind-mount paragraph: deploy `renovate_notify` before `monitor-bridge` (same bind-mount-ownership reason).

- [ ] **Step 5: Validate the rendered template**

Run: `uv run python scripts/validate_compose_templates.py`
Expected: PASS (no malformed YAML; the `validate-compose` PostToolUse hook also re-renders on edit).

- [ ] **Step 6: Commit**

```bash
git add ansible/roles/containers/monitor-bridge/templates/docker-compose.yml.j2 \
        ansible/roles/containers/monitor-bridge/CLAUDE.md
git commit -m "monitor-bridge: wire Renovate Notifier — Alive Kuma push monitor (18th check)"
```

---

### Task 7: push-token secret + rotation registry

**Files:**
- Modify: `ansible/vars/secrets.yml` (via `sops` / `/add-secret`)
- Modify: `ansible/secret_rotation.yml`

- [ ] **Step 1: Add the push token to SOPS**

Generate a 32-alphanumeric token and add it (use the `/add-secret` skill, or directly):

```bash
openssl rand -hex 16   # 32 hex chars — copy the output
sops ansible/vars/secrets.yml   # add:  monitor_bridge_renovate_alive_push_token: <token>
```

- [ ] **Step 2: Add the rotation-registry entry**

In `ansible/secret_rotation.yml`, add alongside the other `monitor_bridge_*_push_token` entries:

```yaml
  monitor_bridge_renovate_alive_push_token:
    last_rotated: '2026-06-19'
    tier: auto
```

- [ ] **Step 3: Sync + verify the registry**

Run: `uv run python scripts/secret_rotation.py sync && uv run python scripts/secret_rotation.py audit`
Expected: registry and `secrets.yml` reconciled; audit reports no missing/orphaned entries.

- [ ] **Step 4: Commit**

```bash
git add ansible/vars/secrets.yml ansible/secret_rotation.yml
git commit -m "secrets: add monitor_bridge_renovate_alive_push_token (Renovate Notifier monitor)"
```

---

### Task 8: full test + lint gate

- [ ] **Step 1: Run the whole suite + hooks**

Run: `uv run pytest && prek run --all-files`
Expected: all tests pass (incl. the new `notify_logic` and `renovate_alive` suites); lint, template validation, and secret scanning green.

- [ ] **Step 2: Commit** (only if a hook auto-fixed formatting)

```bash
git add -A && git commit -m "renovate-notify: formatting/lint fixes" || echo "nothing to fix"
```

---

### Task 9: deploy + end-to-end verification (daniel-server)

> **Order matters** (Global Constraints): `renovate_notify` first, then `monitor-bridge`.

- [ ] **Step 1: Deploy the notifier role**

Run: `uv run ansible-playbook ansible/initial_setup.yml --tags renovate_notify`
Expected: role applies; `/var/lib/renovate-notify` exists owned by `ubuntu`; timer enabled+started. The "Run renovate-notify once" handler fires — it will post the current real backlog (#8/#9/#10/#11) to the gitops-deploy Discord channel once.

- [ ] **Step 2: Dry-run smoke (no post, no state write)**

Run: `sudo -u ubuntu /usr/bin/python3 /opt/renovate-notify/renovate_notify.py --dry-run`
Expected: prints `actionable=N fp=… -> digest` and a `--- DRY RUN, would post ---` block listing the stuck/manual PRs with URLs.

- [ ] **Step 3: Confirm liveness + state files**

Run: `ls -l /var/lib/renovate-notify/ && systemctl status renovate-notify.timer --no-pager`
Expected: `last_run` (and `last_notified` after the real run) present; timer `active (waiting)`, next trigger shown.

- [ ] **Step 4: Deploy monitor-bridge**

Run: `uv run ansible-playbook ansible/deploy.yml --tags monitor-bridge`
Expected: container recreated healthy.

- [ ] **Step 5: Verify the new check + monitor**

Run: `docker exec monitor-bridge python /app/check.py --once | grep renovate_alive`
Expected: `OK   renovate_alive - notifier ran Nm ago`.
Run: `uv run python scripts/probe.py health monitor-bridge`
Expected: exit 0 (running + healthy). Confirm the **Renovate Notifier — Alive** monitor appears green in Uptime Kuma.

- [ ] **Step 6: Final confirmation**

No commit (deploy only). Report: notifier timer live, dry-run output, monitor-bridge healthy with the 18th monitor green, and that the first real run posted the current backlog to Discord.

---

## Self-Review

**Spec coverage:**
- Two buckets (stuck/manual), on-track ignored → Task 1 (`classify_pr`/`actionable`).
- Automerge from body, fail-toward-surfacing → Task 1 (`parse_automerge`) + Global Constraints.
- CI rollup across check-runs + statuses → Task 1 (`ci_rollup`, tested both sources).
- Notify-on-change + `✅ cleared` + empty edges → Task 2 (`should_notify`/`fingerprint`).
- Message shape + ≤1900 truncation + overflow count → Task 2 (`render_digest`).
- Stdlib I/O shell, public REST, reused webhook, `--dry-run` → Task 3.
- Setup role mirroring gitops_deploy + initial_setup wiring + testpaths → Tasks 1 (testpaths) & 4.
- Liveness monitor (18th check, bind-mount, env, token, `renovate_alive`) → Tasks 5–7.
- Tier-A merge (PR link + auto-deploy hint in message) → Task 2 (`_BUCKET_HEADER` manual line).
- Deploy ordering gotcha → Task 9 + Global Constraints.

**Placeholder scan:** none — every code/file step shows full content.

**Type consistency:** `PR` fields (`number/title/url/automerge/ci/conflicting`) are defined in Task 1 and used identically in Tasks 2–3; `actionable` returns `list[tuple[PR, str]]` consumed unchanged by `fingerprint`/`render_digest`; `renovate_alive`/`check_renovate_alive`/`RENOVATE_STATE_DIR`/`RENOVATE_MAX_AGE_S` names match between Task 5 code, tests, and the Task 6 compose env (`RENOVATE_STATE_DIR`/`RENOVATE_MAX_AGE_MIN`/`KUMA_PUSH_RENOVATE_ALIVE`).
