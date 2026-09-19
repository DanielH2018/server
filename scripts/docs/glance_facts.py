"""The setup-plane and Pi-plane fact readers behind `gen_role_glance.py`, and what every plane shares.

`gen_role_glance.py` owns the k8s readers, the in-place writer and the CLI; its docstring
says which facts each role shape prints and why. This module holds the readers for the two
planes #2096 added — a setup role's applying playbook, crons and timers; a Pi compose role's
image pins, `containers_list` facts, `meta/deps.yml` ordering and `common_config_changed`
wiring — and `image_repository`, which every plane's image line goes through.

STATIC PARSING ONLY: tasks and playbooks through `yaml.safe_load`, templates by line with
a regex. Jinja is printed as written.
"""

import re
import sys as _sys
from pathlib import Path as _Path


# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from pathlib import Path
from typing import Any

from lib import yaml_fast
from lib.render_guard import containers_entries, entry_tags, load_yaml
from lib.repo_paths import ANSIBLE, REPO, ROLES
from reference.crons import schedule_text

# The one host that still runs Docker.
PI_HOST_VARS = REPO / "ansible/inventory/host_vars/daniel-pi.yml"
PI_HOST = "daniel-pi"
SETUP_ROLES = ROLES / "setup"
CONTAINERS_ROLES = ROLES / "containers"
# The playbooks that apply setup roles. `deploy.yml` applies none of them.
SETUP_PLAYBOOKS = ("initial_setup.yml", "bootstrap.yml", "k3s-bringup.yml")
# Include-only: no `tasks/main.yml`, no playbook entry, nothing to print.
SETUP_ROLES_OUT_OF_SUBJECT = frozenset({"common"})
# The `[Timer]` keys that decide when a unit fires, in the order they are printed.
_TIMER_KEYS = (
    "OnCalendar",
    "OnBootSec",
    "OnUnitActiveSec",
    "OnActiveSec",
    "OnStartupSec",
)
_TIMER_KEY_RE = re.compile(r"^\s*(" + "|".join(_TIMER_KEYS) + r")\s*=\s*(.*?)\s*$")
# A compose service key is a two-space-indented bare key; its `image:` sits deeper.
_COMPOSE_SERVICE_RE = re.compile(
    r"^  ([A-Za-z0-9_.-]+|\{\{[^}]*\}\}[A-Za-z0-9_.-]*):\s*$"
)
_COMPOSE_IMAGE_RE = re.compile(r"^\s{4,}image:\s*[\"']?([^\"'#\s]+)")
_NESTING_KEYS = ("block", "rescue", "always")

_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]+$")
_JINJA_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def image_repository(ref: str) -> str:
    """`lscr.io/linuxserver/sonarr:4.0.19@sha256:…` -> `lscr.io/linuxserver/sonarr`.

    A Jinja variable in the ref is kept as `<name>`: `{{ k8s_registry_pull_host }}/n8n:latest`
    is an in-cluster build, and `<k8s_registry_pull_host>/n8n` says so without pretending to
    know the host.
    """
    ref = _DIGEST_RE.sub("", ref.strip())
    ref = _JINJA_VAR_RE.sub(lambda m: f"<{m.group(1)}>", ref)
    head, sep, tail = ref.rpartition(":")
    # A `:` after the last `/` is a tag; before it, a registry port.
    if sep and "/" not in tail:
        return head
    return ref


# --- setup-plane facts -----------------------------------------------------------------------


def setup_role_dirs(setup_roles: Path = SETUP_ROLES) -> list[Path]:
    """Every setup role directory the generator writes a block for, sorted."""
    return sorted(
        d
        for d in setup_roles.iterdir()
        if d.is_dir()
        and not d.name.startswith(".")
        and d.name not in SETUP_ROLES_OUT_OF_SUBJECT
    )


def load_yaml_list(path: Path) -> list:
    """A YAML sequence, or `[]` for a missing, empty or non-sequence file.

    `render_guard.load_yaml` is the mapping twin and returns `{}` for a list-topped file,
    which is what a tasks file and a playbook are.
    """
    if not path.is_file():
        return []
    loaded = yaml_fast.safe_load(path.read_text())
    return loaded if isinstance(loaded, list) else []


def _when_text(when: Any) -> str:
    if isinstance(when, list):
        return " and ".join(str(w) for w in when)
    return str(when) if when is not None else ""


