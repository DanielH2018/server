---
generated_from: scripts/docs/reference/crons.py
generated_at: 2026-11-05 06:17 UTC
generated_sha: f0e5cf0cfe
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/crons.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Scheduled jobs

12 cron entrie(s) installed across the roles.

!!! warning "The state column is a heuristic"
    It is judged from the command text, and nothing in a cron task declares its own blast radius. A job that runs a wrapper script reads as "read the script" rather than being guessed at. Treat it as a pointer, not an authority.

| Job | Schedule | Host | User | Changes state | Defined in |
|---|---|---|---|---|---|
| B2 backup budget listing | `20 7 * * *` | conditional (has_repo_checkout) | `ubuntu` | no (read-only by its command) | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| B2 deletion accounting | `10 7 * * *` | conditional (has_repo_checkout) | `ubuntu` | no (read-only by its command) | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| Full etcd restore drill in a throwaway guest | `20 11 1 * *` | every host in the play | `root` | no (read-only by its command) | `ansible/roles/setup/hypervisor/tasks/etcd_drill.yml` |
| Longhorn backup health | `*/10 * * * *` | every host in the play | `ubuntu` | yes (backup) | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| Release staleness drift check | `*/30 * * * *` | every host in the play | `ubuntu` | read the script | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| daniel-box disk health | `*/10 * * * *` | every host in the play | `ubuntu` | read the script | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| etcd restore drill | `20 10 * * 1` | conditional (has_repo_checkout) | `root` | yes (backup) | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| etcd token drift | `45 2 * * *` | every host in the play | `root` | yes (snapshot) | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| fake-remux health | `*/10 * * * *` | every host in the play | `ubuntu` | read the script | `ansible/roles/setup/fake_remux/tasks/main.yml` |
| fake-remux reconcile | `*/20 * * * *` | every host in the play | `ubuntu` | no (read-only by its command) | `ansible/roles/setup/fake_remux/tasks/main.yml` |
| fake-remux scan | `45 4 * * *` | every host in the play | `ubuntu` | no (read-only by its command) | `ansible/roles/setup/fake_remux/tasks/main.yml` |
| mkv attachment repair | `*/15 * * * *` | every host in the play | `ubuntu` | no (read-only by its command) | `ansible/roles/setup/fake_remux/tasks/main.yml` |

## Schedule format

Five fields: minute, hour, day-of-month, month, day-of-week. A schedule or user shows the value every host resolves when its variables come from role defaults or `group_vars/all.yml` and no host overrides them. A value still showing `{{ ... }}` differs by host or does not evaluate statically, so it only resolves at deploy time, and the template is the honest rendering.
