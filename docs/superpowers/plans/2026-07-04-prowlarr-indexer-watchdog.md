# Prowlarr sustained-indexer watchdog — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a monitor-bridge check that pages only when a Prowlarr indexer has been failing ≥ 30 min (age-based, from Prowlarr's `initialFailure`), suppressing sub-threshold flaps, while keeping Prowlarr's instant all-indexers-down red error.

**Architecture:** A new `check_prowlarr_indexers()` in `monitor-bridge`'s `check.py` polls Prowlarr's `/api/v1/indexerstatus` + `/api/v1/indexer` each 300 s cycle and pushes up/down to a new Uptime-Kuma push monitor — the same pattern as the existing 26 checks (direct precedent: `check_arr_queue`, already hitting the *arr APIs over `media`). The duration gate is age-based (`now − initialFailure`), so it survives a monitor-bridge redeploy. Paired with flipping Prowlarr's in-app notification to `includeHealthWarnings=false` (keeping `onHealthIssue=true` as the instant all-down backstop).

**Tech Stack:** Python 3.14 stdlib (`urllib`, `datetime`), Ansible + Jinja2 compose template, SOPS/age secrets, Uptime-Kuma push monitors via the AutoKuma `kuma()` macro, pytest.

**Spec:** `docs/superpowers/specs/2026-07-04-prowlarr-indexer-watchdog-design.md`

## Global Constraints

- **`containers/` is read-only** — edit only `ansible/roles/containers/monitor-bridge/...`. Never edit `containers/`.
- **check.py is stdlib-only** (runs on `python:3.14-alpine`, no deps). No new imports beyond what's already imported (`json`, `os`, `time`, `urllib.*`, `datetime`).
- **Match the existing file style:** rich docstrings, **no type hints** (the file has none), `%`-formatting. Do NOT add type annotations.
- **Multi-exception `except` uses the no-paren Python 3.14 form** — write `except ValueError, TypeError:`, NOT `except (ValueError, TypeError):`. Parenthesizing makes `ruff format` reject the commit. (This is intentional house style — see the `homelab-editing-and-commit-tooling-traps` memory; the file already uses it at several `except ValueError, TypeError:` sites.)
- **New check is NOT Prometheus-backed** — do not add `"prowlarr_indexers"` to `PROM_DEPENDENT` or `EXPORTER_DEPENDENT`.
- **Kuma push tokens must be exactly 32 alphanumeric chars** (`openssl rand -hex 16`) or AutoKuma silently refuses the monitor.
- **Run everything through `uv`** (`uv run pytest`, `uv run ansible-playbook`) so the pinned env is used.
- **Commit style:** direct to `master`, no feature branch; new commit (never amend); use `git commit -F -` heredoc (backticks in `-m` get zsh-substituted). Explain *why* in the body.
- **No network change** — monitor-bridge is already on `media` (joined 2026-07-02 for `check_arr_queue`) and reaches `prowlarr:9696`.

## File Structure

- `ansible/roles/containers/monitor-bridge/files/check.py` — add config block, pure `indexers_down()`, `check_prowlarr_indexers()` wrapper, and one `CHECKS` entry. (Task 1)
- `ansible/roles/containers/monitor-bridge/files/test_check.py` — add an `indexers_down` pure-logic section + wrapper tests. (Task 1)
- `ansible/roles/containers/monitor-bridge/templates/docker-compose.yml.j2` — add 4 env vars + 1 `kuma()` push-monitor label. (Task 2)
- `ansible/vars/secrets.yml` (SOPS) + `ansible/secret_rotation.yml` — two new secrets. (Task 2)
- Live Prowlarr app DB via API PUT — flip `includeHealthWarnings=false`. (Task 4)
- `~/.claude/projects/-home-ubuntu-server/memory/discord-and-healthchecks-topology.md` — record the pairing. (Task 4)

---

### Task 1: `check.py` logic + unit tests (TDD)

