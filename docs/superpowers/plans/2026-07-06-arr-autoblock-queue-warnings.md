# arr-autoblock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A least-privilege writer sidecar (`arr-autoblock`) that auto-blocklists the narrow hard-bad + malware-signature classes of stuck Sonarr/Radarr queue items after a grace period, re-searches for a replacement, and reports to Discord — shipping dry-run first.

**Architecture:** The mutating twin of the read-only `monitor-bridge`. A `python:3.14-alpine` stdlib loop polls each *arr `/api/v3/queue`, classifies candidates via a pure decision core, tracks an in-process consecutive-cycle streak for grace, applies a blast-radius cap, then (unless `DRY_RUN`) issues `DELETE …?removeFromClient=true&blocklist=true` + a series/movie re-search command. Health goes to its own Uptime Kuma push monitor; each action is POSTed to the existing *arr Discord webhook.

**Tech Stack:** Python 3.14 stdlib (urllib/json), Docker Compose, Ansible, Uptime Kuma push monitors (AutoKuma), SOPS/age secrets, pytest.

## Global Constraints

- **`containers/` is read-only** — edit only `ansible/roles/containers/arr-autoblock/**` and `ansible/inventory/host_vars/daniel-server.yml`; never edit `containers/`.
- **Stdlib only** — no pip deps (runs on `python:3.14-alpine` with no build step).
- **Direct Discord POSTs MUST send a `User-Agent` header** — without one, Cloudflare 1010-403s the request and it fails silently (exit 0). Repo-known gotcha.
- **Secrets never inlined in `inventory/`** — reference `{{ arr_autoblock_push_token }}` etc.; the value lives in `ansible/vars/secrets.yml` (SOPS). `inventory/` is not hook-guarded or auto-encrypted.
- **Kuma push tokens must be exactly 32 alphanumeric chars** or AutoKuma silently refuses the monitor (`Invalid push_token`).
- **All bridge push monitors set `max_retries=0`** (a pushed `down` with retries parks in PENDING and the watchdog masks the descriptive msg).
- **Idempotent Ansible; `ansible-lint` clean; `no_log: true` on any secret-handling task.**
- **`sanitize()` all attacker-influenced text** (release titles, statusMessages) before it enters a Discord-bound string.
- **Commit directly to `master`** (no feature branches) — user convention.

---

### Task 1: Add the Kuma push token secret

**Files:**
- Modify (via `sops`): `ansible/vars/secrets.yml`
- Modify: `ansible/secret_rotation.yml` (auto-updated by the sync script)

**Interfaces:**
- Produces: the Ansible var `arr_autoblock_push_token` (32 alphanumeric chars), referenced by Task 3's compose template.

- [ ] **Step 1: Generate a 32-char alphanumeric token**

Run: `LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32; echo`
Copy the output (exactly 32 chars — Kuma rejects other lengths/charsets).

- [ ] **Step 2: Add the secret via the `/add-secret` skill (or `sops` directly)**

Preferred: invoke the `/add-secret` skill with name `arr_autoblock_push_token` and the generated value.

Manual equivalent:
```bash
sops ansible/vars/secrets.yml     # add:  arr_autoblock_push_token: <the 32-char token>
```
The `.sops.yaml` at `ansible/` auto-encrypts on save. Do **not** Write/Edit the file directly — the block-protected-edits hook denies it.

- [ ] **Step 3: Sync the rotation registry**

Run: `uv run python scripts/secret_rotation.py sync`
Expected: `arr_autoblock_push_token` added to `ansible/secret_rotation.yml` with a staggered due-date. Tier: default (a Kuma push token is not `pinned`).

- [ ] **Step 4: Verify the registry is in sync**

Run: `uv run python scripts/secret_rotation.py audit`
Expected: exit 0, no "in secrets.yml but not registry" drift for `arr_autoblock_push_token`.

- [ ] **Step 5: Commit**

```bash
git add ansible/vars/secrets.yml ansible/secret_rotation.yml
git commit -F - <<'EOF'
Add arr_autoblock_push_token for the queue auto-blocklist sidecar

The arr-autoblock writer sidecar pushes its own health to a dedicated
"Arr Auto-Block" Uptime Kuma push monitor; this is that monitor's token.
EOF
```

---

### Task 2: The `autoblock.py` script — pure decision core + runtime + tests

**Files:**
- Create: `ansible/roles/containers/arr-autoblock/files/autoblock.py`
- Test: `ansible/roles/containers/arr-autoblock/files/test_autoblock.py`
- Modify: `pyproject.toml:22-32` (add the new dir to `testpaths`)

