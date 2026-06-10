# n8n Prod-Workflow Failure Monitor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 10th `monitor-bridge` check that polls the n8n REST API for failed executions of *active* ("Prod") workflows over a rolling window and pushes `up|down` to a dedicated Uptime Kuma push monitor.

**Architecture:** A new `check_n8n()` in the existing stdlib-only `check.py` calls the n8n public API on the internal Docker network (`http://n8n:5678/api/v1/...`, `X-N8N-API-KEY` header — bypassing Traefik/Authelia). Pure filtering logic (`n8n_failures`) is factored out and unit-tested without HTTP, mirroring `backup_age_hours`. Wiring is one env block + one AutoKuma label in the compose template, plus adding the `apps` network to monitor-bridge in host_vars.

**Tech Stack:** Python 3.12 (stdlib `urllib`/`json`/`datetime`), pytest (`uv run pytest`), Ansible + Jinja2 compose templates, AutoKuma push monitors, SOPS/age secrets.

**Spec:** `docs/superpowers/specs/2026-06-08-n8n-prod-workflow-monitor-design.md`

**Conventions for every task:**
- All file paths are relative to repo root `/home/ubuntu/server`.
- Run tests with `uv run pytest ansible/roles/containers/monitor-bridge/files -v` (the repo pins its env via uv; bare `pytest` may lack deps).
- `import check` works because `test_check.py` lives in the same dir and `pyproject.toml` sets `pythonpath`.
- Commit messages end with the repo's `Co-Authored-By` trailer.
- Work stays on `master` (per project convention — no feature branches unless asked).

---

## Task 1: `parse_duration` helper

The `*_WINDOW` env vars elsewhere (`OOM_WINDOW`, `RESTART_WINDOW`) are Prometheus duration strings interpolated straight into PromQL, so Prometheus parses them. The n8n check evaluates its window in Python, so we need to parse `"15m"` → seconds ourselves.

**Files:**
- Modify: `ansible/roles/containers/monitor-bridge/files/check.py`
- Test: `ansible/roles/containers/monitor-bridge/files/test_check.py`

- [ ] **Step 1: Write the failing test**

Append to `test_check.py`:

```python
# --- parse_duration ---------------------------------------------------------

def test_parse_duration_units():
    assert check.parse_duration("900s") == 900
    assert check.parse_duration("15m") == 900
    assert check.parse_duration("1h") == 3600
    assert check.parse_duration("2d") == 172800
    assert check.parse_duration("300") == 300  # bare number = seconds
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest ansible/roles/containers/monitor-bridge/files/test_check.py::test_parse_duration_units -v`
Expected: FAIL with `AttributeError: module 'check' has no attribute 'parse_duration'`

- [ ] **Step 3: Write minimal implementation**

In `check.py`, add this function immediately after `parse_rfc3339` (before `backup_age_hours`, around line 104):

```python
def parse_duration(s):
    """Parse a Prometheus-style duration ('900s', '15m', '1h', '2d') to seconds (float).

    A bare number is treated as seconds. The n8n check evaluates its failure window in
    Python (unlike the *_WINDOW vars that are interpolated straight into PromQL, which
    Prometheus parses), so it needs this.
    """
    s = str(s).strip()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in units:
        return float(s[:-1]) * units[s[-1]]
    return float(s)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest ansible/roles/containers/monitor-bridge/files/test_check.py::test_parse_duration_units -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/monitor-bridge/files/check.py ansible/roles/containers/monitor-bridge/files/test_check.py
git commit -m "$(cat <<'EOF'
monitor-bridge: add parse_duration helper for the n8n check

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `n8n_failures` pure filter

The pure decision logic: given the n8n `workflows` and `executions` API payloads, return the active-workflow failures within the window. No HTTP — fully unit-tested.

**Files:**
- Modify: `ansible/roles/containers/monitor-bridge/files/check.py` (add `timedelta` import + `n8n_failures`)
- Test: `ansible/roles/containers/monitor-bridge/files/test_check.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_check.py` (the `from datetime import ...` line at the top must include `timedelta` — see Step 3's note; update it now to `from datetime import datetime, timezone, timedelta`):

```python
# --- n8n_failures -----------------------------------------------------------