def _walk_tasks(tasks: Any):
    """Every task in a tasks list, descending into `block:`/`rescue:`/`always:` wrappers."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in _NESTING_KEYS:
            yield from _walk_tasks(task.get(key))


def setup_appliers(
    role: str,
    playbooks_dir: Path = ANSIBLE,
    playbooks: tuple[str, ...] = SETUP_PLAYBOOKS,
) -> list[tuple[str, str, str]]:
    """`(playbook, tag, when)` for every place a bring-up playbook applies `role`.

    Reads each play's `roles:` list (a bare name or a `{ role:, tags:, when: }` mapping) and
    every `include_role`/`import_role` task, since `k3s-bringup.yml` joins an agent through
    `include_role: { name: k3s, tasks_from: agent }` under its own tag. A role entry with no
    tags is applied whenever the playbook runs; that is printed as the playbook alone.
    """
    found: list[tuple[str, str, str]] = []
    for playbook in playbooks:
        path = playbooks_dir / playbook
        if not path.is_file():
            continue
        for play in load_yaml_list(path):
            if not isinstance(play, dict):
                continue
            for item in play.get("roles") or []:
                if isinstance(item, str):
                    name, tags, when = item, [], ""
                elif isinstance(item, dict):
                    name = str(item.get("role", ""))
                    tags = item.get("tags") or []
                    when = _when_text(item.get("when"))
                else:
                    continue
                if name != role:
                    continue
                for tag in [tags] if isinstance(tags, str) else tags or [""]:
                    found.append((playbook, str(tag), when))
            for section in ("pre_tasks", "tasks", "post_tasks"):
                for task in _walk_tasks(play.get(section)):
                    spec = None
                    for module in (
                        "ansible.builtin.include_role",
                        "ansible.builtin.import_role",
                        "include_role",
                        "import_role",
                    ):
                        if module in task:
                            spec = task[module]
                            break
                    if not isinstance(spec, dict) or str(spec.get("name")) != role:
                        continue
                    tags = task.get("tags") or []
                    for tag in [tags] if isinstance(tags, str) else tags or [""]:
                        found.append((playbook, str(tag), _when_text(task.get("when"))))
    return sorted(set(found))


def cron_jobs(role_dir: Path) -> list[tuple[str, str]]:
    """`(name, schedule)` for every cron a role's tasks install, in file then task order.

    A task whose `state:` is a Jinja expression (`present if armed else absent`) counts: the
    role CAN install it. Only a literal `state: absent` (a teardown arm) is left out. The
    walk descends into blocks, which `docs/reference/crons.py` does not.
    """
    jobs: list[tuple[str, str]] = []
    for tasks_file in sorted((role_dir / "tasks").glob("*.yml")):
        for task in _walk_tasks(load_yaml_list(tasks_file)):
            spec = task.get("ansible.builtin.cron")
            if not isinstance(spec, dict) or spec.get("state") == "absent":
                continue
            name = str(spec.get("name", task.get("name", "unnamed")))
            jobs.append((name, schedule_text(spec)))
    return jobs


def timer_units(role_dir: Path) -> list[tuple[str, list[str]]]:
    """`(unit, ["OnCalendar=…", …])` for every timer a role installs.

    Two sources: a `templates/*.timer.j2` the role ships, and an import of the shared
    `roles/setup/common/tasks/kuma_check_timer.yml`, whose `kuma_check_name` and
    `kuma_check_on_calendar` vars name the unit and its cadence. The second is read from
    `tasks/` because the template lives in `common`, not in the importing role.
    """
    units: list[tuple[str, list[str]]] = []
    for tmpl in sorted((role_dir / "templates").glob("*.timer.j2")):
        keys: list[str] = []
        for line in tmpl.read_text().splitlines():
            m = _TIMER_KEY_RE.match(line)
            if m:
                keys.append(f"{m.group(1)}={m.group(2)}")
        keys.sort(key=lambda k: _TIMER_KEYS.index(k.split("=", 1)[0]))
        units.append((tmpl.name.removesuffix(".j2"), keys))
    for tasks_file in sorted((role_dir / "tasks").glob("*.yml")):
        for task in _walk_tasks(load_yaml_list(tasks_file)):
            target = task.get("ansible.builtin.import_tasks")
            if not isinstance(target, str) or not target.endswith(
                "common/tasks/kuma_check_timer.yml"
            ):
                continue
            variables = task.get("vars") or {}
            if variables.get("kuma_check_state") == "absent":
                continue  # a teardown arm removes the timer; it installs nothing
            name = variables.get("kuma_check_name", "unnamed")
            cadence = variables.get("kuma_check_on_calendar")
            units.append(
                (
                    f"kuma-check-{name}.timer",
                    [f"OnCalendar={cadence}"] if cadence is not None else [],
                )
            )
    return units


def setup_glance_lines(role_dir: Path, *, playbooks_dir: Path = ANSIBLE) -> list[str]:
    """The block's bullet lines for one setup role, unwrapped."""
    appliers = setup_appliers(role_dir.name, playbooks_dir)
    if appliers:
        cells = []
        for playbook, tag, when in appliers:
            cell = f'`{playbook} --tags "{tag}"`' if tag else f"`{playbook}`"
            if when:
                cell += f" when `{when}`"
            cells.append(cell)
        lines: list[str] = ["- **Applied by:** " + "; ".join(cells)]
    else:
        lines: list[str] = [
            "- **Applied by:** no bring-up playbook entry ("
            + ", ".join(f"`{p}`" for p in SETUP_PLAYBOOKS)
            + " read)"
        ]

    jobs = cron_jobs(role_dir)
    if jobs:
        # One sub-bullet per cron: k3s installs twelve and initial_setup seventeen, and a
        # single wrapped bullet splits their Jinja schedules across lines.
        lines.append(f"- **Crons ({len(jobs)}):**")
        lines.extend(f"  - `{name}` — `{schedule}`" for name, schedule in jobs)
    units = timer_units(role_dir)
    if units:
        cells = ", ".join(
            f"`{unit}` ({', '.join(f'`{k}`' for k in keys) or 'no cadence key'})"
            for unit, keys in units
        )
        label = "Timer" if len(units) == 1 else f"Timers ({len(units)})"
        lines.append(f"- **{label}:** {cells}")
    if not jobs and not units:
        lines.append(
            "- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, "
            "no `templates/*.timer.j2`)"
        )
    return lines