**Interfaces:**
- Produces (pure, imported by tests): `dangerous(messages, patterns) -> bool`, `is_candidate(item, patterns) -> bool`, `item_key(item) -> str`, `item_reason(item) -> str`, `eligible(candidate_keys, streaks, grace, max_actions) -> (to_act: list[str], held: list[str])` (mutates `streaks` in place), `search_command(app_name, item) -> dict | None`, `format_action(dry_run, app_name, title, reason, streak, grace) -> str`, `sanitize(s, maxlen=120) -> str`.
- Produces (runtime): `run_once(streaks) -> (ok: bool, msg: str)`, `main()`.

- [ ] **Step 1: Write the failing test file**

Create `ansible/roles/containers/arr-autoblock/files/test_autoblock.py`:

```python
import importlib.util
import pathlib

# Load the bind-mounted script directly (not a package), mirroring monitor-bridge/test_check.py.
_SPEC = importlib.util.spec_from_file_location(
    "autoblock", pathlib.Path(__file__).with_name("autoblock.py")
)
autoblock = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(autoblock)

PATTERNS = ["executable file with extension", "potentially dangerous", "sample"]


def _item(status=None, state=None, messages=None, download_id="d1", qid=1,
          series_id=None, movie_id=None, title="Some.Release"):
    it = {"trackedDownloadStatus": status, "trackedDownloadState": state,
          "downloadId": download_id, "id": qid, "title": title}
    if messages is not None:
        it["statusMessages"] = [{"title": title, "messages": messages}]
    if series_id is not None:
        it["seriesId"] = series_id
    if movie_id is not None:
        it["movieId"] = movie_id
    return it


# --- dangerous ---------------------------------------------------------------
def test_dangerous_matches_executable_message_case_insensitively():
    msgs = ["Caution: Found EXECUTABLE File With Extension: '.exe'"]
    assert autoblock.dangerous(msgs, PATTERNS) is True


def test_dangerous_ignores_benign_message():
    assert autoblock.dangerous(["Waiting to import"], PATTERNS) is False


def test_dangerous_empty_is_false():
    assert autoblock.dangerous([], PATTERNS) is False


# --- is_candidate ------------------------------------------------------------
def test_candidate_hard_bad_status_error():
    assert autoblock.is_candidate(_item(status="error"), PATTERNS) is True


def test_candidate_hard_bad_state_import_blocked():
    assert autoblock.is_candidate(_item(state="importBlocked"), PATTERNS) is True


def test_candidate_hard_bad_state_import_failed():
    assert autoblock.is_candidate(_item(state="importFailed"), PATTERNS) is True


def test_plain_warning_is_not_a_candidate():
    # warning with no dangerous message -> notify-only (monitor-bridge pages it), not auto-blocked
    assert autoblock.is_candidate(
        _item(status="warning", messages=["Waiting to import"]), PATTERNS
    ) is False


def test_warning_with_dangerous_message_is_candidate():
    # the 2026-07-01 poisoned-.exe class
    assert autoblock.is_candidate(
        _item(status="warning",
              messages=["Caution: Found executable file with extension: '.exe'"]),
        PATTERNS,
    ) is True


def test_import_pending_with_messages_is_not_a_candidate():
    assert autoblock.is_candidate(
        _item(state="importPending", messages=["Not an upgrade for existing episode"]),
        PATTERNS,
    ) is False


# --- eligible (grace + blast radius) -----------------------------------------
def test_not_eligible_until_grace_met():
    streaks = {}
    assert autoblock.eligible({"a"}, streaks, grace=3, max_actions=5) == ([], [])
    assert autoblock.eligible({"a"}, streaks, grace=3, max_actions=5) == ([], [])
    assert autoblock.eligible({"a"}, streaks, grace=3, max_actions=5) == (["a"], [])
    assert streaks["a"] == 3


def test_streak_resets_when_candidate_clears():
    streaks = {}
    autoblock.eligible({"a"}, streaks, grace=3, max_actions=5)  # a=1
    autoblock.eligible(set(), streaks, grace=3, max_actions=5)  # a cleared
    assert "a" not in streaks
    to_act, _ = autoblock.eligible({"a"}, streaks, grace=3, max_actions=5)  # a=1 again
    assert to_act == []


def test_blast_radius_holds_and_acts_on_none():
    # 6 items all past grace, cap 5 -> act on none, hold all
    streaks = {k: 3 for k in "abcdef"}
    to_act, held = autoblock.eligible(set("abcdef"), streaks, grace=3, max_actions=5)
    assert to_act == []
    assert held == sorted("abcdef")


def test_within_cap_all_act():
    streaks = {k: 3 for k in "abc"}
    to_act, held = autoblock.eligible(set("abc"), streaks, grace=3, max_actions=5)
    assert to_act == sorted("abc")
    assert held == []


# --- search_command ----------------------------------------------------------
def test_search_command_sonarr_series_level():
    cmd = autoblock.search_command("Sonarr", _item(series_id=42))
    assert cmd == {"name": "SeriesSearch", "seriesId": 42}


def test_search_command_radarr_movie_level():
    cmd = autoblock.search_command("Radarr", _item(movie_id=7))
    assert cmd == {"name": "MoviesSearch", "movieIds": [7]}


def test_search_command_missing_id_returns_none():
    assert autoblock.search_command("Sonarr", _item()) is None


# --- item_reason / format_action ---------------------------------------------
def test_item_reason_prefers_status_messages():
    it = _item(status="warning", messages=["Found executable file with extension: '.exe'"])
    assert "executable" in autoblock.item_reason(it)


def test_item_reason_falls_back_to_status():
    assert autoblock.item_reason(_item(status="error")) == "error"


def test_format_action_dry_run_says_would():
    s = autoblock.format_action(True, "Sonarr", "Bad.Release", "importBlocked", 3, 3)
    assert s.startswith("WOULD blocklist [Sonarr]")
    assert "(3/3)" in s


def test_format_action_live_says_blocklisted():
    s = autoblock.format_action(False, "Radarr", "Bad.Movie", "error", 3, 3)
    assert s.startswith("Blocklisted + re-searched [Radarr]")


def test_sanitize_defuses_discord_mentions_and_backticks():
    assert "@" not in autoblock.sanitize("@everyone `rm`")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest ansible/roles/containers/arr-autoblock/files/test_autoblock.py -q`
