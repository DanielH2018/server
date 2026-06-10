# GitOps Deploy monitor split — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single flapping `GitOps Deploy` push monitor with two robust, work-immune monitors — *Alive* (deployer is ticking) and *Status* (a deploy rolled back) — by moving the heartbeat off the host deployer and into the proven `monitor-bridge` poller.

**Architecture:** The deployer (`gitops_deploy.py`) stops talking to Kuma and only writes two state files (`last_run`, existing `hold_sha`). `monitor-bridge` reads them via a read-only bind-mount and pushes the two monitors in its existing 5-min loop, exactly like its 10 other checks. Heartbeats now depend only on the clock + cheap state files, so normal operator activity can't trip them.

**Tech Stack:** Python stdlib (`check.py` on `python:3.12-alpine`; `gitops_deploy.py` on host py3), pytest, Ansible + Jinja2 compose templates, Uptime-Kuma push monitors via AutoKuma labels, SOPS-encrypted secrets.

**Spec:** `docs/superpowers/specs/2026-06-09-gitops-deploy-monitor-split-design.md`

---

## File structure

| File | Change | Responsibility |
|---|---|---|
| `ansible/roles/containers/monitor-bridge/files/check.py` | modify | Add 2 pure fns + 2 check wrappers + 2 env constants + 2 CHECKS entries |
| `ansible/roles/containers/monitor-bridge/files/test_check.py` | modify | Unit tests for the new pure fns + file-reading wrappers |
| `ansible/roles/containers/monitor-bridge/templates/docker-compose.yml.j2` | modify | Bind-mount state dir, 4 env vars, replace 1 AutoKuma label with 2 |
| `ansible/roles/setup/gitops_deploy/files/gitops_deploy.py` | modify | Write `last_run`; remove `kuma_push` + Kuma config/imports |
| `ansible/roles/setup/gitops_deploy/templates/config.env.j2` | modify | Drop the 3 now-unused `KUMA_*`/`MONITORING_NETWORK` lines |
| `ansible/vars/secrets.yml` (SOPS) | modify | Add 2 push tokens, retire the old one |

**Deploy order (Task 6):** secrets (Task 4) must exist, then `gitops_deploy` (writes `last_run`, owns the bind-mount dir), then `monitor-bridge`.

---

### Task 1: monitor-bridge pure logic — `gitops_alive` + `gitops_status`

**Files:**
- Modify: `ansible/roles/containers/monitor-bridge/files/check.py`
- Test: `ansible/roles/containers/monitor-bridge/files/test_check.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_check.py`:
```python
# --- gitops_alive / gitops_status (pure) ------------------------------------

def test_gitops_alive_fresh():
    ok, msg = check.gitops_alive(60, 5400)
    assert ok
    assert "1m ago" in msg

def test_gitops_alive_at_threshold_is_ok():
    # exactly at max age still counts as alive (<=)
    ok, _ = check.gitops_alive(5400, 5400)
    assert ok

def test_gitops_alive_stale():
    ok, msg = check.gitops_alive(6000, 5400)  # 100m > 90m
    assert not ok
    assert "100m ago" in msg

def test_gitops_status_no_hold():
    ok, msg = check.gitops_status(None)
    assert ok
    assert msg == "no held deploy"

def test_gitops_status_empty_is_ok():
    ok, _ = check.gitops_status("")
    assert ok

def test_gitops_status_held_names_sha():
    ok, msg = check.gitops_status("abc123def4567890")
    assert not ok
    assert "abc123de" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest ansible/roles/containers/monitor-bridge/files -k gitops -v`
Expected: FAIL — `AttributeError: module 'check' has no attribute 'gitops_alive'`

- [ ] **Step 3: Write the pure functions**