**Files:**
- Modify: `ansible/roles/containers/monitor-bridge/files/check.py`
- Test: `ansible/roles/containers/monitor-bridge/files/test_check.py`

**Interfaces:**
- Consumes: existing `parse_rfc3339(ts)`, `_get_json(url, headers=None)`, `_env(name, default)`, `CHECKS` list.
- Produces:
  - `indexers_down(status_json, name_by_id, now, min_down_min) -> list[(name: str, minutes: float)]`
  - `check_prowlarr_indexers() -> (ok: bool, msg: str)`
  - config globals `PROWLARR_URL`, `PROWLARR_API_KEY`, `PROWLARR_INDEXER_MIN_DOWN_MIN`

- [ ] **Step 1: Write the failing tests**

Append to the end of `test_check.py`:

```python
# --- indexers_down (pure) ---------------------------------------------------

INX_NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
INX_NAMES = {1: "EZTV", 2: "1337x", 3: "YTS"}


def _status(*entries):
    """Prowlarr /api/v1/indexerstatus payload from (indexerId, initialFailure) pairs."""
    return [{"indexerId": iid, "initialFailure": init} for iid, init in entries]


def test_indexers_down_flags_indexer_over_threshold():
    status = _status((1, "2026-07-04T11:20:00Z"))  # 40 min ago
    out = check.indexers_down(status, INX_NAMES, INX_NOW, 30)
    assert out == [("EZTV", pytest.approx(40.0, abs=0.1))]


def test_indexers_down_ignores_sub_threshold_flap():
    status = _status((1, "2026-07-04T11:50:00Z"))  # 10 min ago -> below gate
    assert check.indexers_down(status, INX_NAMES, INX_NOW, 30) == []


def test_indexers_down_empty_status_is_clean():
    assert check.indexers_down([], INX_NAMES, INX_NOW, 30) == []


def test_indexers_down_null_initial_failure_skipped():
    assert check.indexers_down(_status((1, None)), INX_NAMES, INX_NOW, 30) == []


def test_indexers_down_malformed_initial_failure_skipped():
    assert check.indexers_down(_status((1, "not-a-timestamp")), INX_NAMES, INX_NOW, 30) == []


def test_indexers_down_multiple_sorted_worst_first():
    status = _status(
        (1, "2026-07-04T11:40:00Z"),  # EZTV 20m -> below gate
        (2, "2026-07-04T11:00:00Z"),  # 1337x 60m
        (3, "2026-07-04T11:25:00Z"),  # YTS 35m
    )
    out = check.indexers_down(status, INX_NAMES, INX_NOW, 30)
    assert [n for n, _ in out] == ["1337x", "YTS"]  # 60m before 35m; EZTV excluded


def test_indexers_down_unknown_id_falls_back_to_id_label():
    out = check.indexers_down(_status((9, "2026-07-04T11:00:00Z")), INX_NAMES, INX_NOW, 30)
    assert out == [("indexer 9", pytest.approx(60.0, abs=0.1))]


# --- check_prowlarr_indexers (wrapper) --------------------------------------


def test_prowlarr_indexers_disabled_without_key(monkeypatch):
    monkeypatch.setattr(check, "PROWLARR_API_KEY", "")
    ok, msg = check.check_prowlarr_indexers()
    assert ok is True
    assert "disabled" in msg


def test_prowlarr_indexers_down_on_sustained(monkeypatch):
    monkeypatch.setattr(check, "PROWLARR_API_KEY", "k")
    monkeypatch.setattr(check, "PROWLARR_INDEXER_MIN_DOWN_MIN", 30.0)
    status = _status((1, "2000-01-01T00:00:00Z"))  # ancient -> definitely over threshold
    indexers = [{"id": 1, "name": "EZTV"}]
    monkeypatch.setattr(check, "_get_json", _seq(status, indexers))  # status, then indexer list
    ok, msg = check.check_prowlarr_indexers()
    assert ok is False
    assert "EZTV down" in msg


def test_prowlarr_indexers_up_when_none_failing(monkeypatch):
    monkeypatch.setattr(check, "PROWLARR_API_KEY", "k")
    monkeypatch.setattr(check, "_get_json", _seq([], [{"id": 1, "name": "EZTV"}]))
    ok, msg = check.check_prowlarr_indexers()
    assert ok is True
    assert "ok" in msg
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest ansible/roles/containers/monitor-bridge/files/test_check.py -k "indexers_down or prowlarr_indexers" -v`
Expected: FAIL — `AttributeError: module 'check' has no attribute 'indexers_down'` (and `check_prowlarr_indexers`).

