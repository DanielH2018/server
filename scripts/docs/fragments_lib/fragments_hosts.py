"""Doc fragments for the host, network and per-service pin pages.

Each entry in `FRAGMENTS` builds one fragment the way `gen_doc_fragments.py` builds its own:
a reader that parses the tree statically, and a pure renderer from what it returned to
markdown. `gen_doc_fragments.FRAGMENTS` merges this table, prepends the provenance header and
writes the file, so a builder returns only `(body, sources)`.

The readers that take text (`parse_loadbalancers`, `parse_audit_watches`,
`parse_disabled_mods`) are separate from the builders that read the file, so a test can hand
them a crafted input and see them reject it.
"""

import re
import sys as _sys
from collections.abc import Callable
from pathlib import Path as _Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from lib.ansible_inventory import PI_HOST
from lib.estate import Estate
from lib.repo_paths import ALL_VARS, K8S_ROLES, REPO, ROLES

OPTIMIZE_PI_ROLE = ROLES / "setup" / "optimize_pi"
VALHEIM_ROLE = K8S_ROLES / "valheim"
JELLYFIN_ROLE = K8S_ROLES / "jellyfin"
RENOVATE_AGENT_ROLE = ROLES / "setup" / "renovate_agent"
AUDIT_TASKS = ROLES / "setup" / "initial_setup" / "tasks" / "audit-and-kernel.yml"


def _rel(path: _Path) -> str:
    return path.relative_to(REPO).as_posix()


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
        *("| " + " | ".join(row) + " |" for row in rows),
    ]
    return "\n".join(lines) + "\n"


# --- cluster-addresses ---------------------------------------------------------------------


def render_cni0_gateways(gateways: list[str]) -> str:
    """The nodes' `cni0` bridge gateways as a bullet list.

    Args:
        gateways: the CIDRs `k3s_cni0_gateways` lists, one per k3s node.
    """
    lines = [f"- `{cidr}`" for cidr in gateways]
    return "\n".join(lines) + "\n"


def _cluster_addresses() -> tuple[str, list[str]]:
    gateways = [str(g) for g in Estate().group_vars["k3s_cni0_gateways"]]
    return render_cni0_gateways(gateways), [_rel(ALL_VARS)]


# --- loadbalancer-services -----------------------------------------------------------------

_SERVICE_NAME = re.compile(r"^metadata:\s*\n(?:[ ]{2}.*\n)*?[ ]{2}name:\s*(\S+)", re.M)


def parse_loadbalancers(text: str, role: str) -> list[dict[str, str]]:
    """Every `type: LoadBalancer` Service a role template declares.

    The text is split on `---` into manifest documents. A document counts when it has
    `kind: Service` and an UNCOMMENTED two-space-indented `type: LoadBalancer` under `spec`,
    so a comment that quotes the phrase does not.

    Args:
        text: one Jinja manifest template.
        role: the k8s role the template belongs to.

    Returns:
        One `{role, service, etp}` per match; `etp` is `Cluster` when the spec omits it,
        which is Kubernetes' default.
    """
    found = []
    for doc in re.split(r"^---\s*$", text, flags=re.M):
        if not re.search(r"^kind:\s*Service\s*$", doc, re.M):
            continue
        if not re.search(r"^[ ]{2}type:\s*LoadBalancer\s*$", doc, re.M):
            continue
        name = _SERVICE_NAME.search(doc)
        etp = re.search(r"^[ ]{2}externalTrafficPolicy:\s*(\w+)\s*$", doc, re.M)
        found.append(
            {
                "role": role,
                "service": name.group(1) if name else "?",
                "etp": etp.group(1) if etp else "Cluster",
            }
        )
    return found


def read_loadbalancers(roles_dir: _Path = K8S_ROLES) -> list[dict[str, str]]:
    """Every LoadBalancer Service under `roles_dir/*/templates/`, sorted by role then name."""
    found = []
    for tpl in sorted(roles_dir.glob("*/templates/*.j2")):
        found += parse_loadbalancers(tpl.read_text(), tpl.parent.parent.name)
    return sorted(found, key=lambda s: (s["role"], s["service"]))


def render_loadbalancers(services: list[dict[str, str]]) -> str:
    """The LoadBalancer Services as a table.

    Args:
        services: `read_loadbalancers` output.
    """
    rows = [[f"`{s['role']}`", f"`{s['service']}`", f"`{s['etp']}`"] for s in services]
    return _table(["Role", "Service", "`externalTrafficPolicy`"], rows)