Expected: FAIL — `autoblock.py` does not exist (`FileNotFoundError` / import error).

- [ ] **Step 3: Write `autoblock.py`**

Create `ansible/roles/containers/arr-autoblock/files/autoblock.py`:

```python
#!/usr/bin/env python3
"""arr-autoblock — auto-blocklist stuck/poisoned Sonarr/Radarr queue items.

The mutating twin of the read-only monitor-bridge. Each cycle it polls Sonarr's and Radarr's
own /api/v3/queue, classifies items as auto-block candidates (the narrow hard-bad +
malware-signature classes), tracks a consecutive-cycle streak in-process for a grace period,
caps the per-cycle blast radius, then — unless DRY_RUN — DELETEs the item with blocklist=true
(removes from client + blocklists the release) and fires a series/movie re-search so the *arr
grabs a clean replacement. Health -> its own Uptime Kuma push monitor; each action -> the *arr
Discord webhook. Stdlib only (python:3.14-alpine); config is env-driven so this stays testable.

Design: docs/superpowers/specs/2026-07-06-arr-autoblock-queue-warnings-design.md
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def _env(name, default):
    return os.environ.get(name, default)


INTERVAL = int(_env("INTERVAL", "300"))
HTTP_TIMEOUT = int(_env("HTTP_TIMEOUT", "10"))
HEARTBEAT_FILE = _env("HEARTBEAT_FILE", "/tmp/heartbeat")
KUMA_URL = _env("KUMA_URL", "http://uptime-kuma:3001").rstrip("/")
KUMA_PUSH = _env("KUMA_PUSH_ARR_AUTOBLOCK", "")

SONARR_URL = _env("SONARR_URL", "http://sonarr:8989").rstrip("/")
SONARR_API_KEY = _env("SONARR_API_KEY", "")
RADARR_URL = _env("RADARR_URL", "http://radarr:7878").rstrip("/")
RADARR_API_KEY = _env("RADARR_API_KEY", "")

DISCORD_WEBHOOK_URL = _env("ARR_DISCORD_WEBHOOK_URL", "")
DRY_RUN = _env("DRY_RUN", "true").strip().lower() in ("1", "true", "yes")
GRACE_CYCLES = int(_env("GRACE_CYCLES", "3"))
MAX_ACTIONS_PER_CYCLE = int(_env("MAX_ACTIONS_PER_CYCLE", "5"))
DANGEROUS_MSG_PATTERNS = [
    p.strip().lower()
    for p in _env(
        "DANGEROUS_MSG_PATTERNS",
        "executable file with extension,potentially dangerous,sample",
    ).split(",")
    if p.strip()
]

HARD_BAD_STATUS = frozenset({"error"})
HARD_BAD_STATE = frozenset({"importBlocked", "importFailed"})


def sanitize(s, maxlen=120):
    """Neutralize adversary-controlled text (release titles, statusMessages) before it enters
    a Discord-bound string: collapse whitespace, defuse @mentions/backticks, cap length."""
    s = "?" if s is None else str(s)
    s = " ".join(s.split())
    s = s.replace("@", "(at)").replace("`", "'")
    if len(s) > maxlen:
        s = s[: maxlen - 3] + "..."
    return s


