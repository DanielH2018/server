# autofix-bridge + Disk Autoprune Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the live `arr-autoblock` role/container to `autofix-bridge` (behavior-preserving), add a threshold-gated host-plane disk-autoprune cron + journald cap in that role, and add a `disk_prune` state-file check to monitor-bridge.

**Architecture:** One generic auto-remediation role (`autofix-bridge`) = the writer twin of monitor-bridge, housing (a) the existing zero-privilege HTTP-API sidecar [arr queue remediation, unchanged] and (b) a non-root host cron for docker/journald reclaim. monitor-bridge stays the single reader and reads the disk cron's state file, exactly like the other six host crons.

**Tech Stack:** Ansible, Docker Compose, Bash (host cron), Python 3.14 stdlib (check.py), SOPS, jq, Uptime-Kuma push monitors.

**Full rationale + the two-plane constraint:** `docs/superpowers/specs/2026-07-06-autofix-bridge-disk-autoprune-design.md`. Read it for the "why"; this plan is the "how".

## Global Constraints

- **Behavior-preserving rename:** Part A changes NO remediation logic. The arr sidecar keeps `DRY_RUN=false` and every env/threshold. A green (renamed) test suite is the proof.
- **Preserve the arr Kuma monitor:** keep `kuma('arr-autoblock', name='Arr Auto-Block', …)` and the `arr_autoblock_push_token` secret unchanged (monitor names describe the check, not the container). Do NOT rename these.
- **Non-root cron:** the disk cron runs as `{{ sys_user }}` (in the `docker` group). Only the journald drop-in task uses `become: true`.
- **Conservative prune only:** `docker image prune -f` + `builder prune -f` + `container prune -f`. NEVER `-a`, never named volumes.
- **`except` style:** in check.py, copy `check_verify`'s exact `except ValueError, TypeError:` — **no parentheses** (PEP 758 / py3.14; parenthesizing makes ruff-format reject the commit). Do not "fix" it.
- **CONTROLLER-only tasks:** Task 1 (SOPS secret) and Task 5 (deploy + live `docker rm`) are performed by the controller, not implementer subagents. Implementers do Tasks 2–4: edit + focused tests + REPORT ONLY (no commit — prek auto-stashes other sessions' work; the controller commits explicit paths).
- Tokens are exactly 32 alphanumeric chars (Kuma rejects others).

---

## File Structure

- `ansible/roles/containers/autofix-bridge/` — renamed from `arr-autoblock/`; gains the disk cron.
  - `files/autofix.py`, `files/test_autofix.py` — renamed from `autoblock.py`/`test_autoblock.py` (logic unchanged).
  - `files/autofix-disk-prune.sh.j2` — NEW: the host prune script.
  - `files/60-autofix-journald.conf.j2` — NEW: journald `SystemMaxUse` drop-in.
  - `templates/docker-compose.yml.j2` — renamed service/container/command/volume; arr Kuma label unchanged.
  - `tasks/main.yml` — renamed refs + NEW disk-cron/journald/state-dir tasks.
- `ansible/roles/containers/monitor-bridge/files/check.py` + `test_check.py` — NEW `disk_prune`/`check_disk_prune`.
- `ansible/roles/containers/monitor-bridge/templates/docker-compose.yml.j2` — NEW bind mount + monitor label + env.
- `ansible/roles/containers/monitor-bridge/CLAUDE.md` — 31→32 checks, token list, ordering note.
- `ansible/inventory/host_vars/daniel-server.yml` — `containers_list` entry rename.
- `pyproject.toml`, `prek.toml`, `scripts/validate_compose_templates.py` — tooling refs rename.
- `ansible/vars/secrets.yml`, `ansible/secret_rotation.yml` — NEW disk push token (Task 1).

---

## Task 1: Add the disk-prune push token (CONTROLLER)

**Files:** `ansible/vars/secrets.yml`, `ansible/secret_rotation.yml`

- [ ] **Step 1: Generate + set a 32-alnum token** (never echo the value)

```bash
LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32 | { read -r v; sops set ansible/vars/secrets.yml '["monitor_bridge_disk_prune_push_token"]' "\"$v\""; }
```

- [ ] **Step 2: Verify it decrypts + is 32 alnum, on-disk ciphertext** (do not print the value)

```bash
sops -d ansible/vars/secrets.yml | grep -c '^monitor_bridge_disk_prune_push_token:'   # 1
sops -d ansible/vars/secrets.yml | awk -F': ' '/^monitor_bridge_disk_prune_push_token:/{print length($2), ($2 ~ /^[A-Za-z0-9]+$/ ? "ALNUM_OK":"BAD")}'  # 32 ALNUM_OK
grep '^monitor_bridge_disk_prune_push_token:' ansible/vars/secrets.yml | grep -q 'ENC\[' && echo CIPHERTEXT_OK
```

- [ ] **Step 3: Register for rotation + audit**

```bash
uv run python scripts/secret_rotation.py sync && uv run python scripts/secret_rotation.py audit
```

Expected: registers `monitor_bridge_disk_prune_push_token` tier `auto`; audit clean.

- [ ] **Step 4: Commit** (controller) — `ansible/vars/secrets.yml`, `ansible/secret_rotation.yml`.

---

## Task 2: Rename `arr-autoblock` → `autofix-bridge` (behavior-preserving)

**Files:** the whole role dir + tooling refs (see File Structure). No logic changes.

- [ ] **Step 1: Move the role dir and script/test files (preserve history)**

```bash
git mv ansible/roles/containers/arr-autoblock ansible/roles/containers/autofix-bridge
git mv ansible/roles/containers/autofix-bridge/files/autoblock.py ansible/roles/containers/autofix-bridge/files/autofix.py
git mv ansible/roles/containers/autofix-bridge/files/test_autoblock.py ansible/roles/containers/autofix-bridge/files/test_autofix.py
rm -rf ansible/roles/containers/autofix-bridge/files/__pycache__
```

- [ ] **Step 2: Update `files/autofix.py`** — rename self-references only (no logic):
  - Module docstring line 2: `"""arr-autoblock — …` → `"""autofix-bridge — auto-remediation sidecar (arr-queue module): auto-blocklist stuck/poisoned Sonarr/Radarr queue items.`
  - Discord `User-Agent`: `{"User-Agent": "arr-autoblock"}` → `"autofix-bridge"`.
  - Startup log `"arr-autoblock starting …"` → `"autofix-bridge starting …"`.
  - Error log `"arr-autoblock error: %s"` → `"autofix-bridge error: %s"`.

- [ ] **Step 3: Update `files/test_autofix.py`** — the loader + module alias:
  - The `importlib.util.spec_from_file_location("autoblock", …with_name("autoblock.py"))` → `"autofix"`, `with_name("autofix.py")`.
  - The module var `autoblock = …` / `autoblock.` throughout → `autofix`.

```bash
# from the files/ dir — mechanical, then eyeball the diff
sed -i 's/autoblock/autofix/g' ansible/roles/containers/autofix-bridge/files/test_autofix.py
```

- [ ] **Step 4: Update `templates/docker-compose.yml.j2`**:
  - service key `arr-autoblock:` → `autofix-bridge:`; `container_name: arr-autoblock` → `autofix-bridge`.
  - `command: ["python", "/app/autoblock.py"]` → `/app/autofix.py`; volume `./autoblock.py:/app/autoblock.py:ro` → `./autofix.py:/app/autofix.py:ro`.
  - Comments mentioning `autoblock.py` → `autofix.py`.
  - **UNCHANGED:** the `kuma('arr-autoblock', name='Arr Auto-Block', …)` label, `KUMA_PUSH_ARR_AUTOBLOCK={{ arr_autoblock_push_token }}`, and all env/thresholds.

- [ ] **Step 5: Update `tasks/main.yml`**:
  - Task name `Deploy arr-autoblock script` → `Deploy autofix-bridge script`; `src: autoblock.py` → `autofix.py`; `dest: …/{{ container_item.name }}/autoblock.py` → `autofix.py`.
  - `register: arr_autoblock_script` → `autofix_script`; the `common_config_changed:` reference likewise. Comments `autoblock.py` → `autofix.py`.

- [ ] **Step 6: Update `containers_list`** (`ansible/inventory/host_vars/daniel-server.yml:246`): `- name: arr-autoblock` → `- name: autofix-bridge`. (Networks/port/authelia unchanged.) `meta/deps.yml` stays `sonarr/radarr/uptime-kuma`.

- [ ] **Step 7: Update tooling refs** (three files):
  - `pyproject.toml`: `ansible/roles/containers/arr-autoblock/files` → `…/autofix-bridge/files`.
  - `prek.toml` pytest `files` regex: `(arr-autoblock|monitor-bridge|terraria-stats)` → `(autofix-bridge|monitor-bridge|terraria-stats)`.
  - `scripts/validate_compose_templates.py`: `"arr-autoblock",` in `WATCHTOWER_AUTOUPDATE` → `"autofix-bridge",`.

- [ ] **Step 8: Verify — renamed suite green + compose renders + guard passes**

```bash
uv run pytest ansible/roles/containers/autofix-bridge/files -q          # all arr tests pass under new names
uv run python scripts/validate_compose_templates.py                     # renders autofix-bridge, no un-escaped $, policy OK
uv run pytest scripts -q -k prek_pytest                                 # files-regex covers testpaths guard green
```

Expected: PASS. No `arr-autoblock`/`autoblock` string survives except the preserved `arr_autoblock_push_token` / `KUMA_PUSH_ARR_AUTOBLOCK` / `kuma('arr-autoblock'…)` monitor references. Confirm with:

```bash
grep -rn "arr-autoblock\|autoblock" ansible/ pyproject.toml prek.toml scripts/ | grep -v "arr_autoblock_push_token\|KUMA_PUSH_ARR_AUTOBLOCK\|kuma('arr-autoblock'\|Arr Auto-Block"
```

Expected: no output (empty).

---

## Task 3: Disk-autoprune host cron + journald cap (in the autofix-bridge role)

**Files:** `files/autofix-disk-prune.sh.j2`, `files/60-autofix-journald.conf.j2`, `tasks/main.yml` (append)

- [ ] **Step 1: Create `files/autofix-disk-prune.sh.j2`**

```bash
#!/usr/bin/env bash
# Disk autoprune — managed by Ansible (autofix-bridge role); edits overwritten.
#
# The host-plane twin of the autofix-bridge sidecar's API remediations: when the docker
# filesystem (/) crosses THRESHOLD_PCT, conservatively reclaim dangling images + build cache +
# stopped containers (never -a, never volumes) so the "Root Disk" monitor never needs a manual
# prune as image churn grows. Runs as {{ sys_user }} (in the docker group — no root needed).
#
# Reporting: writes {"ts","ok","msg"} to STATE; monitor-bridge's `disk_prune` check reads it
# (read-only bind mount) and pushes the "Disk Autoprune" Kuma monitor. ok=false ONLY on a prune
# command error — a disk still full of real data after a clean prune is Root Disk's alert, not this.
set -uo pipefail

STATE=/var/lib/autofix-disk-prune/state.json
THRESHOLD_PCT={{ autofix_disk_threshold_pct | default(80) }}
DRY_RUN={{ (autofix_disk_dry_run | default(false)) | string | lower }}

used_pct() { df -P / | awk 'NR==2 { gsub(/%/,"",$5); print $5 }'; }

write_state() { # ok msg   (jq builds valid JSON; a raw msg could break a hand-built string)
  jq -nc --argjson ts "$(date +%s)" --argjson ok "$1" --arg msg "$2" \
    '{ts: $ts, ok: $ok, msg: $msg}' > "$STATE"
  logger -t autofix-disk-prune "$1: $2"
}

BEFORE=$(used_pct)

if [[ "$BEFORE" -lt "$THRESHOLD_PCT" ]]; then
  write_state true "${BEFORE}% < ${THRESHOLD_PCT}%, no prune needed"
  exit 0
fi

if [[ "$DRY_RUN" == "true" ]]; then
  RECLAIMABLE=$(docker system df 2>/dev/null | awk 'NR>1 { print $1": "$NF }' | tr '\n' ' ')
  write_state true "DRY_RUN ${BEFORE}% >= ${THRESHOLD_PCT}%, would prune (reclaimable ${RECLAIMABLE})"
  exit 0
fi

ERR=""
docker image prune -f     >/dev/null 2>&1 || ERR="image prune failed"
[[ -z "$ERR" ]] && { docker builder prune -f   >/dev/null 2>&1 || ERR="builder prune failed"; }
[[ -z "$ERR" ]] && { docker container prune -f  >/dev/null 2>&1 || ERR="container prune failed"; }

AFTER=$(used_pct)

if [[ -n "$ERR" ]]; then
  write_state false "prune error: ${ERR} (${BEFORE}% -> ${AFTER}%)"
  exit 1
fi

write_state true "pruned ${BEFORE}% -> ${AFTER}%"
```

- [ ] **Step 2: Create `files/60-autofix-journald.conf.j2`**

```ini
# Managed by Ansible (autofix-bridge role) — bounds journald growth. Edits overwritten.
[Journal]
SystemMaxUse={{ autofix_journald_max | default('200M') }}
```

- [ ] **Step 3: Append disk tasks to `tasks/main.yml`** (after the existing arr tasks)

```yaml
- name: Create the disk-autoprune monitor state directory
  tags: [config]
  # monitor-bridge bind-mounts this read-only (/autofix-disk); its `disk_prune` check alerts on a
  # failed/stale run. Created sys_user-owned so the non-root cron can write state.json; deploy
  # autofix-bridge before monitor-bridge on a fresh host (else Docker auto-creates it root-owned).
  ansible.builtin.file:
    path: /var/lib/autofix-disk-prune
    state: directory
    owner: "{{ sys_user }}"
    group: "{{ sys_user }}"
    mode: "0755"
  become: true

- name: Deploy the disk-autoprune script
  tags: [config]
  ansible.builtin.template:
    src: autofix-disk-prune.sh.j2
    dest: /usr/local/bin/autofix-disk-prune.sh
    owner: "{{ sys_user }}"
    group: "{{ sys_user }}"
    mode: "0750"
  become: true

- name: Install journald SystemMaxUse drop-in (bounds journald growth)
  tags: [config]
  # A standing host cap (not a cron) — the biggest one-time reclaim. Lives in this role to keep
  # disk hygiene cohesive + iterable via `deploy.yml --tags autofix-bridge`; the only become task.
  ansible.builtin.template:
    src: 60-autofix-journald.conf.j2
    dest: /etc/systemd/journald.conf.d/60-autofix-journald.conf
    owner: root
    group: root
    mode: "0644"
  become: true
  register: autofix_journald_conf

- name: Restart systemd-journald to apply the cap
  tags: [config]
  ansible.builtin.systemd_service:
    name: systemd-journald
    state: restarted
  become: true
  when: autofix_journald_conf is changed

- name: Schedule the disk-autoprune cron
  tags: [cron]
  ansible.builtin.cron:
    name: "autofix disk prune"
    minute: "0"
    user: "{{ sys_user }}"
    job: /usr/local/bin/autofix-disk-prune.sh
    state: present
  become: true
```

- [ ] **Step 4: Verify — lint + render + shellcheck-clean**

```bash
uv run python scripts/validate_compose_templates.py     # (renders shell templates too via the prek path)
prek run validate-rendered-shell-templates --all-files || true   # bash -n + shellcheck on the rendered .sh.j2
ansible-lint ansible/roles/containers/autofix-bridge/tasks/main.yml
```

Expected: renders clean; shellcheck clean; ansible-lint clean. (The rendered script's `{{ }}` become concrete values — confirm no shellcheck SC2086 etc.)

---

## Task 4: monitor-bridge `disk_prune` check

**Files:** `monitor-bridge/files/check.py`, `monitor-bridge/files/test_check.py`, `monitor-bridge/templates/docker-compose.yml.j2`, `monitor-bridge/CLAUDE.md`

- [ ] **Step 1: Add env constants to `check.py`** (near the `VERIFY_STATE` block, ~line 126)

```python
DISK_PRUNE_STATE = _env("DISK_PRUNE_STATE", "/autofix-disk/state.json")
DISK_PRUNE_MAX_AGE_S = float(_env("DISK_PRUNE_MAX_AGE_H", "3")) * 3600
```

- [ ] **Step 2: Add the pure fn + check** (near `check_verify`/`pi_peers`, ~line 1046). Copy `check_verify`'s `except` style verbatim — no parentheses.

```python
def disk_prune(state, age_s, max_age_s):
    """Pure: did the last disk-autoprune run succeed, and recently? (ok, msg).

    Same state-file idiom as verify/pi_peers. ok=false means the last prune command errored; a
    disk still full of real data after a clean prune is Root Disk's alert, not this one.
    """
    if not state.get("ok"):
        return False, "last disk autoprune FAILED: %s" % state.get("msg", "?")
    if age_s > max_age_s:
        return False, "last disk autoprune %.1fh ago (max %.1fh)" % (
            age_s / 3600,
            max_age_s / 3600,
        )
    return True, "disk autoprune ok %.1fh ago: %s" % (age_s / 3600, state.get("msg", ""))


def check_disk_prune():
    try:
        with open(DISK_PRUNE_STATE) as fh:
            state = json.load(fh)
        age_s = time.time() - float(state.get("ts", 0))
    except FileNotFoundError:
        return False, "no disk-autoprune state (never ran?)"
    except ValueError, TypeError:
        return False, "disk-autoprune state unparseable"
    return disk_prune(state, age_s, DISK_PRUNE_MAX_AGE_S)
```

- [ ] **Step 3: Register in `CHECKS`** (add beside the `verify`/`pi_peers` entries, ~line 1580)

```python
    ("disk_prune", _env("KUMA_PUSH_DISK_PRUNE", ""), check_disk_prune),
```

Do NOT add it to `PROM_DEPENDENT` or `LOKI_DEPENDENT` (it's a pure state-file read). If a test asserts a total check COUNT, bump it (31→32).

- [ ] **Step 4: Add tests to `test_check.py`** (mirror the `verify`/`pi_peers` tests)

```python
def test_disk_prune_ok():
    ok, msg = check.disk_prune({"ok": True, "msg": "82% -> 74%"}, 600, 3 * 3600)
    assert ok and "ok" in msg


def test_disk_prune_failed():
    ok, msg = check.disk_prune({"ok": False, "msg": "image prune failed"}, 60, 3 * 3600)
    assert not ok and "FAILED" in msg


def test_disk_prune_stale():
    ok, msg = check.disk_prune({"ok": True, "msg": "x"}, 5 * 3600, 3 * 3600)
    assert not ok and "ago" in msg
```

- [ ] **Step 5: Wire the compose** (`monitor-bridge/templates/docker-compose.yml.j2`)
  - env (near the `VERIFY_STATE` block): `- DISK_PRUNE_STATE=/autofix-disk/state.json`, `- DISK_PRUNE_MAX_AGE_H=3`, `- KUMA_PUSH_DISK_PRUNE={{ monitor_bridge_disk_prune_push_token }}`.
  - volume (with the other `:ro` state mounts, ~line 248): `- /var/lib/autofix-disk-prune:/autofix-disk:ro`.
  - label (with the other push monitors, ~line 297): `{{ kuma('autofix-disk-prune', monitor_type='push', name='Disk Autoprune', interval=600, max_retries=0, push_token=monitor_bridge_disk_prune_push_token) }}`.

- [ ] **Step 6: Update `monitor-bridge/CLAUDE.md`** — "thirty-one checks" → "thirty-two"; add "Disk Autoprune" to the check list (state-file read of `/autofix-disk/state.json` written by the autofix-bridge role's hourly cron; down on a failed prune or >3h staleness); add `disk_prune` to the push-token list; add the one-line fresh-host ordering note (deploy autofix-bridge before monitor-bridge).

- [ ] **Step 7: Verify**

```bash
uv run pytest ansible/roles/containers/monitor-bridge/files -q     # new + existing checks green
uv run python scripts/validate_compose_templates.py                # monitor-bridge renders
```

Expected: PASS.

---

## Task 5: Deploy + cutover + verify (CONTROLLER)

- [ ] **Step 1: Full local gate** — `prek run --all-files` (ansible-lint, validate-compose, shellcheck, full pytest, ruff, gitleaks). Green.
- [ ] **Step 2: Deploy autofix-bridge** — `uv run ansible-playbook ansible/deploy.yml --tags "autofix-bridge"`. Expect the container recreated as `autofix-bridge` + the disk cron/journald installed.
- [ ] **Step 3: Remove the orphaned old container** — `docker rm -f arr-autoblock`.
- [ ] **Step 4: Verify autofix-bridge** — `uv run python scripts/probe.py health autofix-bridge` (running+healthy); `docker exec autofix-bridge python /app/autofix.py --once` → arr logic clean; the "Arr Auto-Block" Kuma monitor still green (preserved).
- [ ] **Step 5: Smoke the disk cron** — `/usr/local/bin/autofix-disk-prune.sh` once → `cat /var/lib/autofix-disk-prune/state.json` shows `{ts,ok:true,msg:"25% < 80% …"}`; confirm `journalctl --disk-usage` now ≈ the cap.
- [ ] **Step 6: Deploy monitor-bridge** — `uv run ansible-playbook ansible/deploy.yml --tags "monitor-bridge"`. Verify the "Disk Autoprune" monitor is provisioned (AutoKuma log `Creating new push`) and pushes green; `docker exec monitor-bridge python /app/check.py --once` shows `disk_prune` up.
- [ ] **Step 7:** Update the SDD ledger + memory. Keep on local master, unpushed (push only when asked).

---

## Final: whole-branch review

Dispatch the whole-branch code review (opus) over the range `c6b12fb1..HEAD` via `scripts/review-package`. Fix Critical/Important with one fix subagent; record Minor in the ledger.