def _loadbalancers() -> tuple[str, list[str]]:
    return render_loadbalancers(read_loadbalancers()), [
        "ansible/roles/k8s/*/templates/*.j2"
    ]


# --- auditd-watches ------------------------------------------------------------------------

_WATCH = re.compile(r"^\s*-w (\S+) -p (\S+) -k (\S+)\s*$", re.M)


def parse_audit_watches(text: str) -> list[tuple[str, str, str]]:
    """The `-w <path> -p <perms> -k <key>` audit rules in `text`, in file order."""
    return [(m[1], m[2], m[3]) for m in _WATCH.finditer(text)]


def render_audit_watches(watches: list[tuple[str, str, str]]) -> str:
    """The audit watches grouped by key, one row per key.

    Args:
        watches: `(path, perms, key)` tuples from `parse_audit_watches`.
    """
    keys: dict[str, dict[str, list[str]]] = {}
    for path, perms, key in watches:
        group = keys.setdefault(key, {"paths": [], "perms": []})
        group["paths"].append(f"`{path}`")
        if perms not in group["perms"]:
            group["perms"].append(perms)
    rows = [
        [f"`{key}`", ", ".join(g["paths"]), ", ".join(f"`{p}`" for p in g["perms"])]
        for key, g in keys.items()
    ]
    return _table(["Key", "Paths watched", "Events (`-p`)"], rows)


def _auditd_watches() -> tuple[str, list[str]]:
    return render_audit_watches(parse_audit_watches(AUDIT_TASKS.read_text())), [
        _rel(AUDIT_TASKS)
    ]


# --- pi-apt-timers, pi-log2ram -------------------------------------------------------------


def render_pi_apt_timers(timers: list[dict[str, str]]) -> str:
    """The pinned apt timers as a table.

    Args:
        timers: `optimize_pi_apt_timers` entries, each `{unit, at}` with `at` in UTC.
    """
    rows = [[f"`{t['unit']}`", f"`{t['at']}`"] for t in timers]
    return _table(["Timer", "Daily at (UTC)"], rows)


def _pi_apt_timers() -> tuple[str, list[str]]:
    d = Estate().role_vars(OPTIMIZE_PI_ROLE, host=PI_HOST)
    return render_pi_apt_timers(d["optimize_pi_apt_timers"]), [
        _rel(OPTIMIZE_PI_ROLE / "defaults" / "main.yml")
    ]


# log2ram.conf key -> the role var that declares it, in the order the page names them.
_LOG2RAM_KEYS = [
    ("SIZE", "optimize_pi_log2ram_size"),
    ("ZL2R", "optimize_pi_log2ram_zram_backed"),
    ("LOG_DISK_SIZE", "optimize_pi_log2ram_disk_size"),
    ("COMP_ALG", "optimize_pi_log2ram_comp_alg"),
]


def render_pi_log2ram(d: dict) -> str:
    """The four `/etc/log2ram.conf` values the optimize_pi role declares.

    Args:
        d: the optimize_pi role's variables, carrying the `optimize_pi_log2ram_*` set.
    """
    rows = [[f"`{key}`", f"`{var}`", f"`{d[var]}`"] for key, var in _LOG2RAM_KEYS]
    return _table(["`log2ram.conf` key", "Role var", "Value"], rows)


def _pi_log2ram() -> tuple[str, list[str]]:
    d = Estate().role_vars(OPTIMIZE_PI_ROLE, host=PI_HOST)
    return render_pi_log2ram(d), [_rel(OPTIMIZE_PI_ROLE / "defaults" / "main.yml")]


# --- valheim-mods --------------------------------------------------------------------------

_DISABLED_MOD = re.compile(
    r"^[ ]*#[ ]+-[ ]name:[ ]*(\S+)\s*\n[ ]*#[ ]+version:[ ]*(\S+)", re.M
)


def parse_disabled_mods(text: str) -> list[tuple[str, str]]:
    """The commented-out `- name:` / `version:` mod entries, as `(name, version)`.

    A disabled mod is a YAML comment, so the YAML parser never sees it. Only an entry whose
    `version:` line directly follows its `name:` line counts, which is the shape every
    entry in the file has.
    """
    return [(m[1], m[2]) for m in _DISABLED_MOD.finditer(text)]


def render_valheim_mods(
    live: list[tuple[str, str]], disabled: list[tuple[str, str]]
) -> str:
    """The Valheim mod set, enabled entries first.

    Args:
        live: `(name, version)` for each entry in `valheim_k8s_mods`.
        disabled: `(name, version)` for each commented-out entry.
    """
    rows = [[f"`{n}`", f"`{v}`", "enabled"] for n, v in live]
    rows += [[f"`{n}`", f"`{v}`", "disabled"] for n, v in disabled]
    return _table(["Mod", "Version", "State"], rows)


