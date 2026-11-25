"""Censuses of the role tree's tasks, templates and defaults, one `_row_table.Census` row each.

Each row replaces a test file of its own. A row's `reason` keeps what that file's
docstring said a reader needs before changing the rule. The ship rows read a role's
`templates/` as literal text on purpose: their subject is the basename a task or template
names, which a render does not change.

Run: uv run pytest tests/repo/test_census_rows_roles.py
"""

import re

import pytest
from _config_file_rows import CONFIG_FILE_ROWS
from _defaults_and_inventory_rows import DEFAULTS_AND_INVENTORY_ROWS
from _extra_rollouts_row import EXTRA_ROLLOUTS_RESTART_AS_DECLARED
from _flux_ownership_row import K8S_ROLES_ARE_REACHABLE
from _host_script_row import HOST_SCRIPT_WRITES
from _role_files_rows import DASHBOARDS_IN_EXPORTER_FORM, ROLE_PYTHON_HAS_TESTS
from _row_table import Census, Subject, check, proof_problems, tracked
from _secrets_config_rows import SECRETS_CONFIG_ROWS
from _services_comments_row import NO_DESCRIBED_SERVICES
from _template_macro_rows import TEMPLATE_MACRO_ROWS


def _role_files(*directories: str) -> list[str]:
    """Every tracked file under `k8s/roles/<role>/<directory>/`, at any depth."""
    return [
        rel
        for rel in tracked("k8s/roles/*")
        if len(parts := rel.split("/")) > 4 and parts[3] in directories
    ]


def _code(subject: Subject) -> list[tuple[int, str]]:
    """(line number, text before any `#`) for each line. A comment naming a file is not a ship."""
    return [
        (n, line.split("#", 1)[0])
        for n, line in enumerate(subject.text.splitlines(), 1)
    ]


# ── No role ships a pytest file or a markdown file ──────────────────────────────────────

# Jinja `{# #}` comments name test files freely, so a `.py` on any other line is not a ship.
_PY_SHIP_LINE = re.compile(r"""\b(?:src|dest|loop)\s*:|lookup\(\s*['"]file['"]""")
_PY_BASENAME = re.compile(r"(?<![\w.-])([\w-][\w.-]*\.py)\b")


def _shipped_py(subject: Subject) -> list[tuple[int, str]]:
    """(line, basename) for every `.py` file `subject` names on a line that ships a file."""
    return [
        (n, name)
        for n, code in _code(subject)
        if _PY_SHIP_LINE.search(code)
        for name in _PY_BASENAME.findall(code)
    ]


