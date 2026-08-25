# daniel-server: Ubuntu 22.04 → 24.04 LTS Upgrade Runbook (historical)

> ## ⚠️ Do not follow this as instructions
>
> **Status: ✅ COMPLETED 2026-06-05.** Both hosts are on Ubuntu 24.04.4 — daniel-server
> (commit `2837dbc`) and the Pi (aarch64, driven remotely as an Ansible target from
> daniel-server; see Follow-ups). There is no 22.04 host left to upgrade.
>
> This is kept as a **record of what was done**, for the Phase 3 gotcha and the
> config-file decisions. It is written throughout in the imperative — read it as past
> tense. Two things in it are now actively wrong if copied:
>
> - **Step 9 installs ansible via pipx.** That was **replaced by `uv tool` on 2026-06-07**
>   (see `docs/superpowers/specs/2026-06-07-python-uv-test-env-design.md`). pipx appears
>   nowhere in the live `initial_setup` role — it uses
>   `uv tool install --python 3.14 …`. For a new host, follow `ansible/README.md` §3.
> - **Service counts and versions are frozen at mid-2026** and have moved since.

**Goal:** move the host to a single, newer system Python (24.04 ships Python **3.12**)
so we can run **ansible-core 2.21** (latest) without juggling two Python versions.

> This is a real maintenance event: a full `do-release-upgrade` of the box running
> all ~42 services. It reboots and is interactive. Run it yourself in a `tmux`
> session with console access on standby — do **not** run it through an automated/SSH
> agent that the reboot would sever.

---

## Pre-flight snapshot (captured 2026-06-05)

| Check | Value | Why it matters |
|---|---|---|
| Current release | Ubuntu 22.04.5 LTS (jammy), `Prompt=lts` | 24.04 will be offered |
| Machine type | **Bare metal** (`systemd-detect-virt` = none) | No hypervisor snapshot — rollback = LVM snapshot or backup; need **console/IPMI** if networking breaks |
| Root FS | LVM `/dev/mapper/ubuntu--vg-ubuntu--lv`, 299G free | Headroom OK; LVM snapshot possible **if** the VG has free extents |
| /boot | 1.3G free | OK |
| Docker | 29.5.2 from `download.docker.com/linux/ubuntu **jammy**`, **57 containers** | Repo must be re-pointed **jammy→noble** post-upgrade |
| Third-party repos | `docker.list`, `deadsnakes-ubuntu-ppa-jammy.list` (unused) | Both auto-disabled by the upgrade; we remove deadsnakes |
| Kernel | 6.8.0-124-generic (HWE) | Already current |
| ansible-core | 2.17.14 via `pip --user` on Python 3.10 | Max for Py3.10; reinstalled fresh on 3.12 post-upgrade |

**Biggest safety net:** every service is infra-as-code in this repo, so the *host* is
largely reproducible via `ansible-playbook ansible/deploy.yml`. The irreplaceable state
is the **Docker volume data** under `/var/lib/docker` (databases, configs, media
metadata) — it survives an in-place upgrade, but that is what a backup must protect.

---

## Division of labor

- **You** run Phases 0–2 (interactive, sudo, reboots).
- **Claude** handles Phase 3 (the in-repo ansible-core + collections bump) once you
  report you're on 24.04.

---

## Phase 0 — Prep (before the maintenance window)

1. Fully patch 22.04 first (required before a release upgrade):
   ```bash
   sudo apt update && sudo apt full-upgrade && sudo apt autoremove
   ```
   Reboot if the kernel changed.

2. Safety net — do both if possible:
   - Confirm a fresh **Longhorn volume backup to B2** completed (Kopia is retired — see
     `docs/longhorn-backup-tiering.md`). On `daniel-pi`, confirm `pi-peer-backup` ran.
   - LVM rollback point (only if the VG has free space):
     ```bash
     sudo vgs                       # look at the VFree column
     sudo lvcreate -L 20G -s -n root-snap /dev/ubuntu-vg/ubuntu-lv   # if VFree > 0
     ```

3. Run the upgrade inside **tmux** so a dropped SSH won't kill it, and have
   **console/IPMI access** ready (bare metal — no remote console fallback).