In `check.py`, add immediately after the `n8n_failures` function (just before the `# --- checks: each returns (ok, msg)` line is fine too — place with the other pure helpers, before `def check_n8n`):
```python
def gitops_alive(age_s, max_age_s):
    """Pure: is the deployer's last completed tick recent enough? Returns (ok, msg)."""
    if age_s <= max_age_s:
        return True, "deployer ran %.0fm ago" % (age_s / 60)
    return False, "deployer last ran %.0fm ago (> %.0fm)" % (age_s / 60, max_age_s / 60)


def gitops_status(hold_sha):
    """Pure: is a rolled-back commit being held? Returns (ok, msg)."""
    if not hold_sha:
        return True, "no held deploy"
    return False, "deploy held at %s — revert the offending PR" % hold_sha[:8]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest ansible/roles/containers/monitor-bridge/files -k gitops -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/monitor-bridge/files/check.py ansible/roles/containers/monitor-bridge/files/test_check.py
git commit -m "monitor-bridge: add gitops_alive/gitops_status pure logic + tests" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: monitor-bridge wiring — env constants, file-reading checks, CHECKS

**Files:**
- Modify: `ansible/roles/containers/monitor-bridge/files/check.py`
- Test: `ansible/roles/containers/monitor-bridge/files/test_check.py`

- [ ] **Step 1: Write the failing wrapper tests**

At the top of `test_check.py`, add `import time` (the file currently imports only `datetime`/`pytest`/`check`). Then append:
```python
# --- check_gitops_alive / check_gitops_status (file I/O) ---------------------

def _gw(tmp_path, name, content):
    (tmp_path / name).write_text(content)

def test_check_gitops_alive_fresh_file(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "GITOPS_STATE_DIR", str(tmp_path))
    _gw(tmp_path, "last_run", str(time.time()))
    ok, _ = check.check_gitops_alive()
    assert ok

def test_check_gitops_alive_stale_file(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "GITOPS_STATE_DIR", str(tmp_path))
    _gw(tmp_path, "last_run", str(time.time() - 100 * 60))  # 100m old > default 90m
    ok, _ = check.check_gitops_alive()
    assert not ok

def test_check_gitops_alive_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "GITOPS_STATE_DIR", str(tmp_path))
    ok, msg = check.check_gitops_alive()
    assert not ok
    assert "no last_run" in msg

def test_check_gitops_alive_unparseable(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "GITOPS_STATE_DIR", str(tmp_path))
    _gw(tmp_path, "last_run", "not-a-float")
    ok, msg = check.check_gitops_alive()
    assert not ok
    assert "unparseable" in msg

def test_check_gitops_status_no_file_is_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "GITOPS_STATE_DIR", str(tmp_path))
    ok, _ = check.check_gitops_status()
    assert ok

def test_check_gitops_status_held(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "GITOPS_STATE_DIR", str(tmp_path))
    _gw(tmp_path, "hold_sha", "abc123def4567890")
    ok, msg = check.check_gitops_status()
    assert not ok
    assert "abc123de" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest ansible/roles/containers/monitor-bridge/files -k "check_gitops" -v`
Expected: FAIL — `AttributeError: module 'check' has no attribute 'GITOPS_STATE_DIR'` (or `check_gitops_alive`)

- [ ] **Step 3: Add env constants**

In `check.py`, after the `N8N_FAIL_MAX = ...` line (end of the env block, ~line 48), add:
```python
GITOPS_STATE_DIR = _env("GITOPS_STATE_DIR", "/gitops-state")
GITOPS_MAX_AGE_S = float(_env("GITOPS_MAX_AGE_MIN", "90")) * 60
```

- [ ] **Step 4: Add the check wrappers**

In `check.py`, add immediately after `def check_n8n(): ...` (just before the `CHECKS = [` list):
```python
def check_gitops_alive():
    try:
        with open(os.path.join(GITOPS_STATE_DIR, "last_run")) as fh:
            ts = float(fh.read().strip())
    except FileNotFoundError:
        return False, "no last_run marker (deployer never completed a tick?)"
    except ValueError:
        return False, "last_run marker unparseable"
    return gitops_alive(time.time() - ts, GITOPS_MAX_AGE_S)


def check_gitops_status():
    try:
        with open(os.path.join(GITOPS_STATE_DIR, "hold_sha")) as fh:
            hold = fh.read().strip() or None
    except FileNotFoundError:
        hold = None
    return gitops_status(hold)
```

- [ ] **Step 5: Register both in CHECKS**

In `check.py`, append two entries to the `CHECKS` list (after the `("n8n", ...)` line, before the closing `]`):
```python
    ("gitops_alive",  _env("KUMA_PUSH_GITOPS_ALIVE",  ""), check_gitops_alive),
    ("gitops_status", _env("KUMA_PUSH_GITOPS_STATUS", ""), check_gitops_status),