N8N_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


def _n8n_ago(minutes):
    return (N8N_NOW - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _workflows(*items):
    """items: (id, name, active) tuples -> n8n /workflows payload."""
    return {"data": [{"id": i, "name": n, "active": a} for i, n, a in items]}


def _executions(*items):
    """items: (workflowId, stoppedAt) tuples -> n8n /executions payload (all status=error)."""
    return {"data": [{"workflowId": w, "status": "error", "stoppedAt": s} for w, s in items]}


def test_n8n_failure_within_window_named():
    wf = _workflows(("1", "Prod Flow", True))
    ex = _executions(("1", _n8n_ago(5)))
    assert check.n8n_failures(wf, ex, 900, now=N8N_NOW) == [("Prod Flow", 1)]


def test_n8n_failure_outside_window_ignored():
    wf = _workflows(("1", "Prod Flow", True))
    ex = _executions(("1", _n8n_ago(30)))  # 30m ago, window 15m
    assert check.n8n_failures(wf, ex, 900, now=N8N_NOW) == []


def test_n8n_inactive_workflow_ignored():
    wf = _workflows(("1", "Draft Flow", False))
    ex = _executions(("1", _n8n_ago(5)))
    assert check.n8n_failures(wf, ex, 900, now=N8N_NOW) == []


def test_n8n_multiple_failures_counted_and_sorted():
    wf = _workflows(("1", "A Flow", True), ("2", "B Flow", True))
    ex = _executions(
        ("1", _n8n_ago(2)),
        ("2", _n8n_ago(3)), ("2", _n8n_ago(4)), ("2", _n8n_ago(5)),
    )
    # B has 3 failures, A has 1 -> sorted by count desc
    assert check.n8n_failures(wf, ex, 900, now=N8N_NOW) == [("B Flow", 3), ("A Flow", 1)]


def test_n8n_empty_inputs():
    assert check.n8n_failures({"data": []}, {"data": []}, 900, now=N8N_NOW) == []


def test_n8n_missing_stoppedat_falls_back_to_startedat():
    wf = _workflows(("1", "Prod Flow", True))
    ex = {"data": [{"workflowId": "1", "status": "error", "startedAt": _n8n_ago(5)}]}
    assert check.n8n_failures(wf, ex, 900, now=N8N_NOW) == [("Prod Flow", 1)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest ansible/roles/containers/monitor-bridge/files/test_check.py -k n8n -v`
Expected: FAIL with `AttributeError: module 'check' has no attribute 'n8n_failures'`

- [ ] **Step 3: Write minimal implementation**

In `check.py`, update the datetime import (line 18):

```python
from datetime import datetime, timedelta, timezone
```

Then add `n8n_failures` in the checks section, immediately after `check_traefik_5xx` (before the `CHECKS = [` list, around line 280):

```python
def n8n_failures(workflows_json, executions_json, window_s, now=None):
    """Failed executions of *active* workflows within the last `window_s` seconds.

    Returns [(workflow_name, count), ...] sorted by count desc. An execution counts only
    if its workflowId belongs to an active ("Prod") workflow AND its stoppedAt (fallback
    startedAt) is within the window. Pure — fed the n8n /workflows and /executions
    payloads, so it's unit-tested without HTTP (like backup_age_hours).
    """
    now = now or datetime.now(timezone.utc)
    active = {
        w["id"]: w.get("name") or w["id"]
        for w in workflows_json.get("data", [])
        if w.get("active")
    }
    cutoff = now - timedelta(seconds=window_s)
    counts = {}
    for ex in executions_json.get("data", []):
        wid = ex.get("workflowId")
        if wid not in active:
            continue
        ts = ex.get("stoppedAt") or ex.get("startedAt")
        if not ts or parse_rfc3339(ts) < cutoff:
            continue
        counts[wid] = counts.get(wid, 0) + 1
    pairs = [(active[wid], c) for wid, c in counts.items()]
    pairs.sort(key=lambda nc: -nc[1])
    return pairs
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest ansible/roles/containers/monitor-bridge/files/test_check.py -k n8n -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/monitor-bridge/files/check.py ansible/roles/containers/monitor-bridge/files/test_check.py
git commit -m "$(cat <<'EOF'
monitor-bridge: add n8n_failures pure filter (active-workflow failures in window)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `check_n8n` + env wiring + `_get_json` headers

Wire the pure logic into a check: read the n8n env vars, extend `_get_json` to send the API-key header, add `check_n8n`, and register it in `CHECKS`.

**Files:**
- Modify: `ansible/roles/containers/monitor-bridge/files/check.py`
- Test: `ansible/roles/containers/monitor-bridge/files/test_check.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_check.py`:

```python
# --- check_n8n --------------------------------------------------------------

def test_n8n_disabled_without_key():
    # N8N_API_KEY defaults to "" in tests -> monitoring disabled, never a false page
    ok, msg = check.check_n8n()
    assert ok
    assert "disabled" in msg.lower()


def test_n8n_check_down_on_recent_failure(monkeypatch):
    monkeypatch.setattr(check, "N8N_API_KEY", "x")
    wf = {"data": [{"id": "1", "name": "Prod Flow", "active": True}]}
    now_iso = datetime.now(timezone.utc).isoformat()
    ex = {"data": [{"workflowId": "1", "status": "error", "stoppedAt": now_iso}]}
    monkeypatch.setattr(check, "_get_json", _seq(wf, ex))
    ok, msg = check.check_n8n()
    assert not ok
    assert "Prod Flow" in msg


def test_n8n_check_ok_when_no_failures(monkeypatch):
    monkeypatch.setattr(check, "N8N_API_KEY", "x")
    wf = {"data": [{"id": "1", "name": "Prod Flow", "active": True}]}
    ex = {"data": []}
    monkeypatch.setattr(check, "_get_json", _seq(wf, ex))
    ok, msg = check.check_n8n()
    assert ok
    assert "no active-workflow failures" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest ansible/roles/containers/monitor-bridge/files/test_check.py -k "n8n_disabled or n8n_check" -v`
Expected: FAIL with `AttributeError: module 'check' has no attribute 'check_n8n'`

- [ ] **Step 3: Write minimal implementation**

**3a.** In `check.py`, extend `_get_json` (lines 49-52) to accept optional extra headers (backward compatible — existing callers pass no headers):

```python
def _get_json(url, headers=None):
    hdrs = {"User-Agent": "monitor-bridge"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 (internal URLs)
        return json.load(resp)
```

**3b.** Add the n8n env constants after the TRAEFIK block (after line 44, `TRAEFIK_MIN_RPS = ...`):

```python
N8N_URL = _env("N8N_URL", "http://n8n:5678").rstrip("/")
N8N_API_KEY = _env("N8N_API_KEY", "")
N8N_FAIL_WINDOW = _env("N8N_FAIL_WINDOW", "15m")
N8N_FAIL_MAX = float(_env("N8N_FAIL_MAX", "0"))
```

**3c.** Add `check_n8n` immediately after `n8n_failures` (before `CHECKS`):

```python
def check_n8n():
    """Failed executions of active ("Prod") n8n workflows within N8N_FAIL_WINDOW.

    Polls the n8n public API on the internal network (X-N8N-API-KEY header, no Authelia).
    Empty N8N_API_KEY -> disabled (stays up) so it never false-pages before the operator
    sets the key. An unreachable/erroring API raises -> the loop renders it down with the
    error, like check_targets_down (a dead API surfaces, not silent-green).
    """
    if not N8N_API_KEY:
        return True, "n8n monitoring disabled (no API key)"
    headers = {"X-N8N-API-KEY": N8N_API_KEY}
    workflows = _get_json(N8N_URL + "/api/v1/workflows?active=true&limit=250", headers=headers)
    executions = _get_json(N8N_URL + "/api/v1/executions?status=error&limit=100", headers=headers)
    offenders = n8n_failures(workflows, executions, parse_duration(N8N_FAIL_WINDOW))
    total = sum(c for _, c in offenders)
    if total > N8N_FAIL_MAX:
        desc = ", ".join("%s (%d)" % (n, c) for n, c in offenders[:5])
        return False, "%d active workflow(s) failed in %s: %s" % (
            len(offenders), N8N_FAIL_WINDOW, desc)
    return True, "no active-workflow failures in %s" % N8N_FAIL_WINDOW
```

**3d.** Register the check in the `CHECKS` list (after the `traefik5xx` entry, ~line 291):

```python
    ("n8n", _env("KUMA_PUSH_N8N", ""), check_n8n),
```

- [ ] **Step 4: Run the full monitor-bridge suite**

Run: `uv run pytest ansible/roles/containers/monitor-bridge/files -v`
Expected: PASS — all pre-existing tests plus the 10 new n8n/parse_duration tests. (Confirms the `_get_json` signature change didn't break the existing `_get_json`-monkeypatching tests.)

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/monitor-bridge/files/check.py ansible/roles/containers/monitor-bridge/files/test_check.py
git commit -m "$(cat <<'EOF'
monitor-bridge: add check_n8n for active-workflow execution failures

Polls the n8n public API (X-N8N-API-KEY over the internal network), reports
down when an active workflow failed within N8N_FAIL_WINDOW. Empty API key =
disabled. _get_json gains an optional headers arg (backward compatible).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Compose template + host_vars wiring

Expose the env to the container, declare the AutoKuma push monitor, and put monitor-bridge on the `apps` network so it can reach `n8n:5678`.

**Files:**
- Modify: `ansible/roles/containers/monitor-bridge/templates/docker-compose.yml.j2`
- Modify: `ansible/inventory/host_vars/daniel-server.yml`

- [ ] **Step 1: Add the n8n env block to the compose template**

In `docker-compose.yml.j2`, after the `- KUMA_PUSH_TRAEFIK={{ monitor_bridge_traefik_push_token }}` line (line 52), add:

```jinja
      # n8n Prod-workflow failure monitoring (active workflows via the n8n public API).
      - N8N_URL=http://n8n:5678
      - N8N_API_KEY={{ n8n_api_key }}
      - N8N_FAIL_WINDOW=15m
      - N8N_FAIL_MAX=0
      - KUMA_PUSH_N8N={{ monitor_bridge_n8n_push_token }}
```

- [ ] **Step 2: Add the AutoKuma push-monitor label**

In the same file, after the `monitor-bridge-traefik` label line (line 67) and before the `{# Host GitOps deployer liveness ... #}` comment (line 68), add:

```jinja
      {{ kuma('monitor-bridge-n8n', monitor_type='push', name='n8n Prod Workflows', interval=600, push_token=monitor_bridge_n8n_push_token) }}
```

- [ ] **Step 3: Add the `apps` network in host_vars**

In `ansible/inventory/host_vars/daniel-server.yml`, find the monitor-bridge `networks:` block (around lines 236-241). Replace the comment + list so it reads:

```yaml
    networks:
      # monitoring: reach prometheus + uptime-kuma. kopia: query the backup source's
      # status without putting kopia on the broad monitoring net (trusted-infra access,
      # like traefik). apps: reach the n8n public API at n8n:5678 for the workflow-failure
      # check. No web UI, so no port / Authelia.
      - monitoring
      - kopia
      - apps
```

- [ ] **Step 4: Validate the rendered template**

Run: `uv run python scripts/validate_compose_templates.py 2>/dev/null || prek run validate-compose-templates --all-files`
Expected: PASS — the monitor-bridge template renders without Jinja/whitespace errors.

> Note: rendering needs the new vars defined. If the validator stubs vars it will pass; if it decrypts real secrets, this step depends on Task 5 being done first. If it errors on undefined `n8n_api_key`/`monitor_bridge_n8n_push_token`, do Task 5 then re-run this step.

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/monitor-bridge/templates/docker-compose.yml.j2 ansible/inventory/host_vars/daniel-server.yml
git commit -m "$(cat <<'EOF'
monitor-bridge: wire the n8n workflow-failure monitor (env, Kuma label, apps net)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Secrets (operator step)

Two secrets must exist in `ansible/vars/secrets.yml` before deploy, or Jinja rendering fails on the undefined vars. **This is a manual step** — the n8n API key can only be minted in the n8n UI.

**Files:**
- Modify: `ansible/vars/secrets.yml` (encrypted; edit via `sops`)

- [ ] **Step 1: Mint the Kuma push token**

Generate a 32-char alphanumeric token (Kuma rejects other lengths; AutoKuma silently refuses an invalid `push_token`):

Run: `openssl rand -hex 16`
Copy the 32-char output.

- [ ] **Step 2: Mint the n8n API key**

In the n8n UI: **Settings → n8n API → Create an API key**. Scope it to read **Workflow** and **Execution** resources. Copy the key.

- [ ] **Step 3: Add both to secrets**

Run: `sops ansible/vars/secrets.yml`
Add (values from Steps 1-2):

```yaml
monitor_bridge_n8n_push_token: "<32-char-token-from-step-1>"
n8n_api_key: "<api-key-from-step-2>"
```

Save and close (SOPS re-encrypts on save). No commit content is plaintext — `git add ansible/vars/secrets.yml && git commit` is safe and the gitleaks pre-commit hook will verify.

- [ ] **Step 4: Commit the re-encrypted secrets**

```bash
git add ansible/vars/secrets.yml
git commit -m "$(cat <<'EOF'
secrets: add n8n API key + monitor-bridge n8n push token

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Documentation

Bring `monitor-bridge/CLAUDE.md` in line with the new check.

**Files:**
- Modify: `ansible/roles/containers/monitor-bridge/CLAUDE.md`

- [ ] **Step 1: Update the role doc**

Make these edits in `ansible/roles/containers/monitor-bridge/CLAUDE.md`:

1. In **Networks** (line 9-11): append `apps` — e.g. add `+ apps (reach the n8n public API at n8n:5678)` to the networks line.
2. In **Depends on** (line 12): n8n is *not* a hard dep (unreachable n8n just goes `down`), so leave `meta/deps.yml` unchanged; no edit needed here unless you also add n8n to deps.
3. Change "it runs **nine checks**" → "it runs **ten checks**" (line 17).
4. Add a bullet to the checks list (after **Traefik 5xx**, line 35):

```markdown
  - **n8n Prod Workflows** (n8n public API: failed executions of *active* workflows within
    `N8N_FAIL_WINDOW`, naming each one. "Prod" = active. Empty `N8N_API_KEY` = disabled
    (stays up); an unreachable API surfaces as `down`. Reached at `n8n:5678` over `apps`,
    bypassing Authelia via the `X-N8N-API-KEY` header. Caps the workflow page at 250 and the
    error-execution page at 100 — ample for a homelab window.)
```

5. Update the push-tokens line (line 41) to include `n8n`:

```markdown
- Push tokens (`monitor_bridge_{kopia,disk,cert,mem,restarts,oom,cpu,targets,traefik,n8n}_push_token`)
```

6. Add to the env-tunables line (line 44-46): `N8N_FAIL_WINDOW`/`N8N_FAIL_MAX` (and note `N8N_URL`/`N8N_API_KEY` are connection config).
7. In **Operator prerequisites** (after the push-token item): add a note that the n8n monitor also needs `n8n_api_key` in `secrets.yml`, minted in **n8n UI → Settings → n8n API**, scoped to read Workflow + Execution.

- [ ] **Step 2: Commit**

```bash
git add ansible/roles/containers/monitor-bridge/CLAUDE.md
git commit -m "$(cat <<'EOF'
monitor-bridge: document the n8n Prod-workflow check (10 checks now)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Deploy & verify

**Files:** none (deploy + runtime verification)

- [ ] **Step 1: Run the full pre-commit suite**

Run: `prek run --all-files`
Expected: PASS — lint, template validation, gitleaks, and the pytest hook (incl. the new n8n tests).

- [ ] **Step 2: Dry-run the deploy**

Run: `uv run ansible-playbook ansible/deploy.yml --tags "monitor-bridge" --check`
Expected: no errors; shows the monitor-bridge container would be recreated (check.py + compose changed).

- [ ] **Step 3: Deploy**

Run: `uv run ansible-playbook ansible/deploy.yml --tags "monitor-bridge"`
Expected: `ok`/`changed`, no failures. The container recreates because `check.py` changed (per the role's `common_config_changed` wiring) and the compose env/labels changed.

- [ ] **Step 4: Smoke-test one pass**

Run: `docker exec monitor-bridge python /app/check.py --once`
Expected: a line `OK   n8n - no active-workflow failures in 15m` (or a descriptive `DOWN n8n - ...` if a real active workflow failed in the last 15m, or `n8n check error: ...` if the API key/scope is wrong — fix the key if so).

- [ ] **Step 5: Confirm the monitor in Uptime Kuma**

In the Uptime Kuma UI, confirm a push monitor named **n8n Prod Workflows** exists (AutoKuma-provisioned), is receiving heartbeats, and has the Discord notification attached (automatic via the `kuma()` macro). Optionally force a `down`: temporarily deactivate-then-fail a throwaway workflow, or trust the unit tests + the `--once` output.

---

## Self-Review

**Spec coverage:**
- Detection logic (workflows?active=true + executions?status=error, window filter, name offenders) → Tasks 2-3. ✓
- Graceful states (empty key = up; unreachable = down) → Task 3 (`check_n8n` + the loop's existing try/except). ✓
- `parse_rfc3339` reuse for n8n timestamps → Task 2. ✓
- Compose env + AutoKuma label + `apps` network → Task 4. ✓
- Secrets (`monitor_bridge_n8n_push_token` 32-char, `n8n_api_key`) → Task 5. ✓
- Tests (in/out of window, inactive ignored, multi-count sorted, empty, missing stoppedAt) → Task 2; plus check_n8n disabled/down/ok → Task 3. ✓
- Docs (10 checks, n8n bullet, apps net, prereqs, tunables) → Task 6. ✓
- Deploy/verify path → Task 7. ✓
- `role_deps` left unchanged (per spec default) → noted in Task 6. ✓

**Placeholder scan:** none — every code/test step has full code; every command has expected output.

**Type/name consistency:** `parse_duration` (Task 1) used in `check_n8n` (Task 3); `n8n_failures(workflows_json, executions_json, window_s, now=None)` (Task 2) called with positional `parse_duration(N8N_FAIL_WINDOW)` in Task 3; env names (`N8N_URL`, `N8N_API_KEY`, `N8N_FAIL_WINDOW`, `N8N_FAIL_MAX`, `KUMA_PUSH_N8N`) consistent across check.py (Task 3) and compose (Task 4); secret names (`n8n_api_key`, `monitor_bridge_n8n_push_token`) consistent across compose (Task 4) and secrets (Task 5). ✓
