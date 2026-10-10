"""Censuses of the role tree's tasks, templates and defaults, one `_row_table.Census` row each.

Each row was a test file of its own until #3430. A row's `reason` keeps what that file's
docstring said a reader needs before changing the rule. The ship rows read a role's
`templates/` as literal text on purpose: their subject is the basename a task or template
names, which a render does not change.

Run: uv run pytest ansible/tests/repo/test_census_rows_roles.py
"""

import re

import pytest
from _helpers import walk_tasks
from _row_table import Census, Subject, check, proof_problems, tracked
from lib import yaml_fast


def _role_files(*directories: str) -> list[str]:
    """Every tracked file under `ansible/roles/<plane>/<role>/<directory>/`, at any depth."""
    return [
        rel
        for rel in tracked("ansible/roles/*")
        if len(parts := rel.split("/")) > 5 and parts[4] in directories
    ]


def _code(subject: Subject) -> list[tuple[int, str]]:
    """(line number, text before any `#`) for each line. A comment naming a file is not a ship."""
    return [
        (n, line.split("#", 1)[0])
        for n, line in enumerate(subject.text.splitlines(), 1)
    ]


# ── No role ships a test file or a markdown file ──────────────────────────────────────

# `src:`/`dest:` in a copy or template task, and the `lookup('file', ...)` a k8s ConfigMap
# inlines a script with.
SHIPPED_PY = re.compile(
    r"""(?:src|dest)\s*:\s*['"]?\S*?(?P<a>[\w.-]+\.py)"""
    r"""|lookup\(\s*['"]file['"]\s*,\s*['"]?\S*?(?P<b>[\w.-]+\.py)"""
)

# The same two shapes for a `.md`, plus a bare YAML list entry: `home-assistant`'s four
# `*_files` lists in `defaults/main.yml` name what its ConfigMap ships that way.
#
# WHAT THIS SCAN DOES NOT SEE: a ship that names no basename. `with_fileglob`/`fileglob` over a
# directory, a directory-shaped `src:` in a copy task, and `unarchive`/`synchronize` of a tree all
# put files on a host without a literal name to match. None of them ships a `.md` today — the two
# globs in the tree take `*.json` and `*.pub`, the three `unarchive` tasks take remote tarballs,
# and no task carries a directory `src:` — so the hole is latent rather than live. A `.md` added
# under one of those globs would pass this row and be skipped by the deployer, which is the one
# failure mode to check when a role starts globbing a directory that holds prose.
SHIPPED_MD = re.compile(
    r"""(?:src|dest)\s*:\s*['"]?\S*?(?P<a>[\w.-]+\.md)"""
    r"""|lookup\(\s*['"]file['"]\s*,\s*['"]?\S*?(?P<b>[\w.-]+\.md)"""
    r"""|^\s*-\s+['"]?(?P<c>[\w.-]+\.md)['"]?\s*$"""
)


def _shipped(pattern: re.Pattern[str], subject: Subject) -> list[tuple[int, str]]:
    """(line, basename) for every file `subject` names as one to put on a host."""
    return [
        (n, name)
        for n, code in _code(subject)
        for match in pattern.finditer(code)
        if (name := next(g for g in match.groups() if g))
    ]


def _is_test_file(name: str) -> bool:
    return name == "conftest.py" or name.startswith("test_")


# ── No role task writes a home-relative path ──────────────────────────────────────────

# The keys that name a path on the target host. `src:` is deliberately absent: for `copy:` and
# `template:` it names a file in the CONTROL node's role, where `~` never reaches sudo.
_PATH_KEYS = ("path", "dest")


def _home_relative_paths(subject: Subject) -> list[str]:
    """Every `~`-relative target a task in this tasks file writes, whichever module it uses."""
    hits = []
    for task in walk_tasks(yaml_fast.safe_load(subject.text) or []):
        for key, value in task.items():
            if not isinstance(value, dict):
                continue
            hits += [
                f"{task.get('name')}: {key}.{path_key}={target}"
                for path_key in _PATH_KEYS
                if isinstance(target := value.get(path_key), str)
                and target.startswith("~")
            ]
    return hits