- [ ] **Step 3: Add the config block**

In `check.py`, immediately after the Sonarr/Radarr config block (the line `RADARR_API_KEY = _env("RADARR_API_KEY", "")`), add:

```python

# Prowlarr sustained-indexer watchdog: Prowlarr's in-app health notification is binary — with
# warnings on every indexer flap pages, with warnings off only the all-indexers-down red error
# fires; there's no duration grace. We poll /api/v1/indexerstatus and go `down` only when an
# indexer has been FAILING for >= PROWLARR_INDEXER_MIN_DOWN_MIN (age from Prowlarr's own
# initialFailure, so it survives a monitor-bridge redeploy), suppressing the sub-threshold flaps
# public trackers throw that self-clear inside Prowlarr's ~5-15min backoff. Empty key = disabled
# (stays up), same idiom as N8N_API_KEY. Already on `media`, so prowlarr:9696 is reachable.
PROWLARR_URL = _env("PROWLARR_URL", "http://prowlarr:9696").rstrip("/")
PROWLARR_API_KEY = _env("PROWLARR_API_KEY", "")
PROWLARR_INDEXER_MIN_DOWN_MIN = float(_env("PROWLARR_INDEXER_MIN_DOWN_MIN", "30"))
```

- [ ] **Step 4: Add the pure function + wrapper**

In `check.py`, immediately after `check_arr_queue()` (before `check_gitops_alive()`), add:

```python
def indexers_down(status_json, name_by_id, now, min_down_min):
    """Pure: (name, minutes_down) for each Prowlarr indexer failing >= min_down_min minutes.

    Fed /api/v1/indexerstatus (a list of {indexerId, initialFailure, disabledTill, ...}) and an
    indexerId->name map from /api/v1/indexer. An indexer is listed in indexerstatus only while
    Prowlarr has it disabled due to failures; initialFailure is when the CURRENT failure run
    started, so (now - initialFailure) is the outage duration — a flap that recovers before the
    threshold drops out of the list and never qualifies. A null/absent/unparseable initialFailure
    is skipped (treated as just-started) rather than crashing the whole check. Sorted worst-first
    so the longest outage leads the alert msg.
    """
    cutoff_s = min_down_min * 60
    offenders = []
    for s in status_json or []:
        init = s.get("initialFailure")
        if not init:
            continue
        try:
            age_s = (now - parse_rfc3339(init)).total_seconds()
        except ValueError, TypeError:
            continue
        if age_s >= cutoff_s:
            iid = s.get("indexerId")
            offenders.append((name_by_id.get(iid) or "indexer %s" % iid, age_s / 60.0))
    offenders.sort(key=lambda nm: -nm[1])
    return offenders


def check_prowlarr_indexers():
    """Prowlarr sustained-indexer watchdog (see indexers_down): page only when an indexer has been
    failing >= PROWLARR_INDEXER_MIN_DOWN_MIN, not on the brief flaps public trackers throw that
    self-clear inside Prowlarr's backoff.

    Empty PROWLARR_API_KEY -> disabled (stays up), like check_n8n. An unreachable Prowlarr is NOT
    caught here — it bubbles up and _evaluate renders it `down` with the error (the
    check_arr_queue/check_n8n convention; the sustained-failure grace is about indexer flaps, not
    the bridge's own reach). The all-indexers-down red error stays with Prowlarr's own in-app
    onHealthIssue notification — this owns the per-indexer sustained signal Prowlarr can't express.
    """
    if not PROWLARR_API_KEY:
        return True, "prowlarr indexer monitoring disabled (no API key)"
    headers = {"X-Api-Key": PROWLARR_API_KEY}
    status = _get_json(PROWLARR_URL + "/api/v1/indexerstatus", headers=headers)
    indexers = _get_json(PROWLARR_URL + "/api/v1/indexer", headers=headers)
    name_by_id = {i.get("id"): i.get("name") for i in indexers}
    offenders = indexers_down(
        status, name_by_id, datetime.now(timezone.utc), PROWLARR_INDEXER_MIN_DOWN_MIN
    )
    if offenders:
        desc = "; ".join("%s down %.0fm" % (n, m) for n, m in offenders[:5])
        return False, "%d indexer(s) failing >=%gm: %s" % (
            len(offenders),
            PROWLARR_INDEXER_MIN_DOWN_MIN,
            desc,
        )
    return True, "all %d indexer(s) ok (none failing >=%gm)" % (
        len(name_by_id),
        PROWLARR_INDEXER_MIN_DOWN_MIN,
    )
```