# --- pure decision core ------------------------------------------------------
def item_messages(item):
    """All statusMessage strings for a queue item, flattened."""
    out = []
    for sm in item.get("statusMessages") or []:
        out.extend(sm.get("messages") or [])
    return out


def dangerous(messages, patterns):
    """True if any statusMessage matches a known-dangerous substring (case-insensitive)."""
    low = [m.lower() for m in messages]
    return any(p in m for p in patterns for m in low)


def is_candidate(item, patterns):
    """A queue item is an auto-block candidate when it is hard-bad OR malware-signature.

    hard-bad: trackedDownloadStatus == 'error' OR trackedDownloadState in importBlocked/importFailed.
    malware-signature: trackedDownloadStatus == 'warning' AND a statusMessage matches `patterns`.
    Everything else (transient warning, plain importPending) is left for the human via the
    read-only Arr Queue Warnings monitor — failing to match fails SAFE (no action).
    """
    status = item.get("trackedDownloadStatus")
    state = item.get("trackedDownloadState")
    if status in HARD_BAD_STATUS or state in HARD_BAD_STATE:
        return True
    if status == "warning" and dangerous(item_messages(item), patterns):
        return True
    return False


def item_key(item):
    """Stable identity across cycles: the download-client hash, falling back to the queue id."""
    return item.get("downloadId") or ("id:%s" % item.get("id"))


def item_reason(item):
    """Human reason for the action log: the statusMessages, else the status/state."""
    msgs = item_messages(item)
    return (
        "; ".join(msgs)
        or item.get("trackedDownloadStatus")
        or item.get("trackedDownloadState")
        or "warning"
    )


def eligible(candidate_keys, streaks, grace, max_actions):
    """Pure grace + blast-radius decision. MUTATES `streaks` in place.

    - increments the streak of each key that is a candidate THIS cycle;
    - drops keys that are no longer candidates (streak resets to 0);
    - a key is grace-met once its streak >= grace;
    Returns (to_act, held): the sorted grace-met keys to act on, OR — when more than
    max_actions are grace-met at once (a systemic mass-flag) — ([], all grace-met keys),
    so the loop acts on NONE and alerts instead.
    """
    for k in list(streaks):
        if k not in candidate_keys:
            del streaks[k]
    for k in candidate_keys:
        streaks[k] = streaks.get(k, 0) + 1
    met = sorted(k for k, n in streaks.items() if n >= grace)
    if len(met) > max_actions:
        return [], met
    return met, []


def search_command(app_name, item):
    """The /api/v3/command body to re-search the series/movie of a queue item, or None.

    Series/movie granularity (robust for season packs — a queue record's episodeId may not
    represent every episode in a stuck pack; the *arr only grabs genuine gaps).
    """
    if app_name == "Sonarr":
        sid = item.get("seriesId")
        if sid:
            return {"name": "SeriesSearch", "seriesId": sid}
    elif app_name == "Radarr":
        mid = item.get("movieId")
        if mid:
            return {"name": "MoviesSearch", "movieIds": [mid]}
    return None


def format_action(dry_run, app_name, title, reason, streak, grace):
    verb = "WOULD blocklist" if dry_run else "Blocklisted + re-searched"
    return "%s [%s] %s — %s (%d/%d)" % (
        verb, app_name, sanitize(title), sanitize(reason), streak, grace,
    )


# --- I/O ---------------------------------------------------------------------
def log(*args):
    print("[%s]" % time.strftime("%Y-%m-%dT%H:%M:%S"), *args, flush=True)


def _request(url, method="GET", headers=None, data=None):
    """One HTTP call. Always sends a User-Agent (Discord Cloudflare 1010-403s without one)."""
    hdrs = {"User-Agent": "arr-autoblock"}
    if headers:
        hdrs.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=hdrs, data=body, method=method)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 (internal URLs)
        raw = resp.read()
        return json.loads(raw) if raw else None