```

- [ ] **Step 6: Run the full monitor-bridge suite**

Run: `uv run pytest ansible/roles/containers/monitor-bridge/files -v`
Expected: PASS (all existing tests + the 12 new gitops tests)

- [ ] **Step 7: Commit**

```bash
git add ansible/roles/containers/monitor-bridge/files/check.py ansible/roles/containers/monitor-bridge/files/test_check.py
git commit -m "monitor-bridge: wire check_gitops_alive/status from deployer state files" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Deployer — write `last_run`, drop all Kuma pushing

**Files:**
- Modify: `ansible/roles/setup/gitops_deploy/files/gitops_deploy.py`
- Modify: `ansible/roles/setup/gitops_deploy/templates/config.env.j2`

No new unit test: this task is a removal (delete `kuma_push`) plus a trivial I/O write. `deploy_logic.py` (the pure, tested module) is unchanged; we verify by re-running its suite + a syntax/import check + a grep for dangling references.

- [ ] **Step 1: Add the `LAST_RUN` constant**

In `gitops_deploy.py`, after `HOLD_FILE = "/var/lib/gitops-deploy/hold_sha"`:
```python
HOLD_FILE = "/var/lib/gitops-deploy/hold_sha"
LAST_RUN = "/var/lib/gitops-deploy/last_run"
```

- [ ] **Step 2: Remove now-unused imports/constants**

Delete the line `import urllib.parse` (kept: `import urllib.request`, used by `discord`).
Delete the line `CURL_IMAGE = "curlimages/curl:8.11.1"  # pinned; throwaway push-ping container`.
Delete the line `NET = C.get("MONITORING_NETWORK", "monitoring")`.

- [ ] **Step 3: Delete the `kuma_push` function**

Remove the entire function (from `def kuma_push(status: str, msg: str) -> None:` through its final `log(f"kuma push failed ...")` line).

- [ ] **Step 4: Remove every `kuma_push(...)` call in `main()`**

Delete these 7 lines (leave the surrounding `discord`/`write_hold`/`_write_marker`/`return` lines intact):
```python
        kuma_push("up", "working tree dirty — skipping")
        kuma_push("up", "in sync")
        kuma_push("up", "holding known-bad commit")
        kuma_push("up", "broad change deferred")
        kuma_push("up", "no service change")
        kuma_push("up", f"deployed {','.join(sorted(cs.services))}")
        kuma_push("down", f"rolled back {','.join(failed)}")
```

- [ ] **Step 5: Write `last_run` on non-crashing completion**

Replace the `__main__` block:
```python
if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        discord(f"🚨 gitops-deploy crashed: {e}")
        raise
```
with:
```python
if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:
        discord(f"🚨 gitops-deploy crashed: {e}")
        raise
    # Liveness marker: a tick that completed without crashing (incl. a rollback, rc=1).
    # monitor-bridge reads this; a crash skips the write so the Alive monitor goes stale.
    _write_marker(LAST_RUN, str(time.time()))
    sys.exit(rc)
```

- [ ] **Step 6: Trim the deployer config**

In `config.env.j2`, delete these three lines (keep `REPO_DIR`, `BRANCH`, `HEALTH_TIMEOUT_S`, `DISCORD_WEBHOOK`):
```
MONITORING_NETWORK=monitoring
KUMA_PUSH_TOKEN={{ gitops_deploy_kuma_push_token }}
KUMA_PUSH_URL_BASE=http://uptime-kuma:3001/api/push
```

- [ ] **Step 7: Verify — no dangling references, syntax OK, logic tests green**

Run:
```bash
grep -rn "kuma_push\|CURL_IMAGE\|urllib.parse\|MONITORING_NETWORK\|KUMA_PUSH\|gitops_deploy_kuma_push_token" ansible/roles/setup/gitops_deploy/ ; echo "grep-exit=$?"
python3 -m py_compile ansible/roles/setup/gitops_deploy/files/gitops_deploy.py && echo "compile OK"
uv run pytest ansible/roles/setup/gitops_deploy/files -v
```
Expected: grep prints nothing (`grep-exit=1`); `compile OK`; deploy_logic tests PASS.

