#!/usr/bin/env python3
"""Which roles exist under ``ansible/roles/k8s/``, and which of them each reader skips.

``role_dirs`` is the one census of a role tree for code under ``scripts/``: every reader
that lists the roles calls it and passes the names it skips as ``exclude``. The two
exemption sets and ``is_manifest_template`` are the half other guards ask about
(``scripts/validate/tests/test_skip_roles_classes_hold.py``,
``scripts/diagnostics/probe_lib/health.py``), which is why they are their own module rather
than private to the validator.

``K8S_ROLES`` comes from ``lib.repo_paths`` rather than being derived a second time here.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import functools
import re
from collections.abc import Collection
from pathlib import Path

import yaml

from lib import yaml_fast
from lib.ansible_inventory import K8S_HOST_VARS
from lib.jinja_comments import strip_jinja_comments
from lib.repo_paths import FILTER_PLUGINS, K8S_ROLES, REPO, SHARED_TPL

__all__ = [
    "CALLER_RENDERED_ROLES",
    "CLAIM_TEMPLATE",
    "K8S_ROLES",
    "NO_MANIFEST_ROLES",
    "SHARED_MANIFEST_DEFAULTS",
    "SKIP_ROLES",
    "claim_contexts",
    "is_leftover_dir",
    "is_manifest_template",
    "manifest_template",
    "shared_default_templates",
    "k8s_entries",
    "misplaced_template_lookups",
    "non_manifest_documents",
    "role_callers",
    "resolved_manifest_files",
    "role_dirs",
]


def is_leftover_dir(path: Path | str) -> bool:
    """Whether `path` is a retired role's debris: no file left but compiled Python.

    The deployer's fast-forward removes a retired role's tracked files, but a gitignored
    `__pycache__/` left by a pytest run keeps `roles/<plane>/<role>/` on disk. A walker that
    read that shell as a role would render it, census it or credit it.

    The rule itself lives in `ansible/filter_plugins/k8s_autodeploy.py`, because it gates the
    deployer's config write and a filter plugin cannot import from `scripts/` at deploy time.
    Its docstring has why an empty directory is NOT debris. The import is deferred because
    that module imports `ansible.errors`, about 35 ms that a reader which never walks a role
    tree should not pay.
    """
    if str(FILTER_PLUGINS) not in _sys.path:
        _sys.path.insert(0, str(FILTER_PLUGINS))
    from k8s_autodeploy import is_leftover_dir as rule

    return rule(str(path))


def role_dirs(roles_dir: Path = K8S_ROLES, exclude: Collection[str] = ()) -> list[Path]:
    """Every real role directory under `roles_dir`, sorted, minus the names in `exclude`.

    A retired role's debris is always dropped (`is_leftover_dir`). `exclude` is how a reader
    states the roles it skips on purpose: the validator passes `SKIP_ROLES`, the auto-deploy
    count passes the filter plugin's `SHARED_ROLES`. `roles_dir` may be any plane's tree, so
    the setup-role readers walk through here too.
    """
    return sorted(
        d
        for d in roles_dir.iterdir()
        if d.is_dir() and d.name not in exclude and not is_leftover_dir(d)
    )


# Helper roles, included by service roles rather than deployed on their own. They have no
# containers_list entry because they are not services, so the platform check below would always
# fail for them.
#
# cronjob-gate creates a one-off Job from the CALLER's CronJob with `kubectl
# create job --from=cronjob/<name>`, so the pod spec it runs is the caller's rendered manifest.
# volume-snapshot applies one Longhorn Snapshot CR per claim, built inline and piped to `kubectl
# apply -f -` — per-deploy state rather than part of a service's manifest set, and
# `ansible/tests/longhorn/test_volume_snapshot.py` is what checks its shape instead.
#
# Split into the two classes the comment above already distinguishes, because they rot in
# opposite directions and a single set cannot be checked either way. NO_MANIFEST_ROLES makes a
# claim about the tree that `test_skip_roles_classes_hold.py` asserts, so a role that grows its
# first manifest template stops being silently exempt. CALLER_RENDERED_ROLES makes the opposite
# claim, asserted the opposite way, so an entry that stops carrying templates is caught as a
# stale exemption rather than left as a name nobody can justify.
NO_MANIFEST_ROLES = {
    "manifests",
    "cronjob-gate",
    "volume-snapshot",
    "longhorn-api",  # resolves a fact only, same as cronjob-gate/volume-snapshot
    "volume-revert",  # reverts a volume through kubectl and the Longhorn API
    # declares sonarr's/radarr's Discord Connect notification over the app's own API — a row in
    # the app's database, which no manifest can express
    "arr-notification",
}

# These DO carry manifest templates; they are exempt for a different reason. Their templates
# render only with vars a CALLING role passes on its `include_role` task — which image and which
# Dockerfile — and this validator reads role defaults and inventory, not task-level
# `vars:` overrides. Rendering them standalone produces STUB-filled manifests that prove nothing.
#
# That exemption is a coverage gap, not a clean bill. The build job is closed for the test
# suite: `ansible/tests/_k8s_render.py:rendered_build_job_text` renders it against a fixture of
# a caller's vars, and the security-context, token-mount and namespace-defaults guards parse
# that render. One manifest (image-builder's context-configmap) is still parsed as YAML
# nowhere; this names that gap where someone will look for it.
CALLER_RENDERED_ROLES = {
    "image-builder",
}

SKIP_ROLES = NO_MANIFEST_ROLES | CALLER_RENDERED_ROLES


def is_manifest_template(path: Path) -> bool:
    """True if this `.j2` under a role's templates/ is a manifest this script should parse.

    A role may also ship a helper script (observability's telemetry-health.sh.j2) or a Dockerfile
    for image-builder (homelab-mcp). Shell is rendered and linted by validate/shell_templates.py;
    a Dockerfile is consumed by buildctl. Parsing either here reports a comment line as malformed
    YAML.

    Module-level so `test_skip_roles_classes_hold.py` asks the same question this script does.
    A test that reimplemented the predicate would keep passing after this one changed.
    """
    return not path.name.endswith(".sh.j2") and not path.name.startswith("Dockerfile")


_TEMPLATE_LOOKUP = re.compile(r"""lookup\(\s*['"]template['"]\s*,\s*([^)]*)\)""")
_TEMPLATES_PATH = re.compile(r"""['"][^'"]*/templates/([^'"]*)['"]""")
_YAML_COMMENT_LINE = re.compile(r"^\s*#.*$", re.M)


def misplaced_template_lookups(source: str) -> list[str]:
    """The `lookup('template', …)` targets in a manifest's source that sit at `templates/` top level.

    App config a manifest embeds belongs in `templates/config/`, one level down: the validator
    parses every top-level `templates/*.j2` as a manifest, so config sitting there is
    schema-checked as nothing (a document with no `kind` is skipped) and placement-checked as
    nothing.

    Only a literal path is judged. A target passed as a variable (image-builder's
    `lookup('template', src)`) resolves at task time from the caller's vars, which a text scan
    cannot see; those roles are in SKIP_ROLES regardless. Comments are stripped first — a
    `{# #}` block and a `#` line — so a comment that names a lookup by example (pihole's
    configmap.yaml.j2 does) is not judged as a call.
    """
    source = _YAML_COMMENT_LINE.sub("", strip_jinja_comments(source))
    misplaced = []
    for call in _TEMPLATE_LOOKUP.finditer(source):
        path = _TEMPLATES_PATH.search(call.group(1))
        if path and not path.group(1).startswith("config/"):
            misplaced.append(path.group(1))
    return misplaced


def non_manifest_documents(docs) -> list:
    """The parsed documents of a rendered top-level template that are not Kubernetes objects.

    A manifest renders one object per document, each carrying `kind`. A dict without one, a
    list, or a scalar is app config parsed as a manifest — the same placement failure as
    `misplaced_template_lookups`, seen from the render side for a file nothing embeds by a
    literal path. An empty document (`None`) is not counted: a template that renders nothing
    under a condition is still a manifest template.
    """
    return [
        d for d in docs if d is not None and not (isinstance(d, dict) and "kind" in d)
    ]


def k8s_entries(host_vars: Path = K8S_HOST_VARS) -> dict[str, dict]:
    """containers_list entries for the k8s platform in `host_vars`, keyed by service name.

    The filter is `Estate.k8s_entries`, which the docs generators call; this takes the one
    host file a caller names instead of an `Inventory`.
    """
    from lib.estate import Estate, Inventory

    inventory = Inventory(
        host_vars=host_vars.parent, plane_hosts={"k8s": host_vars.stem}
    )
    return Estate(inventory).k8s_entries()


# How one k8s role reaches another's tasks: `include_role`/`import_role` naming it as
# `k8s/<role>`. Whoever deploys the caller runs the callee's tasks.
#
# An `import_tasks` of a sibling role's file by path
# (`{{ role_path }}/../<role>/tasks/<file>.yml`) is the same edge spelled without a role name,
# and the walk does not cover it: no role reaches a sibling by path.
_ROLE_KEYS = (
    "ansible.builtin.include_role",
    "include_role",
    "ansible.builtin.import_role",
    "import_role",
)
_ROLE_NAME = re.compile(r"^k8s/([^/\s]+)$")


def _callees(node) -> set[str]:
    """Every k8s role the tasks under `node` reach. Walks blocks, which nest tasks."""
    found: set[str] = set()
    if isinstance(node, list):
        for item in node:
            found |= _callees(item)
        return found
    if not isinstance(node, dict):
        return found
    for key in _ROLE_KEYS:
        value = node.get(key)
        name = value.get("name") if isinstance(value, dict) else value
        if isinstance(name, str) and (m := _ROLE_NAME.match(name.strip())):
            found.add(m.group(1))
    for value in node.values():
        if isinstance(value, list | dict):
            found |= _callees(value)
    return found


def role_callers(repo: Path | str | None = None) -> dict[str, set[str]]:
    """For each k8s role reached from another role's tasks, the roles that reach it.

    The deploy-coverage question a helper role poses: it has no `containers_list` entry and so
    no deploy tag of its own, but `deploy.yml` runs it under every caller's tag. A change to it
    is therefore applied by deploying its callers — which is what
    `scripts/deploy_tools/land_tags.py` asks this to decide, so that landing a helper-role
    change alongside all of its callers stops reading as `needs-manual-apply`.

    Only ``roles/k8s`` is walked. The Pi's Compose roles include `containers/common`, not a k8s
    role, and no k8s role is reachable from them.

    ``repo`` names the checkout to walk, for a caller that reads a tree other than the one
    this module was imported from (``narrow_broad.narrow(cwd=...)``). Omitted, it walks
    ``lib.repo_paths.K8S_ROLES``, which is this file's own checkout.
    """
    roles_dir = K8S_ROLES if repo is None else Path(repo) / K8S_ROLES.relative_to(REPO)
    callers: dict[str, set[str]] = {}
    for tasks_dir in sorted(roles_dir.glob("*/tasks")):
        caller = tasks_dir.parent.name
        for task_file in sorted(tasks_dir.glob("*.yml")):
            try:
                tasks = yaml_fast.safe_load(task_file.read_text())
            except yaml.YAMLError:
                continue
            for callee in _callees(tasks):
                if callee != caller:
                    callers.setdefault(callee, set()).add(caller)
    return callers


# Manifest basenames `k8s/manifests` renders from a shared template under `ansible/templates/`
# when the owning role ships none of its own, mapped to that template's file name. Mirrors
# `manifests_shared_defaults` in ansible/roles/k8s/manifests/defaults/main.yml, which
# `ansible/tests/k8s/test_shared_manifest_defaults.py` holds it equal to.
#
# Duplicated here rather than read out of that YAML because every consumer is an offline render
# harness: reading the role default would make the harnesses depend on parsing a file whose
# other keys they have no use for, and the guard catches the drift either way.
SHARED_MANIFEST_DEFAULTS = {
    "service.yaml": "service-default.yaml.j2",
    "ingressroute.yaml": "ingressroute-default.yaml.j2",
}

# The keys a caller may set on its include, and the basenames inside whichever value shape it
# wrote. Read textually rather than by loading the tasks file: a role's tasks/main.yml is
# Jinja-bearing YAML, and authelia and traefik build their lists with a folded `>-` expression
# that no plain YAML load resolves to a list at all.
_MANIFEST_FILE_KEY = re.compile(
    r"^(?P<indent>\s*)(?P<key>manifests_(?:secret_|exclude_|deferred_)?files):(?P<rest>.*)$"
)
_MANIFEST_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml\b")
_YAML_COMMENT = re.compile(r"(^|\s)#.*$")

# The derivation `k8s/manifests` applies to a role that passes no `manifests_files` (#3662).
# ansible/roles/k8s/manifests/tasks/main.yml ("Resolve the manifest file lists") is the deploy's
# copy, and ansible/tests/k8s/test_derived_manifest_files.py holds the two in step.
#
# A role's own manifests are its top-level `templates/*.yaml.j2`. A Dockerfile, a `.sh.j2` and a
# `templates/config/` file are not, by their name or their depth.
MANIFEST_TEMPLATE_GLOB = "*.yaml.j2"
# A one-off Job is staged in a reserved directory by the role's own task and applied on its own
# (registry's selftests, the netpol probes). In the directory apply it would run on every deploy.
JOB_MANIFEST_SUFFIX = "-job.yaml"
# A basename naming a Secret renders 0600 under no_log. Every secret template in the tree is
# named this way; a test refuses a Secret template that is not, unless its role lists it itself.
SECRET_MANIFEST_NAME = re.compile(r"(^|-)secret[-.]")
# The containers_list key that earns a role each shared default, when it ships no own template.
SHARED_DEFAULT_TRIGGERS = {"service.yaml": "port", "ingressroute.yaml": "hostname"}


def _named_files(role, roles_dir: Path) -> dict[str, set[str]]:
    """For each file-list key a role's tasks/main.yml sets, the basenames in its value."""
    tasks = roles_dir / role / "tasks" / "main.yml"
    if not tasks.is_file():
        return {}
    lines = tasks.read_text().splitlines()
    named: dict[str, set[str]] = {}
    for i, line in enumerate(lines):
        match = _MANIFEST_FILE_KEY.match(line)
        if not match:
            continue
        region = [match.group("rest")]
        indent = len(match.group("indent"))
        # The value continues while lines stay indented past the key: a block list's `- `
        # items, a folded scalar's body, or an inline list broken over several lines. A comment
        # is not part of it: a name a comment mentions is neither rendered nor excluded.
        for following in lines[i + 1 :]:
            if not following.strip():
                continue
            if len(following) - len(following.lstrip()) <= indent:
                break
            region.append(following)
        region = [_YAML_COMMENT.sub("", line) for line in region]
        named.setdefault(match.group("key"), set()).update(
            _MANIFEST_BASENAME.findall("\n".join(region))
        )
    return named


def _real_entry(role) -> dict:
    """`role`'s containers_list entry in the k8s host's vars, or {} when it has none."""
    return _cached_k8s_entries().get(role, {})


@functools.cache
def _cached_k8s_entries() -> dict[str, dict]:
    return k8s_entries()


def resolved_manifest_files(
    role, k8s_roles=None, entry=None
) -> tuple[set[str], set[str]]:
    """`(manifests_files, manifests_secret_files)` as `k8s/manifests` resolves them for `role`.

    A list the role's include passes wins. Otherwise the list is derived:

    - its secret files are its own manifest templates whose name says `secret`;
    - its files are the rest of its own manifest templates, minus any one-off `-job.yaml`, its
      `manifests_deferred_files` and its `manifests_exclude_files`, plus `service.yaml` and
      `ingressroute.yaml` from the shared defaults when its containers_list entry carries a
      `port` or a `hostname` and it ships no template of that name.

    `entry` is the containers_list entry. Omitted, it is read from the k8s host's vars for the
    real tree, and taken as `{}` for any other `k8s_roles`, whose caller holds its own entries
    and passes the one it means.

    Over-inclusive for a list the caller passes as a Jinja expression: every `*.yaml` basename
    in the value counts, including one in a conditional branch the deploy may not take. That
    is the safe direction for the offline harnesses, which render what this returns.
    """
    roles_dir = Path(k8s_roles or K8S_ROLES)
    named = _named_files(role, roles_dir)
    templates = roles_dir / role / "templates"
    own = (
        {p.name.removesuffix(".j2") for p in templates.glob(MANIFEST_TEMPLATE_GLOB)}
        if templates.is_dir()
        else set()
    )
    candidates = {
        name for name in own if not name.endswith(JOB_MANIFEST_SUFFIX)
    } - named.get("manifests_exclude_files", set())
    if "manifests_secret_files" in named:
        secret = named["manifests_secret_files"]
    else:
        secret = {name for name in candidates if SECRET_MANIFEST_NAME.search(name)}
    if "manifests_files" in named:
        return named["manifests_files"], secret
    if entry is None:
        entry = _real_entry(role) if roles_dir == K8S_ROLES else {}
    shared = {
        basename
        for basename, key in SHARED_DEFAULT_TRIGGERS.items()
        if key in entry and basename not in own
    } - named.get("manifests_exclude_files", set())
    files = (
        (candidates | shared) - secret - named.get("manifests_deferred_files", set())
    )
    return files, secret


def declared_manifest_files(role, k8s_roles=None, entry=None) -> set[str]:
    """Every manifest basename `k8s/manifests` renders into `role`'s staging directory.

    Its files and its secret files, as `resolved_manifest_files` resolves them. The deploy
    renders exactly these, so this is also the list an offline harness has to cover to render
    what a deploy renders.
    """
    files, secret = resolved_manifest_files(role, k8s_roles, entry)
    return files | secret


def manifest_template(role, basename, k8s_roles=None, entry=None) -> Path | None:
    """The template `k8s/manifests` renders `basename` from for `role`, or None if it renders none.

    The role's own `templates/<basename>.j2` first, then the shared default under
    `ansible/templates/` for a basename the role resolves into `manifests_files` and ships
    no template for. `entry` is as for `resolved_manifest_files`.

    The one place a reader asks "does this role get a `<basename>`, and from where". Every
    caller that answered it with `(role/'templates'/f'{basename}.j2').is_file()` silently
    returned False for the 25 roles whose Service and the 16 whose IngressRoute moved to a
    shared default -- a docs generator printing "no route" for a routed service,
    or a guard skipping the role it was written to cover.
    """
    roles_dir = Path(k8s_roles or K8S_ROLES)
    own = roles_dir / role / "templates" / f"{basename}.j2"
    if own.is_file():
        return own
    shared = SHARED_MANIFEST_DEFAULTS.get(basename)
    if shared and basename in declared_manifest_files(role, roles_dir, entry):
        return SHARED_TPL / shared
    return None


def shared_default_templates(role, k8s_roles=None, entry=None) -> list[Path]:
    """The shared templates `k8s/manifests` renders for `role` because it ships none itself.

    Sorted, so a harness iterating this renders in a stable order.
    """
    roles_dir = Path(k8s_roles or K8S_ROLES)
    declared = declared_manifest_files(role, roles_dir, entry)
    return sorted(
        SHARED_TPL / shared
        for basename, shared in SHARED_MANIFEST_DEFAULTS.items()
        if basename in declared
        and not (roles_dir / role / "templates" / f"{basename}.j2").is_file()
    )


# The shared template `k8s/manifests` renders once per entry of a role's `k8s_claims`, as
# `claim-<name>.yaml`. One template, many renders, so a harness cannot find these claims by
# walking templates: it renders this once per context `claim_contexts` returns.
CLAIM_TEMPLATE = SHARED_TPL / "claim-default.yaml.j2"


def claim_contexts(ctx: dict) -> list[dict]:
    """One render context per `k8s_claims` entry in a role's resolved context `ctx`.

    `manifests_claim` is the loop variable the deploy's render task binds, so rendering
    CLAIM_TEMPLATE under each of these gives the bytes the deploy stages. Empty for a role
    that declares no claims.
    """
    return [{**ctx, "manifests_claim": claim} for claim in ctx.get("k8s_claims") or []]
