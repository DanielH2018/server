# Host Python 3.14 Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move every host-run Python script off the Ubuntu 24.04 system 3.12 and onto a pinned 3.14 **invoked through uv**, so the 3.12 syntax floor — and the class of silent failures it hides — stops existing, without introducing a second way of running Python on these hosts.

**Architecture:** uv is already how this repo runs Python everywhere else, and it stays the entry point here. Ansible pins an exact CPython version, installs it with `uv python install`, and exposes **uv itself** at a stable `/usr/local/bin/uv`. The 18 host invocations become `uv run --no-project --python <pin>`. No second interpreter is published at a system path, and `/usr/bin/python3` is never touched.

**Tech Stack:** Ansible, uv, systemd, cron, pytest.

**Context:** `origin/master` at `59686be0` already carries the container-side 3.14 work (PR #236), which deliberately excluded the host interpreter. This plan is that excluded half.

## Global Constraints

- **Never change `/usr/bin/python3` or the `python3` name.** `ansible.cfg:21` sets `interpreter_python = auto_silent`, so Ansible's own modules resolve the target's system interpreter, and apt depends on it. Replacing it is a distro break, not a Python upgrade.
- **Every host invocation goes through `uv run`.** Not a direct interpreter path, not a symlinked `python3.14`. uv is the single entry point for Python in this repo and stays so on the hosts.
- **`--no-project` is mandatory on every invocation.** `uv run` searches upward from the current directory for a project, and systemd and cron have arbitrary working directories. This is not hypothetical: while benchmarking this plan, `uv python find 3.14` run from a repo worktree resolved to that worktree's `.venv/bin/python3` rather than a standalone interpreter. Without `--no-project`, what a host script runs depends on where it was started from.
- **Pin the exact patch version.** The uv-managed 3.14 has already drifted between hosts — `daniel-box` 3.14.6, `daniel-server` 3.14.5 — because nothing pins it. Syntax-level behaviour is exactly the sort that can differ across patch levels.
- **`.python-version` must not be edited by this plan.** `scripts/test_renovate_managers.py::test_python_version_pins_in_lockstep` couples it to both workflows' `python-version:` inputs. The host pin tracks that minor rather than diverging from it.
- Cron and systemd inherit no useful `PATH`; every invocation names uv by absolute path.
- Tests live in a directory already in `pyproject.toml` `testpaths` (`ansible/tests`, `scripts`, `.claude/hooks`).
- Commits are signed. Never `--no-verify` / `--no-gpg-sign`.
- Exit **75** from `./scripts/deploy.sh` means the git-tree lock was busy and nothing deployed — not a failure.

## Measured cost of routing through uv

Benchmarked on `daniel-box`, 10 runs each, starting a do-nothing interpreter:

| Invocation | Per run |
|---|---|
| `/usr/bin/python3` (system 3.12) | 11 ms |
| uv-managed 3.14 interpreter, called directly | 11 ms |
| `uv run --no-project --python 3.14` | **35 ms** |
| `uv run --no-project --no-sync --python 3.14` | 37 ms |

So uv costs about **24 ms per invocation**, roughly 3×. That is irrelevant for the systemd units and crons, which run on timers. It is worth knowing for the two Claude hooks, which sit on an interactive path — but the repo already pays it there: `.claude/hooks/auto-approve-readonly.sh` and `block-protected-edits.sh` route through `uv run` today, and only the two scripts in this plan use a bare `python3`. Moving them to uv makes the hooks consistent rather than adding a new cost.

`--no-sync` measured no faster than plain `--no-project` (37 vs 35 ms is noise at this sample size) and is not used: with `--no-project` there is no project env to reconcile.

## Which host runs what

Established from the inventory before planning, because it changes the shape of the work:

| Group | Host | Evidence |
|---|---|---|
| A — health wrappers | **daniel-box only** | installed by the `configarr` / `janitorr` k8s roles, which run on the control-plane node |
| B — crons | **daniel-box only** | `fake_remux_host: daniel-box` (`group_vars/all.yml:215`); the role is `when: inventory_hostname == fake_remux_host` |
| C — Claude hooks | **both hosts** | daniel-server has the repo, the hooks, uv and `~/.claude/settings.json` — verified by inspection |
| D — renovate-notify | **daniel-box only** | `renovate_notify_host: daniel-box` (`group_vars/all.yml:210`) |
| E — gitops-deploy | **daniel-box only** | `has_gitops: false` on daniel-server (`host_vars/daniel-server.yml:278`) |

So 16 of the 18 scripts run on daniel-box alone. Only the two hook scripts span both machines — which is the only reason daniel-server needs the pinned interpreter at all.

**Deploying to daniel-server is not a `--limit` away.** `hosts.ini` pins both nodes
`ansible_connection=local`, and `initial_setup.yml` uses
`hosts: "{{ target | default(lookup('pipe', 'hostname')) }}"`. Running the playbook on
daniel-box configures daniel-box; `-e target=daniel-server` would still execute locally. Whatever
daniel-server needs must be run **on daniel-server**.

## The 18 host-run scripts

Enumerated by `ansible/tests/test_host_scripts_py312.py`, grouped by invoker. **This grouping is the task order**, chosen so a mistake is contained before it can reach the deploy pipeline.

| Group | Invoked by | Scripts |
|---|---|---|
| A — health wrappers | `configarr-health.sh`, `janitorr-health.sh`, `fake-remux-health.sh` | `configarr_health.py`, `configarr_health_logic.py`, `configarr_status.py`, `janitorr_health.py`, `janitorr_health_logic.py`, `state_push.py` |
| B — crons | `ansible/roles/setup/fake_remux/tasks/main.yml:111,124` | `fake_remux_scan.py`, `fake_remux_logic.py`, `fake_remux_replace.py`, `fake_remux_replace_logic.py` |
| C — Claude hooks | `.claude/hooks/session-health.sh`, `log-instructions.sh` | `session-health.py`, `log-instructions.py` |
| D — systemd | `renovate-notify.service` | `renovate_notify.py`, `notify_logic.py` |
| E — systemd, **the deploy pipeline** | `gitops-deploy.service` | `gitops_deploy.py`, `deploy_logic.py` |
| shared | imported by several of the above | `host_lib.py` |

---

### Task 1: Pin 3.14 and expose uv at a stable path

**Files:**
- Modify: `ansible/inventory/group_vars/all.yml` — the `initial_setup` role has no `defaults/` or `vars/` directory (only `tasks/` and `templates/`), and this repo puts global vars in `group_vars/all.yml`
- Modify: `ansible/roles/setup/initial_setup/tasks/host-basics.yml`
- Test: `ansible/tests/test_host_python_pin.py`

**Interfaces:**
- Produces: `host_python_version` (exact patch, e.g. `3.14.6`); `/usr/local/bin/uv` on **daniel-box**, which every Ansible-managed invocation in Tasks 2–6 names; and the pinned interpreter present on **daniel-server** too, which only the Task 4 hooks need. Later invocations take the form `/usr/local/bin/uv run --no-project --python {{ host_python_version }} <script>`.

- [ ] **Step 1: Write the failing test**

Create `ansible/tests/test_host_python_pin.py`:

```python
"""The host Python pin is exact, and tracks the repo's minor.

Two failure modes, both silent:

  * An UNPINNED `uv python install 3.14` resolves to whatever uv offers per host. That already
    happened: daniel-box carried 3.14.6 and daniel-server 3.14.5 with nothing requesting either.
    Once 18 host scripts run on it, a patch-level difference between the two hosts is a
    difference in what actually executes.
  * A pin that DRIFTS from `.python-version` puts the hosts on one minor while `uv run`, CI and
    the image pins move to the next — reintroducing the split-interpreter problem this migration
    exists to end, in the other direction.

`.python-version` is deliberately not edited by this plan; it is the source of truth this pin
follows. test_python_version_pins_in_lockstep already couples it to both workflows.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_ALL_VARS = _REPO / "ansible/inventory/group_vars/all.yml"
_PYTHON_VERSION = _REPO / ".python-version"


def _pin() -> str:
    return yaml.safe_load(_ALL_VARS.read_text())["host_python_version"]


def test_host_python_version_is_pinned_to_an_exact_patch():
    pin = _pin()
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin), (
        f"host_python_version is {pin!r}; it must be an exact patch version. An unpinned or "
        "minor-only pin lets uv resolve differently per host, which is how daniel-box ended up "
        "on 3.14.6 and daniel-server on 3.14.5."
    )


def test_host_python_pin_tracks_the_repo_minor():
    pin_minor = ".".join(_pin().split(".")[:2])
    repo_minor = ".".join(_PYTHON_VERSION.read_text().strip().split(".")[:2])
    assert pin_minor == repo_minor, (
        f"host_python_version is on {pin_minor} but .python-version is on {repo_minor}. The host "
        "interpreter must track the repo's minor, or host scripts and `uv run` diverge again."
    )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest ansible/tests/test_host_python_pin.py -v -n0`
Expected: FAIL — `KeyError: 'host_python_version'`.

- [ ] **Step 3: Add the pin**

In `ansible/inventory/group_vars/all.yml`, add:

```yaml
# Exact patch, not `3.14`. uv resolves a bare minor to whatever it already has, which is how the
# two hosts ended up on 3.14.6 and 3.14.5 with nothing asking for either. Eighteen host scripts
# run on this, and they must be the same interpreter on both machines. The minor must track
# .python-version — ansible/tests/test_host_python_pin.py enforces both halves.
host_python_version: "3.14.6"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest ansible/tests/test_host_python_pin.py -v -n0`
Expected: PASS, 2 tests. If `test_host_python_pin_tracks_the_repo_minor` fails, `.python-version` has moved — change `host_python_version` to match its minor, never the other way round.

- [ ] **Step 5: Install the interpreter and publish uv**

In `ansible/roles/setup/initial_setup/tasks/host-basics.yml`, immediately after the `Install Python CLI tooling as uv tools` task, add:

```yaml
- name: Install the pinned host Python
  tags: [tooling]
  # become: false to match how uv itself is installed above — uv keeps interpreters under the
  # connecting user's data dir.
  ansible.builtin.command:
    cmd: "{{ initial_setup_user_home.stdout }}/.local/bin/uv python install {{ host_python_version }}"
  register: initial_setup_host_python
  changed_when: "'Installed' in initial_setup_host_python.stdout"
  become: false

- name: Publish uv at a stable path for systemd and cron
  tags: [tooling]
  # Host scripts run through `uv run`, so what needs a stable absolute path is UV, not a Python.
  # systemd and cron inherit no useful PATH and cannot expand ~, and hardcoding the connecting
  # user's home into unit files couples them to a path that is not guaranteed.
  #
  # Deliberately NOT a python3.14 symlink: a second system-level interpreter would be a second
  # way to run Python on these hosts, and the whole point is that there is one way — uv.
  ansible.builtin.file:
    src: "{{ initial_setup_user_home.stdout }}/.local/bin/uv"
    dest: /usr/local/bin/uv
    state: link
    force: true
  become: true
```

- [ ] **Step 6: Deploy on daniel-box and verify**

Run: `uv run ansible-playbook ansible/initial_setup.yml --tags tooling`

`--tags tooling` matches exactly two existing tasks plus the two added above, all in
`host-basics.yml` — it is not a broad run.

Then verify from a directory that is **not** a Python project. The `cd /tmp` is part of the
check, not incidental: `uv run` searches upward for a project from its working directory.

```bash
cd /tmp && /usr/local/bin/uv run --no-project --python 3.14.6 python -V
```

Expected: `Python 3.14.6`. If it reports a different patch, or resolves a `.venv`, stop —
everything after this is built on it.

- [ ] **Step 7: Give daniel-server the pinned interpreter, for the hooks only**

daniel-server runs none of the systemd units or crons, but it does run Claude Code, so the two
hook wrappers from Task 4 execute there. It needs the interpreter; it does **not** need
`/usr/local/bin/uv`, because hooks run in an interactive user session where `~/.local/bin` is
already on `PATH`.

Ansible cannot reach it from here — `hosts.ini` pins it `ansible_connection=local` — so install
the interpreter directly:

```bash
ssh daniel-server '~/.local/bin/uv python install 3.14.6'
ssh daniel-server 'cd /tmp && ~/.local/bin/uv run --no-project --python 3.14.6 python -V'
```

Expected: `Python 3.14.6`, matching daniel-box exactly. This is the step that closes the
3.14.6 / 3.14.5 drift between the two machines.

If a later `uv python uninstall` or a fresh host loses it, `uv run --python 3.14.6` will fetch
the interpreter on demand (uv's python-downloads default is automatic) — but do not rely on
that here: verify the version explicitly, because a silent download of a *different* build is
exactly the drift this pin exists to prevent.

- [ ] **Step 8: Commit**

```bash
git add ansible/inventory/group_vars/all.yml ansible/roles/setup/initial_setup ansible/tests/test_host_python_pin.py
git commit -m "Pin a host Python 3.14 and publish uv for systemd and cron

Ubuntu 24.04 ships only 3.12 and apt depends on it, and ansible.cfg's
interpreter_python = auto_silent means Ansible's own modules resolve that
same interpreter — so /usr/bin/python3 is not ours to move. Host scripts will
instead run through uv, which is already how this repo runs Python
everywhere else.

What gets a stable path is uv, not a second interpreter: publishing a
/usr/local/bin/python3.14 would create a second way to run Python on these
hosts, which is the thing worth avoiding.

The version is pinned to an exact patch because the unpinned installs had
already drifted — daniel-box on 3.14.6, daniel-server on 3.14.5, with nothing
requesting either."
```

---

### Task 2: Repoint group A — the health wrappers

Lowest risk in the set: each wrapper pushes a monitor result, so a failure surfaces as one stale monitor rather than as broken machinery.

**Files:**
- Modify: `ansible/roles/k8s/configarr/templates/configarr-health.sh.j2:33`
- Modify: `ansible/roles/k8s/janitorr/templates/janitorr-health.sh.j2:34`
- Modify: `ansible/roles/setup/fake_remux/templates/fake-remux-health.sh.j2:23`

- [ ] **Step 1: Repoint all three**

In each file, replace `/usr/bin/python3` with:

```
/usr/local/bin/uv run --no-project --python {{ host_python_version }}
```

There is exactly one occurrence per file, at the line noted. `--no-project` is not optional: these wrappers are invoked by cron and by monitoring with no defined working directory, and without it `uv run` may resolve a project env from wherever it happens to start.

- [ ] **Step 2: Verify the templates still render and lint**

Run: `uv run python scripts/validate_shell_templates.py`
Expected: exit 0. This is the `bash -n` + shellcheck gate; a failure here means a typo in the substitution.

- [ ] **Step 3: Deploy**

```bash
./scripts/deploy.sh --tags configarr
./scripts/deploy.sh --tags janitorr
uv run ansible-playbook ansible/initial_setup.yml --tags fake_remux
```

- [ ] **Step 4: Verify each actually ran**

```bash
/usr/local/bin/configarr-health.sh; echo "configarr exit=$?"
/usr/local/bin/janitorr-health.sh; echo "janitorr exit=$?"
```

Expected: exit 0 with output from both. **An exit 0 with EMPTY output is a failure in disguise** — both wrappers test `[[ -z "$OUT" ]]` precisely because a silent interpreter error is the failure mode here.

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/k8s/configarr ansible/roles/k8s/janitorr ansible/roles/setup/fake_remux
git commit -m "Run the health wrappers through uv on the pinned 3.14

First of the host scripts to move, because a wrapper failure surfaces as one
stale monitor rather than as broken deployment machinery."
```

---

### Task 3: Repoint group B — the fake-remux crons

**Files:**
- Modify: `ansible/roles/setup/fake_remux/tasks/main.yml:111` and `:124`

- [ ] **Step 1: Repoint both cron jobs**

Replace `/usr/bin/python3` with `/usr/local/bin/uv run --no-project --python {{ host_python_version }}` in both `ansible.builtin.cron` job strings.

- [ ] **Step 2: Deploy**

Run: `uv run ansible-playbook ansible/initial_setup.yml --tags fake_remux`

- [ ] **Step 3: Verify the deployed cron file took the new invocation**

Both jobs use `cron_file:`, so they land in `/etc/cron.d/autofix-fake-remux` and **never** appear in
`crontab -l` — and that file is not world-readable, with `sudo` denied, so it cannot be read directly
either. Verify by idempotence instead: re-run step 2 and require **`changed=0`** with both
`Schedule the fake-remux …` tasks reporting `ok`. Ansible's `cron` module compares the rendered job
line against the file's contents, so `ok` on a second run is proof the on-disk line matches the
template — a stale file would re-report `changed`.

`--check` does **not** work here: the play aborts earlier at *"sonarr has no ClusterIP in homelab"*,
because check mode skips the `command` that looks the address up. That is the recorded
check-mode-breaks-downstream-consumers class, not a real failure.

- [ ] **Step 4: Run one by hand rather than waiting for the timer**

```bash
cd /tmp && /usr/local/bin/uv run --no-project --python 3.14.6 /opt/autofix-fake-remux/fake_remux_scan.py; echo "exit=$?"
```

Expected: exit 0. (`fake_remux_opt_dir` is `/opt/autofix-fake-remux` — `ansible/roles/setup/fake_remux/defaults/main.yml:56`.) Running it from `/tmp` deliberately mimics cron's working directory.

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/setup/fake_remux
git commit -m "Run the fake-remux crons through uv on the pinned 3.14"
```

---

### Task 4: Repoint group C — the Claude hooks

These have a proven silent-failure history: `session-health.py` once shipped a 3.14-only `except A, B, C:` that SyntaxErrored on the host and went unnoticed, because the wrapper routes stderr to `/dev/null` and exits 0 by design.

**Files:**
- Modify: `.claude/hooks/session-health.sh`
- Modify: `.claude/hooks/log-instructions.sh:11`

- [ ] **Step 1: Repoint both wrappers**

Replace the bare `python3` invocation with `/usr/local/bin/uv run --no-project --python 3.14.6`. These are not Ansible templates, so the version is literal — see the note in Task 7 about keeping it consistent.

Keep the `2>/dev/null` and the `exit 0`: a hook must not be able to block a session, and that is deliberate.

Update each wrapper's comment. They currently justify running "system python3 directly rather than routing through uv" on latency grounds. That reasoning is now superseded — measured, uv costs ~24 ms per invocation, and the sibling hooks (`auto-approve-readonly.sh`, `block-protected-edits.sh`) already pay it. State the real reason: one way to run Python, and 3.12 is no longer a viable interpreter for these scripts.

- [ ] **Step 2: Verify both run**

```bash
.claude/hooks/session-health.sh; echo "session-health exit=$?"
.claude/hooks/log-instructions.sh </dev/null; echo "log-instructions exit=$?"
```

Expected: exit 0 from both. Because these swallow stderr, also confirm the underlying invocation independently — this is the one whose failure the wrapper hides:

```bash
cd /tmp && /usr/local/bin/uv run --no-project --python 3.14.6 "$OLDPWD/.claude/hooks/session-health.py"; echo "direct exit=$?"
```

- [ ] **Step 3: Run the hook test suite**

Run: `uv run pytest .claude/hooks -q`
Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add .claude/hooks
git commit -m "Run the Claude hooks through uv on the pinned 3.14

These are the invocations with a proven silent-failure history:
session-health.py once shipped a 3.14-only except clause that SyntaxErrored
on the host 3.12 and went unnoticed, because the wrapper routes stderr to
/dev/null and exits 0 so a hook can never block a session. That property is
kept; the interpreter that made it dangerous is not.

Their comments claimed a latency reason for bypassing uv. Measured, uv costs
~24ms per invocation and the sibling hooks already pay it, so the comments
now say what is actually true: one way to run Python on these hosts."
```

---

### Task 5: Repoint group D — renovate-notify

**Files:**
- Modify: `ansible/roles/setup/renovate_notify/templates/renovate-notify.service.j2:21`

- [ ] **Step 1: Repoint the unit**

Replace:

```
ExecStart=/usr/bin/python3 /opt/renovate-notify/renovate_notify.py
```

with:

```
ExecStart=/usr/local/bin/uv run --no-project --python {{ host_python_version }} /opt/renovate-notify/renovate_notify.py
```

- [ ] **Step 2: Deploy**

Run: `uv run ansible-playbook ansible/initial_setup.yml --tags renovate_notify`

- [ ] **Step 3: Verify the unit RUNS, not merely that it deployed**

```bash
systemctl cat renovate-notify.service | grep ExecStart
sudo systemctl start renovate-notify.service
systemctl status renovate-notify.service --no-pager | tail -20
```

Expected: `ExecStart` names uv, and the run completes without `203/EXEC` or a `SyntaxError`. If `sudo` is unavailable in your session, ask the user to run the start — do not skip the verification.

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/setup/renovate_notify
git commit -m "Run renovate-notify through uv on the pinned 3.14"
```

---

### Task 6: Repoint group E — gitops-deploy, the deploy pipeline

**This is the one that can break the machine that fixes things.** `gitops-deploy.service` is the pull-based pipeline on a 30-minute timer; if its `ExecStart` cannot exec, it dies every tick, and this repo has history of the deployer being broken behind green monitors. It goes last and is verified by a real run.

**Files:**
- Modify: `ansible/roles/setup/gitops_deploy/templates/gitops-deploy.service.j2:37`

- [ ] **Step 1: Confirm uv and the interpreter are present on daniel-box first**

```bash
cd /tmp && /usr/local/bin/uv run --no-project --python 3.14.6 python -V
```

Expected: `Python 3.14.6`. If this fails, STOP and fix Task 1 — this unit is the deploy
pipeline, and pointing it at an interpreter that is not there is the one mistake that removes
your ability to ship the correction.

daniel-server is deliberately not checked here: `has_gitops: false` there
(`host_vars/daniel-server.yml:278`), so this unit does not exist on that host.

- [ ] **Step 2: Repoint the unit**

The line wraps the interpreter in `flock`. Replace only the interpreter portion:

```
ExecStart=/usr/bin/flock -w 180 /var/lock/server-git-tree.lock /usr/local/bin/uv run --no-project --python {{ host_python_version }} /opt/gitops-deploy/gitops_deploy.py
```

Leave the `flock` invocation, its timeout and the lock path exactly as they are — that lock is what stops the pipeline interleaving with a manual deploy.

- [ ] **Step 3: Deploy**

Run: `uv run ansible-playbook ansible/initial_setup.yml --tags gitops_deploy`

- [ ] **Step 4: Verify by a real run**

```bash
systemctl cat gitops-deploy.service | grep ExecStart
sudo systemctl start gitops-deploy.service
journalctl -u gitops-deploy.service -n 40 --no-pager
```

Expected: `ExecStart` names uv, and the journal shows the deployer running to a normal conclusion — not `203/EXEC`, not a `SyntaxError` traceback, and not a uv project-resolution error.

Then confirm the timer is still armed: `systemctl list-timers gitops-deploy* --no-pager`

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/setup/gitops_deploy
git commit -m "Run gitops-deploy through uv on the pinned 3.14

Last of the eighteen deliberately: this unit is the deploy pipeline, so an
ExecStart it cannot run kills the thing that would otherwise ship the fix,
and a dead deployer has hidden behind green monitors here before."
```

---

### Task 7: Replace the 3.12 guard with its inverse

Only after every group is verified on **both** hosts. The 3.12 guard is currently the single executable check for the `except (A, B)` / PEP 758 trap — `ruff format` strips the parentheses and the result is a SyntaxError below 3.14. Deleting it in the same change that repoints callers would leave a half-migrated host able to hit the bug with nothing watching.

**Files:**
- Delete: `ansible/tests/test_host_scripts_py312.py`
- Create: `ansible/tests/test_host_python_invocations.py`

- [ ] **Step 1: Confirm the migration is actually complete**

Run: `grep -rn "/usr/bin/python3" ansible/roles .claude/hooks --include='*.j2' --include='*.yml' --include='*.sh'`
Expected: no hits outside container contexts (Dockerfiles and compose healthchecks name an interpreter inside their own image and are not in scope).

- [ ] **Step 2: Write the replacement guard**

Create `ansible/tests/test_host_python_invocations.py`:

```python
"""Host Python runs through uv, and only through uv.

This replaces test_host_scripts_py312.py. That guard existed because the hosts ran Ubuntu
24.04's Python 3.12 while the repo was on 3.14, so 3.13+ syntax parsed in CI and SyntaxErrored
on the host — silently, in the case of session-health.py, whose wrapper routes stderr to
/dev/null. Those scripts now run a pinned 3.14 through uv and the floor is gone.

What remains dangerous is the way back. A new unit, cron entry or hook wrapper reaching for
`/usr/bin/python3` puts one script under 3.12 again without restoring any check that notices —
the same silent failure with none of the detection. And an invocation that names an interpreter
directly instead of going through `uv run` reintroduces a second way to run Python on these
hosts, which is what pinning through uv exists to prevent.

Container contexts are deliberately out of scope: a Dockerfile or compose healthcheck names the
interpreter inside its own digest-pinned image, which has nothing to do with the host.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SEARCH_ROOTS = [_REPO / "ansible/roles", _REPO / ".claude/hooks"]
_SUFFIXES = {".j2", ".yml", ".sh"}

# A container's own interpreter, not the host's.
_CONTAINER_CONTEXT = re.compile(r"Dockerfile|docker-compose|deployment\.yaml|healthcheck")


def _candidate_files():
    for root in _SEARCH_ROOTS:
        for path in root.rglob("*"):
            if path.suffix not in _SUFFIXES or not path.is_file():
                continue
            if "archive" in path.parts or _CONTAINER_CONTEXT.search(str(path)):
                continue
            yield path


def _offending_lines(needle: str):
    for path in _candidate_files():
        for n, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or _CONTAINER_CONTEXT.search(line):
                continue
            if needle in line:
                yield f"{path.relative_to(_REPO)}:{n}: {stripped[:100]}"


def test_no_host_invocation_uses_the_system_interpreter():
    offenders = sorted(_offending_lines("/usr/bin/python3"))
    assert not offenders, (
        "these invoke the host's system Python (Ubuntu 24.04's 3.12) instead of running through "
        "uv on the pinned interpreter. That silently puts a script back below the repo's syntax "
        "floor:\n  " + "\n  ".join(offenders)
    )


def test_no_host_invocation_names_an_interpreter_directly():
    """Publishing or calling a python3.14 path would be a second way to run Python here."""
    offenders = sorted(_offending_lines("/usr/local/bin/python3"))
    assert not offenders, (
        "these name an interpreter directly instead of going through `uv run`. uv is the single "
        "entry point for Python on these hosts:\n  " + "\n  ".join(offenders)
    )


def test_uv_invocations_disable_project_discovery():
    """`uv run` searches upward for a project from its working directory, and systemd and cron
    have arbitrary ones. Without --no-project, what a host script runs depends on where it was
    started — observed while planning this: `uv python find 3.14` from a repo worktree resolved
    that worktree's .venv rather than a standalone interpreter."""
    offenders = []
    for path in _candidate_files():
        for n, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "uv run" not in line:
                continue
            if "--no-project" not in line:
                offenders.append(f"{path.relative_to(_REPO)}:{n}: {stripped[:100]}")

    assert not offenders, (
        "`uv run` without --no-project resolves whatever project the working directory happens "
        "to sit in:\n  " + "\n  ".join(sorted(offenders))
    )


def test_hook_wrappers_pin_the_same_version_as_ansible():
    """The hooks are plain shell, not Ansible templates, so they carry the version as a literal
    and cannot interpolate host_python_version. Nothing else couples the two, so a bump to the
    Ansible pin would leave the hooks silently requesting an interpreter the hosts no longer
    install — and these are the wrappers that route stderr to /dev/null."""
    import yaml

    pin = yaml.safe_load((_REPO / "ansible/inventory/group_vars/all.yml").read_text())[
        "host_python_version"
    ]
    offenders = []
    for path in (_REPO / ".claude/hooks").glob("*.sh"):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if "--python" not in line or line.strip().startswith("#"):
                continue
            match = re.search(r"--python\s+(\S+)", line)
            if match and match.group(1) != pin:
                offenders.append(
                    f"{path.relative_to(_REPO)}:{n}: requests {match.group(1)}, pin is {pin}"
                )

    assert not offenders, (
        "hook wrappers must request the same interpreter Ansible installs:\n  "
        + "\n  ".join(sorted(offenders))
    )
```

- [ ] **Step 3: Run it, and prove each guard bites**

Run: `uv run pytest ansible/tests/test_host_python_invocations.py -v -n0`
Expected: PASS, 4 tests.

Then prove the first two catch a regression, reverting after each:

```bash
sed -i 's|/usr/local/bin/uv run --no-project --python {{ host_python_version }} /opt/janitorr-health|/usr/bin/python3 /opt/janitorr-health|' ansible/roles/k8s/janitorr/templates/janitorr-health.sh.j2
uv run pytest ansible/tests/test_host_python_invocations.py -q -n0   # expect FAIL naming janitorr
git checkout ansible/roles/k8s/janitorr/templates/janitorr-health.sh.j2
```

Then the third, on the `--no-project` guard:

```bash
sed -i 's|uv run --no-project --python|uv run --python|' ansible/roles/setup/fake_remux/tasks/main.yml
uv run pytest ansible/tests/test_host_python_invocations.py -q -n0   # expect FAIL naming fake_remux
git checkout ansible/roles/setup/fake_remux/tasks/main.yml
```

Re-run to confirm all four PASS again, and `git status --short` to confirm the tree is clean before continuing.

- [ ] **Step 4: Remove the obsolete guard**

```bash
git rm ansible/tests/test_host_scripts_py312.py
```

- [ ] **Step 5: Full gate**

```bash
uv run pytest -q
prek run --all-files
```
Expected: both pass.

- [ ] **Step 6: Commit**

```bash
git add ansible/tests
git commit -m "Replace the 3.12 syntax floor guard with a uv-invocation guard

The floor is gone: every host script now runs the pinned 3.14 through uv, so
the guard that kept them parseable under Ubuntu's 3.12 has nothing left to
protect.

Replaced rather than deleted, because what remains dangerous is the way back.
Reaching for /usr/bin/python3 puts a script under 3.12 again with no check
that notices — the same silent failure that let session-health.py ship a
SyntaxError behind a /dev/null stderr. Naming an interpreter path directly
reintroduces a second way to run Python here. And a `uv run` without
--no-project resolves whatever project its working directory sits in, which
for systemd and cron is arbitrary."
```

---

## Acceptance

1. On **daniel-box**: `cd /tmp && /usr/local/bin/uv run --no-project --python 3.14.6 python -V` reports the pinned version. The `cd /tmp` is part of the check, not incidental.
2. On **daniel-server**: `cd /tmp && ~/.local/bin/uv run --no-project --python 3.14.6 python -V` reports the *identical* version. Only the hooks need this, but the drift between the two machines is what this closes.
3. Every group A–E invocation has been run once on the host that owns it, and produced its normal output — not merely deployed.
4. `grep -rn "/usr/bin/python3" ansible/roles .claude/hooks` returns nothing outside container contexts.
5. `uv run pytest -q` and `prek run --all-files` pass.

**"The playbook succeeded" is not acceptance.** A systemd unit with an unexecutable `ExecStart` deploys perfectly and fails at every tick.

## Operational log

Actions taken against the hosts during this migration that are **not** represented in any diff,
recorded here because the execution ledger is gitignored and would not survive:

- **2026-08-16 — `uv self update` on daniel-server, 0.11.19 → 0.12.5.** Task 1 step 7 failed with
  `No download found for request: cpython-3.14.6-linux-x86_64-gnu`: that host's uv predated the
  3.14.6 build metadata, while daniel-box's 0.12.1 resolved it fine. Updating uv was the
  narrowest thing that unblocked the authorised step, and is the same character of action (a
  user-scoped tool under `~/.local/bin`). **The hosts' uv versions are now asymmetric —
  daniel-box 0.12.1, daniel-server 0.12.5 — and that is fine:** what this plan requires to match
  is the *interpreter*, and both report 3.14.6. Do not "reconcile" uv toward daniel-box.

- **Known hole, deliberately not closed here.** uv itself is unpinned. The install task at
  `ansible/roles/setup/initial_setup/tasks/host-basics.yml:156` is
  `curl -LsSf https://astral.sh/uv/install.sh | sh` guarded by `creates:`, so it takes whatever
  is latest at first run and then never updates and never pins. Host uv drift is therefore
  guaranteed by design — it is how the interpreters reached 3.14.6 and 3.14.5 in the first
  place. This plan pins the interpreter and leaves the same hole one level up, in the tool that
  installs it. Closing it is a follow-up, not part of this migration.

## Risks

- **System services depend on a user-scoped uv.** `/usr/local/bin/uv` is a symlink into `/home/<sys_user>/.local/bin/`, and the interpreters it manages live under that home too. If the home is unavailable, or someone runs `uv python uninstall`, `gitops-deploy` and `renovate-notify` stop executing. Accepted — uv is already how this repo runs Python, and the alternative is a third-party apt PPA — but it is a real new coupling for the deploy pipeline, not a free win.
- **uv adds ~24 ms per invocation** (35 ms vs 11 ms, measured). Irrelevant on timers; visible only on the two Claude hooks, where the sibling hooks already pay it.
- **`uv run` is working-directory sensitive.** `--no-project` is the mitigation and is guarded by a test, because the failure is silent: it runs *something*, just not the pinned interpreter.
- **Patch drift is the failure most likely to return.** The pin and its test address the repo side; nothing continuously asserts the two hosts agree at runtime. Task 1 step 6 and Task 6 step 1 check it by hand at the two moments it matters.
- **Rollback is cheap until Task 7** — each group is one invocation string, revertible with `git revert` plus a redeploy of that role. After Task 7 the old guard is gone, so a rollback should restore it.