# --- Pi compose-plane facts ------------------------------------------------------------------


def pi_service_entries(pi_host_vars: Path = PI_HOST_VARS) -> list[dict[str, Any]]:
    """Every `containers_list` entry of the Pi (the platform default there is Docker)."""
    return [
        e
        for e in containers_entries(pi_host_vars)
        if e.get("platform", "docker") != "k8s"
    ]


def compose_images(role_dir: Path) -> list[tuple[str, list[str]]]:
    """`(repository, [service, …])` for every image the compose template pins, first seen first.

    The template is Jinja over YAML, so it is read by line: a two-space-indented key opens
    a service, and the next `image:` under it belongs to that service. docker-proxy's three
    services share one image and collapse to one line naming all three.
    """
    tmpl = role_dir / "templates" / "docker-compose.yml.j2"
    if not tmpl.is_file():
        return []
    found: dict[str, list[str]] = {}
    service = "?"
    for line in tmpl.read_text().splitlines():
        m = _COMPOSE_SERVICE_RE.match(line)
        if m:
            service = m.group(1)
            continue
        m = _COMPOSE_IMAGE_RE.match(line)
        if m:
            found.setdefault(image_repository(m.group(1)), []).append(service)
    return list(found.items())


def role_deps(role_dir: Path) -> list[str]:
    """The `role_deps` a `meta/deps.yml` declares, or none."""
    data = (
        load_yaml(role_dir / "meta" / "deps.yml")
        if (role_dir / "meta" / "deps.yml").is_file()
        else None
    )
    deps = (data or {}).get("role_deps") if isinstance(data, dict) else None
    return [str(d) for d in deps or []]


def config_change_wiring(role_dir: Path) -> str | None:
    """The `common_config_changed` expression the role hands `docker_deploy`, or None.

    `roles/containers/common/CLAUDE.md`: a role that bind-mounts a templated config file
    registers those tasks and passes the flag as an `include_role` var, which is what turns
    a config-only edit into a container recreate.
    """
    for tasks_file in sorted((role_dir / "tasks").glob("*.yml")):
        for task in _walk_tasks(load_yaml_list(tasks_file)):
            vars_ = task.get("vars")
            if isinstance(vars_, dict) and "common_config_changed" in vars_:
                return str(vars_["common_config_changed"])
    return None


def pi_glance_lines(entry: dict[str, Any], role_dir: Path) -> list[str]:
    """The block's bullet lines for one Pi compose role, unwrapped."""
    tags = ",".join(entry_tags(entry))
    lines: list[str] = [f'- **Deploy tag:** `--tags "{tags}" -e target={PI_HOST}`']

    images = compose_images(role_dir)
    if images:
        label = "Image" if len(images) == 1 else "Images"
        cells = ", ".join(
            f"`{repo}` (" + ", ".join(f"`{svc}`" for svc in services) + ")"
            for repo, services in images
        )
        lines.append(f"- **{label}:** {cells}")
    else:
        lines.append("- **Image:** none found in `templates/docker-compose.yml.j2`")

    facts = []
    if entry.get("port") is not None:
        facts.append(f"port `{entry['port']}`")
    if entry.get("udp_port") is not None:
        facts.append(f"UDP port `{entry['udp_port']}`")
    networks = entry.get("networks") or []
    facts.append(
        "networks " + ", ".join(f"`{n}`" for n in networks)
        if networks
        else "no networks"
    )
    facts.append("Authelia" if entry.get("use_authelia") else "no Authelia")
    lines.append(f"- **Entry:** `host_vars/{PI_HOST}.yml` → " + ", ".join(facts))

    deps = role_deps(role_dir)
    if deps:
        lines.append(
            "- **Depends on:** "
            + ", ".join(f"`{d}`" for d in deps)
            + " (`meta/deps.yml`)"
        )
    else:
        lines.append("- **Depends on:** nothing (no `meta/deps.yml`)")

    wiring = config_change_wiring(role_dir)
    if wiring:
        lines.append(
            f"- **Config-change wiring:** `common_config_changed: {wiring}` — a config edit "
            "recreates the container"
        )
    else:
        lines.append(
            "- **Config-change wiring:** none — the compose file is the only config, and a "
            "compose change recreates on its own"
        )
    return lines