def _valheim_mods() -> tuple[str, list[str]]:
    defaults = VALHEIM_ROLE / "defaults" / "main.yml"
    live = [
        (str(m["name"]), str(m["version"]))
        for m in Estate().role_vars(VALHEIM_ROLE)["valheim_k8s_mods"]
    ]
    return render_valheim_mods(live, parse_disabled_mods(defaults.read_text())), [
        _rel(defaults)
    ]


# --- pr-agent-bounds -----------------------------------------------------------------------

# (bound, var, unit suffix) in the order the page explains them.
_RENOVATE_BOUNDS = [
    ("PRs per tick", "renovate_agent_max_prs", ""),
    ("Wall clock", "renovate_agent_run_timeout_s", " s"),
    ("Wall clock, backstop", "renovate_agent_unit_timeout", ""),
    ("Spend", "renovate_agent_budget_usd", " USD"),
]


def render_renovate_bounds(d: dict) -> str:
    """The Renovate agent's four run bounds with their values.

    Args:
        d: the renovate_agent role's variables.
    """
    rows = [
        [bound, f"`{var}`", f"`{d[var]}{unit}`"]
        for bound, var, unit in _RENOVATE_BOUNDS
    ]
    return _table(["Bound", "Var", "Value"], rows)


def _renovate_bounds() -> tuple[str, list[str]]:
    return render_renovate_bounds(Estate().role_vars(RENOVATE_AGENT_ROLE)), [
        _rel(RENOVATE_AGENT_ROLE / "defaults" / "main.yml")
    ]


# --- jellyfin-plugin-pins ------------------------------------------------------------------

_PLUGIN_NAMES = {
    "anisync": "Ani-Sync",
    "introskipper": "Intro Skipper",
    "webhook": "Webhook",
    "mergeversions": "Merge Versions",
    "mediacleaner": "Media Cleaner",
}
_PLUGIN_VERSION = re.compile(r"^jellyfin_k8s_(\w+?)_version$")


def read_jellyfin_plugins(d: dict) -> list[dict[str, str]]:
    """Every plugin the jellyfin role pins, found by its `jellyfin_k8s_<key>_version` var.

    Args:
        d: the jellyfin role's variables.

    Returns:
        One `{name, version, target_abi}` per plugin in file order; `target_abi` is `—`
        when the role declares no `jellyfin_k8s_<key>_target_abi`.
    """
    plugins = []
    for var, version in d.items():
        match = _PLUGIN_VERSION.match(var)
        if not match:
            continue
        key = match[1]
        plugins.append(
            {
                "name": _PLUGIN_NAMES.get(key, key),
                "version": str(version),
                "target_abi": str(d.get(f"jellyfin_k8s_{key}_target_abi", "—")),
            }
        )
    return plugins


def render_jellyfin_plugins(image: str, plugins: list[dict[str, str]]) -> str:
    """The Jellyfin image tag and the plugin pins.

    Args:
        image: `jellyfin_k8s_image`, a tag with an optional `@sha256:` digest.
        plugins: `read_jellyfin_plugins` output.
    """
    tag = image.split("@")[0]
    rows = [
        [
            p["name"],
            f"`{p['version']}`",
            "—" if p["target_abi"] == "—" else f"`{p['target_abi']}`",
        ]
        for p in plugins
    ]
    return f"Jellyfin image (`jellyfin_k8s_image`): `{tag}`.\n\n" + _table(
        ["Plugin", "Pinned version", "`targetAbi`"], rows
    )


def _jellyfin_plugin_pins() -> tuple[str, list[str]]:
    d = Estate().role_vars(JELLYFIN_ROLE)
    return render_jellyfin_plugins(d["jellyfin_k8s_image"], read_jellyfin_plugins(d)), [
        _rel(JELLYFIN_ROLE / "defaults" / "main.yml")
    ]


# name -> () -> (body, sources). The name is the file stem a page includes.
FRAGMENTS: dict[str, Callable[[], tuple[str, list[str]]]] = {
    "cluster-addresses": _cluster_addresses,
    "loadbalancer-services": _loadbalancers,
    "auditd-watches": _auditd_watches,
    "pi-apt-timers": _pi_apt_timers,
    "pi-log2ram": _pi_log2ram,
    "valheim-mods": _valheim_mods,
    "pr-agent-bounds": _renovate_bounds,
    "jellyfin-plugin-pins": _jellyfin_plugin_pins,
}