- [ ] **Step 8: Commit**

```bash
git add ansible/roles/setup/gitops_deploy/files/gitops_deploy.py ansible/roles/setup/gitops_deploy/templates/config.env.j2
git commit -m "gitops_deploy: write last_run marker; remove all Kuma pushing (moved to monitor-bridge)" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Secrets — two push tokens, retire the old one

**Files:**
- Modify: `ansible/vars/secrets.yml` (via `sops`)

Kuma requires push tokens that are **exactly 32 alphanumeric chars**; `openssl rand -hex 16` produces 32 hex chars and satisfies it.

- [ ] **Step 1: Generate two tokens**

Run: `openssl rand -hex 16; openssl rand -hex 16`
Copy the two values.

- [ ] **Step 2: Edit secrets**

Run: `sops ansible/vars/secrets.yml`
- Add:
  ```yaml
  monitor_bridge_gitops_alive_push_token: <first token>
  monitor_bridge_gitops_status_push_token: <second token>
  ```
- Delete the line `gitops_deploy_kuma_push_token: ...`.

- [ ] **Step 3: Verify keys present / old key gone (no values printed)**

Run:
```bash
sops -d ansible/vars/secrets.yml | grep -c monitor_bridge_gitops_alive_push_token
sops -d ansible/vars/secrets.yml | grep -c monitor_bridge_gitops_status_push_token
sops -d ansible/vars/secrets.yml | grep -c gitops_deploy_kuma_push_token
```
Expected: `1`, `1`, `0`.

- [ ] **Step 4: Commit**

```bash
git add ansible/vars/secrets.yml
git commit -m "secrets: add gitops alive/status push tokens; retire gitops_deploy_kuma_push_token" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: monitor-bridge compose — bind-mount, env, two AutoKuma labels

**Files:**
- Modify: `ansible/roles/containers/monitor-bridge/templates/docker-compose.yml.j2`

- [ ] **Step 1: Bind-mount the deployer state dir (read-only)**

Replace:
```yaml
    volumes:
      - ./check.py:/app/check.py:ro
```
with:
```yaml
    volumes:
      - ./check.py:/app/check.py:ro
      - /var/lib/gitops-deploy:/gitops-state:ro
```

- [ ] **Step 2: Add env vars**

After the `- KUMA_PUSH_N8N={{ monitor_bridge_n8n_push_token }}` line, add:
```yaml
      # GitOps deployer liveness + deploy-status. State files are written on the host by
      # gitops_deploy.py and bind-mounted read-only at /gitops-state.
      - GITOPS_STATE_DIR=/gitops-state
      - GITOPS_MAX_AGE_MIN=90
      - KUMA_PUSH_GITOPS_ALIVE={{ monitor_bridge_gitops_alive_push_token }}
      - KUMA_PUSH_GITOPS_STATUS={{ monitor_bridge_gitops_status_push_token }}
```

- [ ] **Step 3: Replace the single AutoKuma label with two**

Replace:
```jinja
      {# Host GitOps deployer liveness — interval 2400s (40m) > the 30m tick so one missed push isn't a false alarm. -#}
      {{ kuma('gitops-deploy', monitor_type='push', name='GitOps Deploy', interval=2400, push_token=gitops_deploy_kuma_push_token) }}
```
with:
```jinja
      {# GitOps deployer: liveness (deployer ticking) + deploy-status (rollback hold), pushed by
         check_gitops_alive/status from the host state files. interval 600s = 2× the 5-min loop. -#}
      {{ kuma('gitops-deploy-alive', monitor_type='push', name='GitOps Deploy — Alive', interval=600, push_token=monitor_bridge_gitops_alive_push_token) }}
      {{ kuma('gitops-deploy-status', monitor_type='push', name='GitOps Deploy — Status', interval=600, push_token=monitor_bridge_gitops_status_push_token) }}
```

- [ ] **Step 4: Validate the rendered template**