## Phase 1 — The upgrade (interactive)

4. Confirm 24.04 is offered, then upgrade:
   ```bash
   sudo do-release-upgrade -c       # confirms a new LTS is available
   tmux new -s upgrade              # then, inside tmux:
   sudo do-release-upgrade
   ```
   - It disables `docker.list` and the deadsnakes PPA — expected.
   - On config-file prompts (for example, `/etc/ssh/sshd_config`), **keep your existing
     version** for anything you recognize. Reboots at the end.

## Phase 2 — Post-upgrade (re-point repos + verify)

5. Confirm the new release and Python:
   ```bash
   lsb_release -a            # → 24.04
   python3 --version         # → 3.12.x
   ```

6. Re-point the Docker apt repo `jammy` → `noble`:
   ```bash
   sudo sed -i 's/ jammy / noble /' /etc/apt/sources.list.d/docker.list
   sudo apt update
   sudo apt install --only-upgrade docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin    # optional, brings Docker current
   ```

7. Remove the now-unused deadsnakes PPA (we want a single system Python):
   ```bash
   sudo rm -f /etc/apt/sources.list.d/deadsnakes-ubuntu-ppa-*.list
   sudo apt update
   ```

8. Verify the fleet is healthy:
   ```bash
   docker ps                 # all ~57 containers back / healthy
   ```
   Spot-check Traefik + a couple of services in the browser.

## Phase 3 — ansible-core + collections  (Claude does this in the repo)

9. Install ansible-core 2.21 cleanly. 24.04 enforces PEP 668 (externally managed
   environment), so `pip install --user` is blocked by default.

   **This step is obsolete — do not copy it.** It used pipx; the homelab moved to **uv**
   on 2026-06-07, and pipx is no longer used anywhere in the `initial_setup` role. The
   role now runs `uv tool install --python 3.14 {ansible-core, ansible-lint, prek}`
   (`ansible/roles/setup/initial_setup/tasks/main.yml`), and `ansible/bring-up.sh`
   installs uv itself. For a fresh host, follow `ansible/README.md` §3 — not this.

   <details><summary>What was actually run in June 2026 (historical)</summary>

   ```bash
   sudo apt install pipx
   pipx install ansible-core==2.21.0
   pipx install ansible-lint            # optional, for local linting
   pipx ensurepath                      # then re-open the shell
   ```
   (The old `~/.local/lib/python3.10/site-packages` ansible-core became orphaned and
   could be ignored or removed.)
   </details>

10. Claude then:
    - bumps `ansible/requirements.yml` to the latest collections (community.general
      **13.x** unlocks once core ≥2.18, etc.),
    - aligns the `prek.toml` ansible-core constraint,
    - re-runs `scripts/validate/validate_compose_templates.py` + `ansible-lint`,
    - commits.

> **Gotcha hit during this upgrade (apply to the Pi too):** bumping community.general
> past 12.0.0 **removes the `community.general.yaml` stdout callback**, which
> `ansible.cfg` referenced via `stdout_callback = yaml` — playbooks then fail to start.
> Fix (already applied in `ansible.cfg`): `stdout_callback = default` + `result_format
> = yaml`. A `--check` deploy of one service is what surfaced it; always run one after
> a major core/collection bump.

---

## Rollback

- **LVM snapshot** (if created in Phase 0): boot from a live USB / rescue and
  `lvconvert --merge /dev/ubuntu-vg/root-snap`, then reboot. (Snapshot must not have
  filled up.)
- **No snapshot:** reinstall 24.04 (or 22.04) clean, then rebuild. On a cluster node that
  means restoring Longhorn volumes from their B2 backups
  (`docs/longhorn-disaster-recovery.md`) and re-running `ansible/k3s-bringup.yml` +
  `ansible/deploy.yml`; on the Pi, `ansible/deploy.yml -e target=daniel-pi`.

## Follow-ups

- ✅ `daniel-pi` is on **Ubuntu 24.04** (aarch64). It no longer runs Ansible locally —
  it is driven remotely over SSH from daniel-server, the sole Ansible controller
  (`-e target=daniel-pi`; see `ansible/inventory/hosts.ini`).