def post_discord(msg):
    if not DISCORD_WEBHOOK_URL:
        log("WARN: no Discord webhook set; skipping report:", msg)
        return
    try:
        _request(DISCORD_WEBHOOK_URL, method="POST", data={"content": msg})
    except Exception as e:  # best-effort report; never crash the loop
        log("discord post failed (%s):" % msg, e)


def push(ok, msg):
    if not KUMA_PUSH:
        log("WARN: no push token set; skipping push:", msg)
        return
    qs = urllib.parse.urlencode({"status": "up" if ok else "down", "msg": msg})
    try:
        _request("%s/api/push/%s?%s" % (KUMA_URL, KUMA_PUSH, qs))
    except Exception as e:  # best-effort heartbeat; never crash the loop
        log("push failed (%s):" % msg, e)


def touch_heartbeat():
    try:
        with open(HEARTBEAT_FILE, "w") as fh:
            fh.write("%s\n" % time.time())
    except OSError as e:
        log("WARN: heartbeat write failed:", e)


def run_once(streaks):
    """One poll+decide+act cycle. Returns (ok, msg) for the Kuma push. Raises on an
    unreachable *arr / failed mutation, which main() converts to a descriptive `down`."""
    apps = [
        ("Sonarr", SONARR_URL,
         SONARR_URL + "/api/v3/queue?includeUnknownSeriesItems=true&pageSize=250",
         SONARR_API_KEY),
        ("Radarr", RADARR_URL,
         RADARR_URL + "/api/v3/queue?includeUnknownMovieItems=true&pageSize=250",
         RADARR_API_KEY),
    ]
    configured = [a for a in apps if a[3]]
    if not configured:
        return True, "arr auto-block disabled (no API keys)"

    candidates = {}  # item_key -> (app_name, base, key, item)
    for app_name, base, url, key in configured:
        data = _request(url, headers={"X-Api-Key": key})
        for item in data.get("records", []):
            if is_candidate(item, DANGEROUS_MSG_PATTERNS):
                candidates[item_key(item)] = (app_name, base, key, item)

    to_act, held = eligible(
        set(candidates), streaks, GRACE_CYCLES, MAX_ACTIONS_PER_CYCLE
    )
    if held:
        msg = "%d queue items eligible — holding (max %d/cycle), investigate" % (
            len(held), MAX_ACTIONS_PER_CYCLE,
        )
        post_discord(msg)
        return False, msg

    acted = 0
    for k in to_act:
        app_name, base, key, item = candidates[k]
        streak = streaks.get(k, GRACE_CYCLES)
        report = format_action(
            DRY_RUN, app_name, item.get("title") or "?", item_reason(item),
            streak, GRACE_CYCLES,
        )
        log(report)
        post_discord(report)
        if not DRY_RUN:
            _request(
                "%s/api/v3/queue/%s?removeFromClient=true&blocklist=true"
                % (base, item["id"]),
                method="DELETE", headers={"X-Api-Key": key},
            )
            cmd = search_command(app_name, item)
            if cmd:
                _request(base + "/api/v3/command", method="POST",
                         headers={"X-Api-Key": key}, data=cmd)
        acted += 1

    if acted:
        verb = "would act on" if DRY_RUN else "acted on"
        return True, "%s %d queue item(s)" % (verb, acted)
    return True, "queue clean (%s)" % ", ".join(a[0] for a in configured)


def main():
    once = "--once" in sys.argv
    streaks = {}
    log("arr-autoblock starting (interval=%ss, dry_run=%s, once=%s)"
        % (INTERVAL, DRY_RUN, once))
    while True:
        try:
            ok, msg = run_once(streaks)
        except Exception as e:  # an unreachable *arr / failed mutation must not kill the loop
            ok, msg = False, "arr-autoblock error: %s" % e
        log("OK  " if ok else "DOWN", msg)
        push(ok, msg)
        touch_heartbeat()
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest ansible/roles/containers/arr-autoblock/files/test_autoblock.py -q`
Expected: PASS (all ~20 tests green).

- [ ] **Step 5: Register the new test dir in `testpaths`**

In `pyproject.toml`, add the line to the `testpaths` list (after the monitor-bridge entry at line 24):
```toml
  "ansible/roles/containers/arr-autoblock/files",