Run: `prek run validate-compose-templates --all-files`
Expected: PASS (no Jinja/whitespace errors; tokens resolve from secrets added in Task 4).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/monitor-bridge/templates/docker-compose.yml.j2
git commit -m "monitor-bridge: split GitOps monitor into Alive + Status (replace single push monitor)" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Deploy & verify (operational — no code commit)

**Pre-req:** `/var/lib/gitops-deploy` must exist owned `ubuntu:ubuntu` before `monitor-bridge` mounts it. The `gitops_deploy` role creates it, so deploy `gitops_deploy` **first**; otherwise Docker auto-creates the mount source root-owned. (On daniel-server `initial_setup` has already run, so the dir exists — but the order below is safe regardless.)

- [ ] **Step 1: Run the full pre-deploy gate**

Run: `prek run --all-files`
Expected: PASS (pytest, yaml lint, ansible-lint, validate-compose-templates, gitleaks).

- [ ] **Step 2: Deploy the deployer (writes `last_run`, drops Kuma config)**

Run: `uv run ansible-playbook ansible/initial_setup.yml --tags gitops_deploy`
The `Run gitops-deploy once` handler fires on the script/config change → the deployer runs once and writes `last_run`.

- [ ] **Step 3: Verify the state file exists**

Run: `ls -l /var/lib/gitops-deploy/last_run && cat /var/lib/gitops-deploy/last_run`
Expected: file present, owner `ubuntu`, contents a unix timestamp (e.g. `1749...`).

- [ ] **Step 4: Deploy monitor-bridge**

Run: `uv run ansible-playbook ansible/deploy.yml --tags monitor-bridge`

- [ ] **Step 5: Smoke-test one pass**

Run: `docker exec monitor-bridge python /app/check.py --once 2>&1 | grep -i gitops`
Expected: two lines — `OK  gitops_alive - deployer ran 0m ago` and `OK  gitops_status - no held deploy`.

- [ ] **Step 6: Verify Kuma migrated cleanly**

Run:
```bash
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('file:/home/ubuntu/server/containers/uptime-kuma/data/kuma.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row
for r in con.execute("select id,name,active from monitor where name like 'GitOps Deploy%'"):
    d = dict(r)
    hb = con.execute("select status,msg,time from heartbeat where monitor_id=? order by time desc limit 1", (d['id'],)).fetchone()
    print(d['id'], repr(d['name']), 'active=', d['active'], '| last:', dict(hb) if hb else None)
con.close()
PY
```
Expected: rows for `GitOps Deploy — Alive` and `GitOps Deploy — Status` with recent `status=1` heartbeats. The old `GitOps Deploy` (id 198) should be **gone** — if AutoKuma left it as an orphan, delete it once via the Kuma UI and note that AutoKuma doesn't auto-delete on label removal here.

- [ ] **Step 7: Check the flagged `oneshot` start-timeout risk**

Run: `systemctl show gitops-deploy.service -p TimeoutStartUSec`
If the value is finite and smaller than a realistic full deploy + health-gate (could be many minutes), file a follow-up to add `TimeoutStartSec=0` to `gitops-deploy.service.j2` (a killed deploy = false rollback). If `infinity`, no action.

---

## Self-review

- **Spec coverage:** deployer `last_run` + de-Kuma (Task 3); `config.env` trim (Task 3); `check.py` pure fns/wrappers/env/CHECKS (Tasks 1–2); compose bind-mount/env/labels (Task 5); secrets ±tokens (Task 4); tests (Tasks 1–2); migration + both risks + rollout order (Task 6). ✓
- **Placeholders:** none — `<token>` in Task 4 is genuine operator input, not a stub.
- **Type/name consistency:** `gitops_alive(age_s, max_age_s)` / `gitops_status(hold_sha)`, wrappers `check_gitops_alive` / `check_gitops_status`, env `GITOPS_STATE_DIR` / `GITOPS_MAX_AGE_MIN`→`GITOPS_MAX_AGE_S`, CHECKS ids `gitops_alive` / `gitops_status`, tokens `monitor_bridge_gitops_alive_push_token` / `monitor_bridge_gitops_status_push_token`, env keys `KUMA_PUSH_GITOPS_ALIVE` / `KUMA_PUSH_GITOPS_STATUS` — consistent across all tasks. ✓