# ── No caller outside uptime-kuma spells Kuma's port ──────────────────────────────────

# A dial of the Kuma Service, short or fully qualified, and the port that follows it: digits
# for a literal, `{{` for one read from the entry.
_KUMA_DIAL = re.compile(
    r"\buptime-kuma(?:\.\{\{ ?k8s_namespace ?\}\}\.svc\.cluster\.local)?:(\d+|\{\{)"
)


def _kuma_dials(subject: Subject) -> list[tuple[int, str]]:
    """(line, port) for every line of `subject` that dials the Kuma Service."""
    return [
        (n, match.group(1))
        for n, line in enumerate(subject.text.splitlines(), 1)
        for match in _KUMA_DIAL.finditer(line)
    ]


# ── Every LSIO container takes PUID/PGID/TZ from lsio_env() ───────────────────────────

_LSIO_LITERAL = re.compile(r"^\s*- name: (PUID|PGID)\s*$", re.MULTILINE)
_LSIO_CALL = "{{ lsio_env() }}"

# ── A CronJob container takes its resources from job_container_resources() ────────────────

_JOB_RESOURCES_LITERAL = re.compile(
    r"^ {14}resources:\n {16}(limits|requests):", re.MULTILINE
)
_JOB_RESOURCES_CALL = "{{ job_container_resources("

# ── A pod-template container takes its resources from container_resources() ──────────────

# The `limits:`/`requests:` line is what separates a container's block from authelia's
# access-control `resources:` list, which sits at the same 10 spaces.
_POD_RESOURCES_LITERAL = re.compile(
    r"^ {10}resources:\n {12}(limits|requests):", re.MULTILINE
)
_POD_RESOURCES_CALL = "{{ container_resources("