```

- [ ] **Step 6: Run the full suite to confirm nothing regressed**

Run: `uv run pytest -q`
Expected: PASS, and the new file's tests are now collected via `testpaths`.

- [ ] **Step 7: Lint + format the new Python**

Run: `uv run ruff check ansible/roles/containers/arr-autoblock/files && uv run ruff format --check ansible/roles/containers/arr-autoblock/files`
Expected: no errors. If `ruff format --check` reports diffs, run `uv run ruff format ansible/roles/containers/arr-autoblock/files` and re-run the suite.

- [ ] **Step 8: Commit**

```bash
git add ansible/roles/containers/arr-autoblock/files/autoblock.py \
        ansible/roles/containers/arr-autoblock/files/test_autoblock.py pyproject.toml
git commit -F - <<'EOF'
Add arr-autoblock queue auto-blocklist script + unit tests

The read-only Arr Queue Warnings monitor detects stuck/poisoned *arr downloads
but remediation was manual (the 2026-07-01 poisoned-.exe sat a full day). This
adds the writer's decision core + runtime: classify hard-bad + malware-signature
queue items, gate on a 3-cycle grace and a 5/cycle blast cap, then blocklist+
re-search. Pure functions are unit-tested; ships behind DRY_RUN.
EOF
```

---

### Task 3: Ansible role + host registration

**Files:**
- Create: `ansible/roles/containers/arr-autoblock/tasks/main.yml`
- Create: `ansible/roles/containers/arr-autoblock/meta/deps.yml`
- Create: `ansible/roles/containers/arr-autoblock/templates/docker-compose.yml.j2`
- Modify: `ansible/inventory/host_vars/daniel-server.yml` (add the `containers_list` entry)

**Interfaces:**
- Consumes: `arr_autoblock_push_token` (Task 1); existing `sonarr_api_key`, `radarr_api_key`, `arr_discord_webhook_url`; `autoblock.py` (Task 2).
- Produces: a deployable `arr-autoblock` service with a "Arr Auto-Block" Kuma push monitor.

- [ ] **Step 1: Create the compose template**

Create `ansible/roles/containers/arr-autoblock/templates/docker-compose.yml.j2`:

```jinja
{% from 'autokuma.yml.j2' import labels as kuma with context %}
{% from 'healthcheck.yml.j2' import healthcheck %}
{% from 'networks.yml.j2' import service_networks, external_networks with context %}
{% from 'resources.yml.j2' import resources %}
---