# Blind spot: a ship naming no basename (`fileglob`, a directory-shaped `src:`,
# `unarchive`/`synchronize`)
# would pass this row and map to no deploy tag in `role_paths.role_for`.
SHIPPED_MD = re.compile(
    r"""(?:src|dest)\s*:\s*['"]?\S*?(?P<a>[\w.-]+\.md)"""
    r"""|lookup\(\s*['"]file['"]\s*,\s*['"]?\S*?(?P<b>[\w.-]+\.md)"""
    r"""|role_file\(\s*['"][^'"]*?(?P<d>[\w.-]+\.md)"""
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


# ── Every LSIO container takes PUID/PGID/TZ from lsio_env() ───────────────────────────

_LSIO_LITERAL = re.compile(r"^\s*- name: (PUID|PGID)\s*$", re.MULTILINE)
_LSIO_CALL = "{{ lsio_env() }}"

# ── A CronJob container takes its resources from job_container_resources() ────────────────

_JOB_RESOURCES_LITERAL = re.compile(
    r"^ {14}resources:\n {16}(limits|requests):", re.MULTILINE
)
_JOB_RESOURCES_CALL = "{{ job_container_resources("

# The `limits:`/`requests:` line separates a container's block from authelia's access-control
# `resources:` list at the same indent.
_POD_RESOURCES_LITERAL = re.compile(
    r"^ {10}resources:\n {12}(limits|requests):", re.MULTILINE
)
_POD_RESOURCES_CALL = "{{ container_resources("

ROWS = (
    Census(
        name="no-role-ships-a-pytest-file",
        reason=(
            "`role_paths.is_test_path` drops test-suite paths before `land.sh` maps a change to "
            "a deploy tag, and `path_kinds.is_test_only_path` before it decides whether the "
            "change moves the bundle, so a change to a role's own `tests/` or to a "
            "`test_*.py`/`conftest.py` gates no workload. That is safe only while a file they "
            "drop is also one no ConfigMap receives. Rename the shipped file, or narrow those "
            "predicates."
        ),
        files=lambda: _role_files("templates"),
        offence=lambda s: [
            f"line {n} ships {name}"
            for n, name in _shipped_py(s)
            if name == "conftest.py" or name.startswith("test_")
        ],
        count=lambda s: len(_shipped_py(s)),
        red=(
            Subject("a.yml", "- ansible.builtin.copy:\n    src: test_deploy.py\n"),
            Subject(
                "b.yml",
                "data: {{ lookup('file', role_file('files/conftest.py')) }}\n",
            ),
            Subject("c.yml", "  loop: [host_lib.py, test_host_lib.py]\n"),
        ),
        green=(
            Subject("a.yml", "- ansible.builtin.copy:\n    src: deploy_logic.py\n"),
            Subject("b.yml", "# src: test_commented.py is not a ship\n"),
            Subject("c.yml", "{# test_render.py pins this block #}\n"),
        ),
        min_matches=32,
        # Two ConfigMap-inlined scripts.
        must_find=frozenset(
            {
                "k8s/roles/configarr/templates/health-configmap.yaml.j2",
                "k8s/roles/dri-device-plugin/templates/admission-reaper-cronjob.yaml.j2",
            }
        ),
    ),
    Census(
        name="no-role-ships-a-markdown-file",
        reason=(
            "`role_paths.role_for` maps every `.md` to no role, so `land.sh` gates no deploy tag "
            "for it, `land_tags.doc_paths` reads it as a change nothing renders, and "
            "`cluster_pins` lets a pin-bump PR carry it as docs. That is safe only while a `.md` "
            "they skip is also one no ConfigMap receives. Rename the shipped file, or give "
            "role_for a carve-out; this scan matches a literal basename and the comment above "
            "SHIPPED_MD names the glob shapes it cannot see."
        ),
        # `defaults/` and `vars/` too: a list of basenames there ships files no task names.
        files=lambda: _role_files("templates", "defaults", "vars"),
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
            Subject(
                "c.yml",
                "example_modules:\n  - name: README.md\n"
                "    src: \"{{ role_file('files/README.md', role='other') }}\"\n",
            ),
        ),
        green=(
            Subject("a.yml", "# src: COMMENTED.md is not a ship\n"),
            Subject(
                "c.yml",
                "example_modules:\n  - name: run.py\n"
                "    src: \"{{ role_file('files/run.py') }}\"\n",
            ),
            Subject("b.yml", "example_config_files:\n  - settings.yaml\n"),
        ),
        min_matches=433,
        # The `*_files` lists the bare-entry alternative exists for.
        must_find=frozenset({"k8s/roles/home-assistant/defaults/main.yml"}),
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
        # The callers loop is the model itself; the homelab baseline falls outside the glob.
        files=lambda: [
            rel
            for rel in tracked(
                "k8s/roles/netpol-baseline/templates/networkpolicy-*.yaml.j2"
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
        min_matches=15,
        must_find=frozenset(
            {
                "k8s/roles/netpol-baseline/templates/networkpolicy-traefik.yaml.j2",
                "k8s/roles/netpol-baseline/templates/networkpolicy-loki.yaml.j2",
            }
        ),
    ),
    Census(
        name="lsio-env-is-the-macro",
        reason=(
            "A linuxserver.io image chowns its config to PUID:PGID and drops to that uid, so "
            "the PUID, PGID and TZ entries travel together and come from `lsio_env()` in "
            "k8s/templates/lsio-env.yml.j2 (#3729). A copy is where one goes missing and "
            "the container runs as the image's default uid. A non-LSIO container that sets "
            "only TZ writes it out; this row never flags TZ alone."
        ),
        files=lambda: [
            rel
            for rel in tracked("k8s/roles/*/templates/*.j2", "k8s/templates/*.j2")
            if rel != "k8s/templates/lsio-env.yml.j2"
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
        min_matches=12,
        must_find=frozenset(
            {
                "k8s/roles/qbittorrent/templates/deployment.yaml.j2",
                "k8s/roles/home-assistant/templates/deployment.yaml.j2",
                "k8s/templates/arr-deployment.yml.j2",
                "k8s/templates/wireguard-sidecar.yml.j2",
            }
        ),
    ),
    Census(
        name="cronjob-container-resources-is-the-macro",
        reason=(
            "A CronJob's containers sit under `jobTemplate`, 14 spaces in, and take their "
            "requests and limits from `job_container_resources()` in "
            "k8s/templates/container-resources.yml.j2 (#3721). The 10-space "
            "`container_resources()` left that depth out, and 12 CronJob containers there had "
            "become hand-written copies. A copy is where a request loses its limit and is "
            "refused at admission against the namespace default."
        ),
        files=lambda: tracked("k8s/roles/*/templates/*.j2"),
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
        min_matches=12,
        must_find=frozenset(
            {
                "k8s/roles/configarr/templates/cronjob.yaml.j2",
                "k8s/roles/pi-peer-backup/templates/cronjob.yaml.j2",
            }
        ),
    ),
    Census(
        name="pod-container-resources-is-the-macro",
        reason=(
            "A Deployment, DaemonSet, StatefulSet or Job container sits 10 spaces in and takes "
            "its requests and limits from `container_resources()` in "
            "k8s/templates/container-resources.yml.j2. The CronJob row above covers only "
            "`jobTemplate` depth, so a hand-written block here passed every check (#4090). A "
            "copy is where a request loses its limit and is refused at admission against the "
            "namespace default."
        ),
        files=lambda: tracked("k8s/roles/*/templates/*.j2"),
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
        min_matches=100,
        must_find=frozenset(
            {
                "k8s/roles/bento-pdf/templates/deployment.yaml.j2",
                "k8s/roles/image-builder/templates/build-job.yaml.j2",
            }
        ),
    ),
    Census(
        name="ha-config-reads-no-persistent-notification-state",
        reason=(
            "Home Assistant has kept persistent notifications out of the state machine since "
            "release 2023.6, so `states.persistent_notification` is always empty and a template "
            "that counts it is always false. The arrival flush counted it and delivered no held "
            "alert for five months (#9598). Keep what a template must read back in an entity, "
            "as `sensor.bedroom_held_alerts` does."
        ),
        files=lambda: tracked("k8s/roles/home-assistant/files/*"),
        offence=lambda s: [
            f"line {n} reads states.persistent_notification"
            for n, code in _code(s)
            if "states.persistent_notification" in code
        ],
        red=(
            Subject(
                "alerts.yaml",
                '      value_template: "{{ states.persistent_notification | list | count > 0 }}"\n',
            ),
        ),
        green=(
            Subject(
                "alerts.yaml",
                "      value_template: \"{{ state_attr('sensor.bedroom_held_alerts', 'held') | count > 0 }}\"\n"
                "    - service: persistent_notification.dismiss\n",
            ),
        ),
        must_find=frozenset({"k8s/roles/home-assistant/files/automations/alerts.yaml"}),
    ),
    HOST_SCRIPT_WRITES,
    K8S_ROLES_ARE_REACHABLE,
    ROLE_PYTHON_HAS_TESTS,
    DASHBOARDS_IN_EXPORTER_FORM,
    EXTRA_ROLLOUTS_RESTART_AS_DECLARED,
    *CONFIG_FILE_ROWS,
    *SECRETS_CONFIG_ROWS,
    *DEFAULTS_AND_INVENTORY_ROWS,
    *TEMPLATE_MACRO_ROWS,
    NO_DESCRIBED_SERVICES,
)

_IDS = [row.name for row in ROWS]


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_holds_on_the_tree(row: Census):
    problems = check(row)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_flags_its_red_subjects_and_passes_its_green_ones(row: Census):
    assert not proof_problems(row)