ROWS = (
    Census(
        name="no-role-ships-a-test-file",
        reason=(
            "`deploy_logic._is_test_only_path` skips test-suite paths before every plane prefix, "
            "so a test-only push produces an empty ChangeSet and the deployer fast-forwards it. "
            "That is safe only while a file the deployer ignores is also a file no host "
            "receives. A role shipping its own test suite would get a change to deployed code "
            "fast-forwarded and never deployed — silent, and green from every repo-side check."
        ),
        files=lambda: _role_files("tasks", "templates", "handlers"),
        offence=lambda s: [
            f"line {n} ships {name}"
            for n, name in _shipped(SHIPPED_PY, s)
            if _is_test_file(name)
        ],
        count=lambda s: len(_shipped(SHIPPED_PY, s)),
        red=(
            Subject("a.yml", "- ansible.builtin.copy:\n    src: test_deploy.py\n"),
            Subject("b.yml", "data: {{ lookup('file', 'files/conftest.py') }}\n"),
        ),
        green=(
            Subject("a.yml", "- ansible.builtin.copy:\n    src: deploy_logic.py\n"),
            Subject("b.yml", "# src: test_commented.py is not a ship\n"),
        ),
        # 83 ship lines across 15 files when the row landed (2026-10-03).
        min_matches=60,
        must_find=frozenset(
            {
                "ansible/roles/setup/gitops_deploy/tasks/code.yml",
                "ansible/roles/setup/k3s/tasks/health-crons.yml",
                "ansible/roles/k8s/crowdsec/tasks/main.yml",
            }
        ),
    ),
    Census(
        name="no-role-ships-a-markdown-file",
        reason=(
            "`deploy_logic.is_doc` reads every `.md` as prose no playbook applies, and the tick, "
            "the deploy-plane narrowing and the setup-role narrowing all ask it. That is safe "
            "only while a `.md` the deployer skips is also one no host receives. Rename the "
            "shipped file, or give is_doc back a carve-out; this scan matches a literal basename "
            "and the comment above SHIPPED_MD names the glob shapes it cannot see."
        ),
        # `defaults/` and `vars/` too: a list of basenames there ships files no task names.
        files=lambda: _role_files("tasks", "templates", "handlers", "defaults", "vars"),
        offence=lambda s: [
            f"line {n} ships {name}" for n, name in _shipped(SHIPPED_MD, s)
        ],
        red=(
            Subject(
                "a.yml",
                "- name: Ship the runbook\n  ansible.builtin.copy:\n"
                "    src: RUNBOOK.md\n    dest: /opt/example/RUNBOOK.md\n",
            ),
            Subject("b.yml", "example_config_files:\n  - listed.md\n"),
        ),
        green=(
            Subject("a.yml", "# src: COMMENTED.md is not a ship\n"),
            Subject("b.yml", "example_config_files:\n  - settings.yaml\n"),
        ),
        min_matches=500,
        # The `*_files` lists the bare-entry alternative exists for, and the deployer's own role.
        must_find=frozenset(
            {
                "ansible/roles/k8s/home-assistant/defaults/main.yml",
                "ansible/roles/setup/gitops_deploy/tasks/code.yml",
            }
        ),
    ),
    Census(
        name="role-tasks-write-no-home-relative-path",
        reason=(
            "`path: ~/.ssh` in a task with no `become:` of its own, under a play that becomes "
            "root, writes /root/.ssh: the file module expands `~` as the become user and sudo "
            "passes `-H`. Nothing in the task's text says which directory it writes, so the bug "
            "survives every reading of it. Every playbook here becomes root and none means "
            "/root, so name the directory absolutely (`/home/{{ sys_user }}/.ssh`)."
        ),
        files=lambda: [
            rel
            for rel in tracked("ansible/roles/*/tasks/*.yml")
            if len(parts := rel.split("/")) == 6 and parts[4] == "tasks"
        ],
        offence=_home_relative_paths,
        red=(
            Subject(
                "a.yml",
                "- name: Set SSH directory permissions\n"
                "  ansible.builtin.file:\n    path: ~/.ssh\n",
            ),
            Subject(
                "b.yml",
                "- block:\n    - ansible.builtin.copy:\n        src: ~/x\n        dest: ~/y\n",
            ),
        ),
        green=(
            Subject(
                "a.yml",
                "- name: Harden\n  ansible.builtin.file:\n"
                "    path: /home/{{ sys_user }}/.ssh\n",
            ),
            Subject(
                "b.yml",
                "- ansible.builtin.copy:\n    src: ~/control-side\n    dest: /x\n",
            ),
        ),
        min_matches=100,
        must_find=frozenset({"ansible/roles/setup/initial_setup/tasks/access.yml"}),
    ),
    Census(
        name="bespoke-netpol-says-why-it-is-not-a-callers-fence",
        reason=(
            "A per-workload NetworkPolicy of one TCP port admitting same-namespace `app:` "
            "callers belongs on its containers_list entry as `netpol_from`/`netpol_fences`, "
            "where the callers template renders it. Every hand-written one in netpol-baseline "
            "is of another shape, and its opening `{# DECIDED:` says which (#3701). A new one "
            "either moves onto the entry or opens with that marker, so the next census does "
            "not have to re-derive it."
        ),
        # The callers loop is the model itself. The homelab baseline, networkpolicy.yaml.j2,
        # falls outside the glob; the observability one is inside it and carries a marker.
        files=lambda: [
            rel
            for rel in tracked(
                "ansible/roles/k8s/netpol-baseline/templates/networkpolicy-*.yaml.j2"
            )
            if not rel.endswith("/networkpolicy-callers.yaml.j2")
        ],
        offence=lambda s: (
            []
            if s.text.startswith("{# DECIDED: ")
            else ["opens with no DECIDED: marker"]
        ),
        red=(Subject("networkpolicy-new.yaml.j2", "---\nkind: NetworkPolicy\n"),),
        green=(Subject("networkpolicy-old.yaml.j2", "{# DECIDED: x. #}\n---\n"),),
        # 15 policies when the row landed (2026-10-09).
        min_matches=15,
        must_find=frozenset(
            {
                "ansible/roles/k8s/netpol-baseline/templates/networkpolicy-traefik.yaml.j2",
                "ansible/roles/k8s/netpol-baseline/templates/networkpolicy-loki.yaml.j2",
            }
        ),
    ),
    Census(
        name="kuma-callers-read-the-entry-port",
        reason=(
            "uptime-kuma's listener, Service and scrape target all follow the `port` on its "
            "containers_list entry (#3863). A caller in another role that spells the port "
            "keeps dialling the old one when the entry changes, and its heartbeat tile goes "
            "DOWN at its deadline (#3867). Read it with `containers_list | "
            "entry_port('uptime-kuma')`, and leave a Python default empty so the env var is "
            "the only source."
        ),
        files=lambda: [
            rel
            for rel in _role_files("templates", "defaults", "files")
            if not rel.startswith("ansible/roles/k8s/uptime-kuma/")
        ],
        offence=lambda s: [
            f"line {n} dials uptime-kuma at a literal :{port}"
            for n, port in _kuma_dials(s)
            if port.isdigit()
        ],
        count=lambda s: len(_kuma_dials(s)),
        red=(
            Subject(
                "env-secret.yaml.j2",
                "  KUMA_URL: http://uptime-kuma.{{ k8s_namespace }}.svc.cluster.local:3001\n",
            ),
            Subject(
                "autofix.py", 'KUMA_URL = _env("KUMA_URL", "http://uptime-kuma:3001")\n'
            ),
        ),
        green=(
            Subject(
                "env-secret.yaml.j2",
                "  KUMA_URL: http://uptime-kuma.{{ k8s_namespace }}.svc.cluster.local:"
                "{{ containers_list | entry_port('uptime-kuma') }}\n",
            ),
        ),
        # Five dials across five files when the row landed (2026-10-09). netpol-baseline's
        # probe row names the port in a table, not a dial; the entry-port render test covers it.
        min_matches=5,
        must_find=frozenset(
            {
                "ansible/roles/k8s/monitor-bridge/templates/env-secret.yaml.j2",
                "ansible/roles/k8s/cloudflare-ddns/templates/deployment-direct.yaml.j2",
                "ansible/roles/k8s/pi-peer-backup/templates/secret.yaml.j2",
            }
        ),
    ),
    Census(
        name="lsio-env-is-the-macro",
        reason=(
            "A linuxserver.io image chowns its config to PUID:PGID and drops to that uid, so "
            "the PUID, PGID and TZ entries travel together and come from `lsio_env()` in "
            "ansible/templates/lsio-env.yml.j2 (#3729). A copy is where one goes missing and "
            "the container runs as the image's default uid. A non-LSIO container that sets "
            "only TZ writes it out; this row never flags TZ alone."
        ),
        files=lambda: [
            rel
            for rel in tracked(
                "ansible/roles/k8s/*/templates/*.j2", "ansible/templates/*.j2"
            )
            if rel != "ansible/templates/lsio-env.yml.j2"
        ],
        offence=lambda s: [
            f"`{m.group(1)}` written out instead of lsio_env()"
            for m in _LSIO_LITERAL.finditer(s.text)
        ],
        count=lambda s: s.text.count(_LSIO_CALL),
        red=(
            Subject(
                "deployment.yaml.j2",
                "          env:\n            - name: PUID\n"
                '              value: "{{ puid }}"\n',
            ),
        ),
        green=(
            Subject("deployment.yaml.j2", f"          env:\n{_LSIO_CALL}\n"),
            Subject(
                "tz-only.yaml.j2",
                '          env:\n            - name: TZ\n              value: "{{ tz }}"\n',
            ),
        ),
        # 12 calls across 11 templates when the row landed (2026-10-09); qbittorrent has two.
        min_matches=12,
        must_find=frozenset(
            {
                "ansible/roles/k8s/qbittorrent/templates/deployment.yaml.j2",
                "ansible/roles/k8s/home-assistant/templates/deployment.yaml.j2",
                "ansible/templates/arr-deployment.yml.j2",
            }
        ),
    ),
    Census(
        name="cronjob-container-resources-is-the-macro",
        reason=(
            "A CronJob's containers sit under `jobTemplate`, 14 spaces in, and take their "
            "requests and limits from `job_container_resources()` in "
            "ansible/templates/container-resources.yml.j2 (#3721). The 10-space "
            "`container_resources()` left that depth out, and 12 CronJob containers there had "
            "become hand-written copies. A copy is where a request loses its limit and is "
            "refused at admission against the namespace default."
        ),
        files=lambda: tracked("ansible/roles/k8s/*/templates/*.j2"),
        offence=lambda s: [
            f"`resources.{m.group(1)}` written out instead of job_container_resources()"
            for m in _JOB_RESOURCES_LITERAL.finditer(s.text)
        ],
        count=lambda s: s.text.count(_JOB_RESOURCES_CALL),
        red=(
            Subject(
                "cronjob.yaml.j2",
                "              resources:\n                limits:\n"
                '                  cpu: "100m"\n',
            ),
        ),
        green=(
            Subject(
                "cronjob.yaml.j2",
                f'{_JOB_RESOURCES_CALL}cpu_limit="1", mem_limit="1Gi", '
                'cpu_request="1", mem_request="1Gi") }}\n',
            ),
            # Pod-template depth belongs to container_resources(), not to this row.
            Subject(
                "deployment.yaml.j2", "          resources:\n            limits:\n"
            ),
        ),
        # 12 calls across 4 templates when the row landed (2026-10-09).
        min_matches=12,
        must_find=frozenset(
            {
                "ansible/roles/k8s/configarr/templates/cronjob.yaml.j2",
                "ansible/roles/k8s/pi-peer-backup/templates/cronjob.yaml.j2",
                "ansible/roles/k8s/uptime-kuma/templates/maintenance-sync-cronjob.yaml.j2",
                "ansible/roles/k8s/uptime-kuma/templates/status-page-sync-cronjob.yaml.j2",
            }
        ),
    ),
    Census(
        name="pod-container-resources-is-the-macro",
        reason=(
            "A Deployment, DaemonSet, StatefulSet or Job container sits 10 spaces in and takes "
            "its requests and limits from `container_resources()` in "
            "ansible/templates/container-resources.yml.j2. The CronJob row above covers only "
            "`jobTemplate` depth, so a hand-written block here passed every check (#4090). A "
            "copy is where a request loses its limit and is refused at admission against the "
            "namespace default."
        ),
        files=lambda: tracked("ansible/roles/k8s/*/templates/*.j2"),
        offence=lambda s: [
            f"`resources.{m.group(1)}` written out instead of container_resources()"
            for m in _POD_RESOURCES_LITERAL.finditer(s.text)
        ],
        count=lambda s: s.text.count(_POD_RESOURCES_CALL),
        red=(
            Subject(
                "deployment.yaml.j2",
                "          resources:\n            limits:\n"
                '              cpu: "100m"\n',
            ),
            Subject(
                "deployment.yaml.j2",
                "          resources:\n            requests:\n"
                '              cpu: "100m"\n',
            ),
        ),
        green=(
            Subject(
                "deployment.yaml.j2",
                f'{_POD_RESOURCES_CALL}cpu_limit="1", mem_limit="1Gi", '
                'cpu_request="1", mem_request="1Gi") }}\n',
            ),
            # authelia's access-control rules carry a `resources:` list at the same depth.
            Subject(
                "config-secret.yaml.j2",
                '          resources:\n            - "^/api/push/.*$"\n',
            ),
            # CronJob depth belongs to job_container_resources(), the row above.
            Subject(
                "cronjob.yaml.j2", "              resources:\n                limits:\n"
            ),
        ),
        # 103 calls across 71 templates when the row landed (2026-10-10).
        min_matches=100,
        must_find=frozenset(
            {
                "ansible/roles/k8s/bento-pdf/templates/deployment.yaml.j2",
                "ansible/roles/k8s/image-builder/templates/build-job.yaml.j2",
            }
        ),
    ),
)

_IDS = [row.name for row in ROWS]


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_holds_on_the_tree(row: Census):
    problems = check(row)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_flags_its_red_subjects_and_passes_its_green_ones(row: Census):
    assert not proof_problems(row)