services:
  arr-autoblock:
    image: python:3.14-alpine
    container_name: arr-autoblock
    user: "{{ puid }}:{{ pgid }}"
    restart: unless-stopped
    # Pure stdlib Python over HTTP — no caps, no writable rootfs needed.
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    command: ["python", "/app/autoblock.py"]
    # autoblock.py touches /tmp/heartbeat after every completed cycle; stale mtime means the
    # loop HUNG (death already exits the container) -> unhealthy -> autoheal restart. 1000s
    # ≈ 3×INTERVAL + slack. Kuma push silence on "Arr Auto-Block" remains the alerting path.
    {{ healthcheck("[\"CMD\", \"python3\", \"-c\", \"import os,sys,time; sys.exit(0 if time.time()-os.stat('/tmp/heartbeat').st_mtime<1000 else 1)\"]") }}
    environment:
      - TZ={{ tz }}
      - PYTHONUNBUFFERED=1
      - PYTHONDONTWRITEBYTECODE=1
      - INTERVAL=300
      - KUMA_URL=http://uptime-kuma:3001
      # Ships DRY_RUN=true: report "WOULD blocklist …" to Discord, mutate NOTHING. Flip to
      # false (edit here + redeploy) after observing a week — see the design's Rollout section.
      - DRY_RUN=true
      # Only act on an item that stays a candidate GRACE_CYCLES cycles running (~15 min at 300s),
      # so a transient blip self-clears first. Same hysteresis idiom as monitor-bridge CPU_CONSECUTIVE.
      - GRACE_CYCLES=3
      # Blast-radius valve: if more than this many items are eligible in one cycle, act on NONE and
      # alert — a mass-flag is a systemic cause (disk full, poisoned batch) where auto-nuking is wrong.
      - MAX_ACTIONS_PER_CYCLE=5
      # warning-status items are auto-blocked ONLY when a statusMessage matches one of these
      # (the 2026-07-01 poisoned-.exe class); generic transient warnings stay notify-only.
      - DANGEROUS_MSG_PATTERNS=executable file with extension,potentially dangerous,sample
      # Same URLs/keys as monitor-bridge's read-only check_arr_queue; reached over `media`.
      - SONARR_URL=http://sonarr:8989
      - SONARR_API_KEY={{ sonarr_api_key }}
      - RADARR_URL=http://radarr:7878
      - RADARR_API_KEY={{ radarr_api_key }}
      # Direct Discord POST for the action log (the same channel the *arr post health to). urllib
      # POSTs 403 silently via Cloudflare 1010 without a User-Agent — autoblock.py sets one.
      - ARR_DISCORD_WEBHOOK_URL={{ arr_discord_webhook_url }}
      - KUMA_PUSH_ARR_AUTOBLOCK={{ arr_autoblock_push_token }}
    volumes:
      - ./autoblock.py:/app/autoblock.py:ro
    {{ service_networks() }}
    labels:
      {# Single push monitor: it pushes health every cycle (up/down), so its heartbeat is also
         the dead-man for the container. max_retries=0 like every bridge push monitor. -#}
      {{ kuma('arr-autoblock', monitor_type='push', name='Arr Auto-Block', interval=600, max_retries=0, push_token=arr_autoblock_push_token) }}
    # Resource caps for blast-radius containment; tune from cAdvisor/Grafana.
    {{ resources('0.10', '64M', '0.02', '16M') }}

{{ external_networks() }}
```

- [ ] **Step 2: Create the tasks file**

Create `ansible/roles/containers/arr-autoblock/tasks/main.yml`:

```yaml
---
- name: Create required directories
  tags: [config]
  ansible.builtin.include_role:
    name: common
    tasks_from: setup_dirs.yml
  vars:
    common_dirs_to_create:
      - "{{ container_item.name }}"

- name: Deploy arr-autoblock script
  tags: [config]
  ansible.builtin.copy:
    src: autoblock.py
    dest: "/home/{{ sys_user }}/server/containers/{{ container_item.name }}/autoblock.py"
    mode: "0644"
  register: arr_autoblock_script

- name: Deploy Container
  tags: [deploy]
  ansible.builtin.include_role:
    name: common
    tasks_from: docker_deploy.yml
  vars:
    # autoblock.py is bind-mounted; the loop reads it once at startup, so only a recreate
    # applies a code change. Without this a script-only edit leaves recreate: auto and the
    # new logic silently never deploys. See common/CLAUDE.md.
    common_config_changed: "{{ arr_autoblock_script is changed }}"
```

- [ ] **Step 3: Create the deps file**

Create `ansible/roles/containers/arr-autoblock/meta/deps.yml`:

```yaml
---
# Deploy after the services it reads/writes/pushes to, so they're up first. Sequencing is
# computed by the toposort filter in deploy.yml from these names (matching containers_list
# entries), not by Ansible meta dependencies.
role_deps:
  - sonarr
  - radarr
  - uptime-kuma
```

- [ ] **Step 4: Register in `containers_list`**

In `ansible/inventory/host_vars/daniel-server.yml`, add this entry to `containers_list` (place it right after the `monitor-bridge` entry ending at line 245):

```yaml
  - name: arr-autoblock
    port: false
    use_authelia: false
    networks:
      # media: reach sonarr:8989 + radarr:7878 (queue read + blocklist/search writes).
      # monitoring: push to uptime-kuma:3001 AND egress to the *arr Discord webhook. No web UI.
      - media
      - monitoring
```

- [ ] **Step 5: Verify the compose template renders (the validate-compose hook runs on save, but run it explicitly)**

Run: `uv run python scripts/validate_compose_templates.py`
Expected: exit 0, no malformed-YAML / un-escaped-`$` errors for `arr-autoblock`. (Vanilla Jinja misses Ansible's trim/lstrip_blocks whitespace bugs — this script catches them.)

- [ ] **Step 6: Verify the network invariant + run the ansible-adjacent tests**

Run: `uv run pytest ansible/tests/test_network_invariant.py -q`
Expected: PASS (`media` + `monitoring` are both created by `docker_install`).

- [ ] **Step 7: Lint the role**

Run: `ansible-lint ansible/roles/containers/arr-autoblock`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add ansible/roles/containers/arr-autoblock ansible/inventory/host_vars/daniel-server.yml
git commit -F - <<'EOF'
Wire the arr-autoblock sidecar into the fleet (role + registration)

Least-privilege writer twin of the read-only monitor-bridge: on media
(reach sonarr/radarr) + monitoring (push Kuma, Discord egress) only, no web
UI/Authelia. Ships DRY_RUN=true with a 3-cycle grace and a 5/cycle blast cap.
Deploys after sonarr/radarr/uptime-kuma via deps.yml.
EOF
```

---

### Task 4: Deploy dry-run and verify

**Files:** none (deploy + verification only).

**Interfaces:**
- Consumes: everything from Tasks 1-3.

- [ ] **Step 1: Dry-run the deploy (no container change)**

Run: `uv run ansible-playbook ansible/deploy.yml --tags "arr-autoblock" --check`
Expected: no errors; shows the dir + script copy + container create as changes.

- [ ] **Step 2: Deploy for real**

Run: `uv run ansible-playbook ansible/deploy.yml --tags "arr-autoblock"`
Expected: `ok`/`changed`, no failed tasks.

- [ ] **Step 3: Gate on container health**

Run: `uv run python scripts/probe.py health arr-autoblock`
Expected: exit 0 — container running + healthy (waits out the healthcheck `start_period`).

- [ ] **Step 4: Smoke-test one pass in dry-run**

Run: `docker exec arr-autoblock python /app/autoblock.py --once`
Expected: a `starting (… dry_run=True …)` line, then `OK  queue clean (Sonarr, Radarr)` (or `would act on N …` if any candidate already passed grace — dry-run, so nothing is mutated). No traceback.

- [ ] **Step 5: Confirm the Kuma monitor exists and is green**

Run: `docker logs --tail 20 arr-autoblock`
Expected: recurring `OK  queue clean …` lines and no `push failed` warnings (the "Arr Auto-Block" push monitor is receiving heartbeats). If AutoKuma hasn't provisioned it yet, redeploy `uptime-kuma` so it re-reads labels.

- [ ] **Step 6: Confirm Discord dry-run reporting works only when there's an action**

Note: in a clean queue there is nothing to report — Discord stays silent (correct; only actions/holds post). To exercise the path without waiting for a real bad release, temporarily lower the bar for ONE cycle and observe a `WOULD blocklist …` line, then revert:
```bash
docker exec -e DANGEROUS_MSG_PATTERNS= -e GRACE_CYCLES=1 arr-autoblock python /app/autoblock.py --once
```
Expected: if any queue item is currently in a hard-bad state, a `WOULD blocklist [App] …` line is logged and posted to the *arr Discord channel; still **no** mutation (DRY_RUN unchanged). A fully clean queue logs `queue clean` — that is also a pass (nothing to report). This `docker exec` override is ephemeral and does not persist.

- [ ] **Step 7: Record the rollout state**

The service is now live in **dry-run**. Per the design's Rollout section, watch the Discord reports + the "Arr Auto-Block" Kuma monitor for ~a week, confirm it only targets genuine bad releases, then flip `DRY_RUN=true` → `false` in the compose template and redeploy. No commit here — this step is operational.

---

## Self-Review

**Spec coverage:**
- Predicate (hard-bad + malware-sig, D1) → Task 2 `is_candidate`/`dangerous` + tests. ✅
- Grace (3-cycle, in-process, resets on redeploy) → Task 2 `eligible` + tests; env `GRACE_CYCLES`. ✅
- Blast-radius valve (5/cycle, act-none + alert) → Task 2 `eligible` held-branch + `run_once`; env `MAX_ACTIONS_PER_CYCLE`. ✅
- Action = DELETE blocklist+remove then series/movie re-search (D3) → Task 2 `run_once` + `search_command`. ✅
- Discord action log w/ User-Agent → Task 2 `post_discord`/`_request`; template env. ✅
- Kuma liveness "Arr Auto-Block", max_retries=0 → Task 3 template label. ✅
- Fail-loud on unreachable *arr / failed mutation → Task 2 `main` try/except → `down`. ✅
- Empty keys skip/disable → Task 2 `run_once` `configured`. ✅
- DRY_RUN first, then flip → template default + Task 4 Step 7. ✅
- Least-privilege networks [media, monitoring], no Authelia/port → Task 3 registration. ✅
- New secret + rotation sync → Task 1. ✅
- Unit tests + testpaths → Task 2 Steps 1-6. ✅
- Deploy after sonarr/radarr/uptime-kuma → Task 3 deps.yml. ✅

**Placeholder scan:** none — every step has concrete code/commands.

**Type consistency:** `eligible()` returns `(to_act, held)` and mutates `streaks` — used consistently in `run_once` and tested identically. `search_command` returns the exact dict shape asserted in tests and posted in `run_once`. `is_candidate`/`dangerous`/`item_reason`/`format_action`/`sanitize` signatures match between the script, the tests, and `run_once`'s call sites. Command names `SeriesSearch`/`MoviesSearch` are identical in `search_command` and its tests.