- [ ] **Step 5: Register the check in `CHECKS`**

In `check.py`, in the `CHECKS` list, add this line immediately after the `arr_queue` entry:

```python
    ("prowlarr_indexers", _env("KUMA_PUSH_PROWLARR_INDEXERS", ""), check_prowlarr_indexers),
```

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `uv run pytest ansible/roles/containers/monitor-bridge/files/test_check.py -k "indexers_down or prowlarr_indexers" -v`
Expected: PASS (10 tests).

- [ ] **Step 7: Run the FULL monitor-bridge suite (guard test must stay green)**

Run: `uv run pytest ansible/roles/containers/monitor-bridge/files -q`
Expected: PASS — in particular `test_prom_dependent_set_matches_real_checks` (it asserts `PROM_DEPENDENT <= {names in CHECKS}`; adding a non-prom check only grows the name set, so the subset holds).

- [ ] **Step 8: Lint/format check**

Run: `uv run ruff check ansible/roles/containers/monitor-bridge/files/check.py && uv run ruff format --check ansible/roles/containers/monitor-bridge/files/check.py`
Expected: PASS. If `ruff format --check` reports the `except ValueError, TypeError:` line, do NOT parenthesize it — re-read the Global Constraints; the un-parenthesized form is correct and `ruff format` accepts it (parenthesizing is what fails).

- [ ] **Step 9: Commit**

```bash
git add ansible/roles/containers/monitor-bridge/files/check.py ansible/roles/containers/monitor-bridge/files/test_check.py
git commit -F - <<'EOF'
Add Prowlarr sustained-indexer watchdog to monitor-bridge

Prowlarr's in-app health notification can't distinguish a transient
indexer flap from a real outage — warnings-on pages on every flap,
warnings-off only fires on all-indexers-down. This age-based check
pages only when an indexer has been failing >=30 min (from Prowlarr's
own initialFailure timestamp, so it survives a bridge redeploy),
suppressing the sub-threshold flaps public trackers throw that
self-clear inside Prowlarr's backoff.

Pure indexers_down() is unit-tested; the wrapper follows the
check_arr_queue precedent (X-Api-Key over the media network, empty
key disables). Not Prometheus-backed, so not added to PROM_DEPENDENT.
EOF
```

---

### Task 2: Compose template env + Kuma monitor + secrets

**Files:**
- Modify: `ansible/roles/containers/monitor-bridge/templates/docker-compose.yml.j2`
- Modify (SOPS): `ansible/vars/secrets.yml`, `ansible/secret_rotation.yml`

**Interfaces:**
- Consumes: `check.py`'s `KUMA_PUSH_PROWLARR_INDEXERS`, `PROWLARR_URL`, `PROWLARR_API_KEY`, `PROWLARR_INDEXER_MIN_DOWN_MIN` env names (from Task 1); the shared `kuma()` macro.
- Produces: Jinja vars `prowlarr_api_key`, `monitor_bridge_prowlarr_indexers_push_token` (must exist in `secrets.yml` for the template to render at deploy).

- [ ] **Step 1: Add the two secrets**

Both are new (verified: no existing refs to `prowlarr_api_key` / `monitor_bridge_prowlarr_indexers_push_token`). Get the Prowlarr API key value:

Run: `docker exec prowlarr grep -oE "<ApiKey>[^<]+" /config/config.xml`
(Use the printed key as the value below.)

Add via the repo's SOPS flow (the `/add-secret` skill automates edit → `secret_rotation.py sync` → commit; or do it manually):

```bash
# Prowlarr API key — tier assisted (rotate = regenerate in Prowlarr Settings > General, then re-sync)
sops set ansible/vars/secrets.yml '["prowlarr_api_key"]' '"<APIKEY_FROM_ABOVE>"'
# Kuma push token — tier auto, EXACTLY 32 alnum chars
sops set ansible/vars/secrets.yml '["monitor_bridge_prowlarr_indexers_push_token"]' "\"$(openssl rand -hex 16)\""
```

Then register both in the rotation registry and sync:

Run: `uv run python scripts/secret_rotation.py sync`
Expected: it reports the two new secrets added to `ansible/secret_rotation.yml` (assign `prowlarr_api_key` tier `assisted`, the push token tier `auto` — follow the script's prompts / edit the registry entries to match the other `monitor_bridge_*_push_token` tiers).

- [ ] **Step 2: Add the env vars to the compose template**

In `docker-compose.yml.j2`, immediately after the `- KUMA_PUSH_ARR_QUEUE={{ monitor_bridge_arr_queue_push_token }}` line, add:

```yaml
      # Prowlarr sustained-indexer watchdog: Prowlarr's in-app health notification is binary
      # (every flap pages, or — with warnings off — only the all-indexers-down red error). This
      # polls /api/v1/indexerstatus and `down`s only when an indexer has been failing >= 30 min
      # (age from Prowlarr's own initialFailure, so it survives a bridge redeploy), suppressing the
      # sub-threshold flaps public trackers throw that self-clear inside Prowlarr's ~5-15min backoff.
      # Empty key = disabled (stays up), like N8N_API_KEY. Already on `media`, reaches prowlarr:9696.
      - PROWLARR_URL=http://prowlarr:9696
      - PROWLARR_API_KEY={{ prowlarr_api_key }}
      - PROWLARR_INDEXER_MIN_DOWN_MIN=30
      - KUMA_PUSH_PROWLARR_INDEXERS={{ monitor_bridge_prowlarr_indexers_push_token }}
```

- [ ] **Step 3: Add the Kuma push-monitor label**

In `docker-compose.yml.j2`, in the `labels:` block, immediately after the `monitor-bridge-arr-queue` `kuma(...)` label line, add:

```jinja
      {# Prowlarr sustained-indexer watchdog: check_prowlarr_indexers pages only when an indexer
         has been failing >= PROWLARR_INDEXER_MIN_DOWN_MIN (30m) per Prowlarr's initialFailure —
         the sustained-per-indexer signal Prowlarr's binary in-app warning can't express. -#}
      {{ kuma('monitor-bridge-prowlarr-indexers', monitor_type='push', name='Prowlarr Indexers', interval=600, max_retries=0, push_token=monitor_bridge_prowlarr_indexers_push_token) }}
```

(Use a plain `{# ... -#}` Jinja comment exactly like the neighboring labels — do NOT use `{# #}` inside the env list. Both spots here already use `{# ... -#}`.)

- [ ] **Step 4: Verify the template renders to valid YAML**

The `validate-compose` PostToolUse hook re-renders on save; also run it explicitly:

Run: `uv run python scripts/validate_compose_templates.py`
Expected: PASS (no malformed YAML, no un-escaped `$`). If it can't resolve the new secrets, confirm Step 1 wrote them (`sops -d ansible/vars/secrets.yml | grep -E 'prowlarr_api_key|prowlarr_indexers_push_token'`).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/monitor-bridge/templates/docker-compose.yml.j2 ansible/vars/secrets.yml ansible/secret_rotation.yml
git commit -F - <<'EOF'
Wire monitor-bridge Prowlarr Indexers monitor + secrets

Adds the PROWLARR_* env + KUMA_PUSH_PROWLARR_INDEXERS token and the
"Prowlarr Indexers" AutoKuma push monitor for the check added in the
previous commit. New secrets: prowlarr_api_key (assisted) and
monitor_bridge_prowlarr_indexers_push_token (auto).
EOF
```

---

### Task 3: Deploy + smoke-verify the bridge check

**Files:** none (deploy/verify only).

- [ ] **Step 1: Deploy monitor-bridge**

Run: `uv run ansible-playbook ansible/deploy.yml --tags "monitor-bridge"`
Expected: play completes, `monitor-bridge` recreated (the `check.py` bind-mount + env changed).

- [ ] **Step 2: Gate on container health**

Run: `uv run python scripts/probe.py health monitor-bridge`
Expected: exit 0 (running + healthy).

- [ ] **Step 3: Smoke-test one cycle**

Run: `docker exec monitor-bridge python /app/check.py --once`
Expected: the log includes a line like `OK   prowlarr_indexers - all N indexer(s) ok (none failing >=30m)` (N = your indexer count). A `DOWN ... check error` here means the API key/URL is wrong — fix Task 2 Step 1 before proceeding.

- [ ] **Step 4: Confirm the Kuma monitor exists and is UP**

In the Uptime-Kuma UI (or however monitors are normally inspected), confirm a monitor named **Prowlarr Indexers** now exists and is UP with the descriptive msg. AutoKuma provisions it from the label within a cycle.

- [ ] **Step 5: (Optional) exercise the down path live**

The unit tests already cover the down path. A live down-path test needs a genuinely-failing indexer (can't be simulated without one, since a healthy Prowlarr returns an empty `indexerstatus`). Do NOT hack the threshold to 0 in the deployed env (all-healthy still yields no offenders). Skip unless an indexer is actually failing; note in the rollout notes that the down path is unit-verified, not live-verified.

- [ ] **Step 6: No commit** (deploy/verify only). If anything required a fix, it belongs in Task 1 or 2's files — commit there.

---

### Task 4: Flip Prowlarr in-app warnings off + record the pairing

**Do this LAST** — only after Task 3 confirms the bridge monitor is live, so there's no window where Prowlarr's per-indexer warnings are off but the replacement isn't running.

**Files:**
- Live Prowlarr app DB (via API PUT — not git-tracked).
- Modify: `~/.claude/projects/-home-ubuntu-server/memory/discord-and-healthchecks-topology.md`

- [ ] **Step 1: Flip `includeHealthWarnings=false` on Prowlarr (keep `onHealthIssue=true`)**

Round-trip the full notification body, flipping one flag (same method used for Sonarr/Radarr on 2026-07-04). Prowlarr notification id is 1; API key from `config.xml`:

```bash
PKEY=$(docker exec prowlarr grep -oE '<ApiKey>[^<]+' /config/config.xml | cut -d'>' -f2)
docker exec prowlarr curl -s "http://localhost:9696/api/v1/notification/1" -H "X-Api-Key: $PKEY" \
 | python3 -c 'import sys,json; d=json.load(sys.stdin); d["includeHealthWarnings"]=False; sys.stdout.write(json.dumps(d))' \
 | docker exec -i prowlarr curl -s -o /dev/null -w "prowlarr PUT HTTP %{http_code}\n" -X PUT \
     "http://localhost:9696/api/v1/notification/1" -H "X-Api-Key: $PKEY" -H "Content-Type: application/json" -d @-
```
Expected: `prowlarr PUT HTTP 202`.

- [ ] **Step 2: Verify persisted (warnings off, health-issue still on)**

```bash
PKEY=$(docker exec prowlarr grep -oE '<ApiKey>[^<]+' /config/config.xml | cut -d'>' -f2)
docker exec prowlarr curl -s "http://localhost:9696/api/v1/notification/1" -H "X-Api-Key: $PKEY" \
 | python3 -c 'import sys,json; d=json.load(sys.stdin); print("onHealthIssue",d["onHealthIssue"],"includeHealthWarnings",d["includeHealthWarnings"])'
```
Expected: `onHealthIssue True includeHealthWarnings False`.

- [ ] **Step 3: Record the pairing in memory**

Edit `discord-and-healthchecks-topology.md` — extend the 2026-07-04 dedup note to record that Prowlarr is now `includeHealthWarnings=false` too, with the per-indexer sustained signal moved to the monitor-bridge `Prowlarr Indexers` check (30-min age gate), and that Prowlarr keeps `onHealthIssue=true` as the instant all-down backstop. Note this is deliberate — don't re-flag Prowlarr's disabled warnings as a gap.

- [ ] **Step 4: No repo commit needed** (the notification lives in the app DB; SOPS holds the durable webhook copy). The memory file is outside the repo.

---

## Self-Review

**Spec coverage:**
- Age-based gate from `initialFailure`, 30-min default → Task 1 (`indexers_down`, `PROWLARR_INDEXER_MIN_DOWN_MIN=30`). ✓
- Two GETs (`indexerstatus` + `indexer` for id→name) → Task 1 `check_prowlarr_indexers`. ✓
- Null `initialFailure` treated as just-started → Task 1 Step 4 + test `..._null_initial_failure_skipped`. ✓
- Names each sustained indexer + duration; `up` when none → Task 1 wrapper + tests. ✓
- Unreachable Prowlarr → immediate `down` (no grace) via `_evaluate` (not caught in the check) → Task 1 wrapper docstring; deferred 2-cycle grace explicitly NOT built (spec "Out of scope"). ✓
- Empty key disables (stays up) → test `..._disabled_without_key`. ✓
- Not in `PROM_DEPENDENT` → Global Constraints + Task 1 Step 7 guard-test check. ✓
- New env + `kuma()` label, no network change → Task 2. ✓
- Secrets `prowlarr_api_key` (assisted) + push token (auto) → Task 2 Step 1. ✓
- Prowlarr `includeHealthWarnings=false`, keep `onHealthIssue=true` → Task 4. ✓
- Unit tests for below/at/above threshold, null, multi-indexer, empty, disabled → Task 1 Step 1. ✓
- TDD, smoke via `check.py --once`, deploy tag → Tasks 1 & 3. ✓

**Placeholder scan:** none — all code and commands are literal.

**Type consistency:** `indexers_down(status_json, name_by_id, now, min_down_min)` and `check_prowlarr_indexers()` names/args match between Task 1 implementation, tests, and the compose env names (`PROWLARR_URL`/`PROWLARR_API_KEY`/`PROWLARR_INDEXER_MIN_DOWN_MIN`/`KUMA_PUSH_PROWLARR_INDEXERS`) used in Task 2. Monitor slug `monitor-bridge-prowlarr-indexers` / name `Prowlarr Indexers` / token var `monitor_bridge_prowlarr_indexers_push_token` consistent across Task 2 and Task 3.
