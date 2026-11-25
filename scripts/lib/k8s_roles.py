#!/usr/bin/env python3
"""Which roles exist under ``k8s/roles/``, and which of them each reader skips.

``role_dirs`` is the one census of a role tree for code under ``scripts/``: every reader
that lists the roles calls it and passes the names it skips as ``exclude``. The two
exemption sets and ``is_manifest_template`` are the half other guards ask about
(``scripts/validate/tests/test_skip_roles_classes_hold.py``,
``scripts/diagnostics/probe_lib/health.py``), which is why they are their own module rather
than private to the validator.

``K8S_ROLES`` comes from ``lib.repo_paths`` rather than being derived a second time here.
"""

import dataclasses
import functools
import os
import re
from collections.abc import Collection, Iterable
from pathlib import Path

import jinja2
import yaml

from lib import yaml_fast
from lib.jinja_comments import strip_jinja_comments
from lib.k8s_role_callers import role_callers
from lib.repo_paths import K8S_ROLES, K8S_SERVICES, SHARED_TPL

__all__ = [
    "CALLER_RENDERED_ROLES",
    "CLAIM_TEMPLATE",
    "K8S_ROLES",
    "NO_MANIFEST_ROLES",
    "RoleDefaults",
    "RoleDefaultsError",
    "MANIFESTS_PRUNE_DEFAULT",
    "ROLLOUT_TIMEOUT_DEFAULT",
    "SHARED_MANIFEST_DEFAULTS",
    "SKIP_ROLES",
    "claim_contexts",
    "declared_secret_file",
    "is_leftover_dir",
    "is_manifest_template",
    "k8s_autodeploy_denylist",
    "manifest_template",
    "shared_default_templates",
    "k8s_entries",
    "misplaced_template_lookups",
    "non_manifest_documents",
    "role_callers",
    "resolved_manifest_files",
    "role_defaults",
    "role_dirs",
    "RENDER_KEYS",
    "SECRET_TEMPLATE",
]


def is_leftover_dir(path: Path | str) -> bool:
    """Whether `path` is a retired role's debris: no file left but compiled Python.

    checkout-sync's fast-forward removes a retired role's tracked files, but a gitignored
    `__pycache__/` left by a pytest run keeps `roles/<plane>/<role>/` on disk. A walker that
    read that shell as a role would render it, census it or credit it.

    The predicate is "nothing real is left", not "it looks like a role" (no `tasks/`, no
    `defaults/`). A shape test would skip a genuine role that carries neither directory, and
    `k8s_autodeploy_denylist` would then leave it off the list. A real role always holds at
    least one non-`.pyc` file.

    An EMPTY directory is not debris. git removes a directory the moment its last tracked file
    goes, so one exists only because a person or a script made it, which is worth failing on.
    """
    found_debris = False
    for dirpath, dirnames, filenames in os.walk(path):
        if os.path.basename(dirpath) == "__pycache__":
            dirnames[:] = []
            found_debris = found_debris or bool(filenames)
            continue
        for name in filenames:
            if not name.endswith(".pyc"):
                return False
            found_debris = True
    return found_debris


def role_dirs(roles_dir: Path = K8S_ROLES, exclude: Collection[str] = ()) -> list[Path]:
    """Every real role directory under `roles_dir`, sorted, minus the names in `exclude`.

    A retired role's debris is always dropped (`is_leftover_dir`). `exclude` is how a reader
    states the roles it skips on purpose: the validator passes `SKIP_ROLES`. `roles_dir` may be
    any plane's tree, so the setup-role readers walk through here too.
    """
    return sorted(
        d
        for d in roles_dir.iterdir()
        if d.is_dir() and d.name not in exclude and not is_leftover_dir(d)
    )


def k8s_autodeploy_denylist(roles_dir: Path | str = K8S_ROLES) -> list[str]:
    """The k8s roles declaring `k8s_autodeploy: false`, sorted.

    Renovate may automerge an image bump only for a role this list leaves out, and the lander
    (`land_renovate_pr.py`) refuses a PR under a role it names. A role absent from the list is
    therefore eligible, so anything this cannot read with certainty raises rather than drop out.

    Args:
        roles_dir: the directory holding one subdirectory per k8s role.

    Raises:
        ValueError: a role declares no boolean stance or no reason, its defaults cannot be
            read, an entry is not a directory, or the list is empty.
    """
    roles_dir = Path(roles_dir)
    if not roles_dir.is_dir():
        raise ValueError(f"k8s_autodeploy_denylist: no such directory: {roles_dir}")

    denied = []
    for entry in sorted(roles_dir.iterdir()):
        role = entry.name
        if role.startswith(".") or role == "__pycache__":
            continue
        if not entry.is_dir():
            raise ValueError(
                f"k8s_autodeploy_denylist: '{entry}' is not a directory. Every entry under "
                f"k8s/roles/ must be a real role directory declaring `k8s_autodeploy`; a "
                f"dangling symlink is one likely cause."
            )
        if is_leftover_dir(entry):
            continue

        path = entry / "defaults" / "main.yml"
        if not path.is_file():
            raise ValueError(
                f"k8s_autodeploy_denylist: role '{role}' has no defaults/main.yml and so "
                f"declares no `k8s_autodeploy` stance. Add one (copy the shape from a "
                f"sibling role)."
            )
        try:
            data = yaml_fast.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ValueError(
                f"k8s_autodeploy_denylist: cannot read '{role}' defaults ({path}): {exc}"
            ) from exc

        if not isinstance(data, dict) or "k8s_autodeploy" not in data:
            raise ValueError(
                f"k8s_autodeploy_denylist: role '{role}' does not set `k8s_autodeploy` in "
                f"{path}. Every role under k8s/roles/ must declare whether Renovate may "
                f"automerge its image bumps."
            )
        value = data["k8s_autodeploy"]
        if not isinstance(value, bool):
            raise ValueError(
                f"k8s_autodeploy_denylist: role '{role}' sets `k8s_autodeploy: {value!r}`, "
                f"which is not a boolean. Use true or false."
            )
        reason = data.get("k8s_autodeploy_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(
                f"k8s_autodeploy_denylist: role '{role}' sets `k8s_autodeploy` but no "
                f"`k8s_autodeploy_reason`. A bare boolean cannot be audited."
            )
        if value is False:
            denied.append(role)

    if not denied:
        raise ValueError(
            f"k8s_autodeploy_denylist: derived an EMPTY denylist from {roles_dir}, which "
            "means the derivation is broken."
        )
    return denied


# Helper roles, included by service roles rather than deployed on their own. They have no
# service entry because they are not services, so the entry check below would always
# fail for them.
#
# volume-snapshot applies one Longhorn Snapshot CR per claim, built inline and piped to `kubectl
# apply -f -` — per-deploy state rather than part of a service's manifest set, and
# `scripts/renderer/tests/test_renderer_snapshot.py` is what checks its shape instead.
#
# Split into the two classes the comment above already distinguishes, because they rot in
# opposite directions and a single set cannot be checked either way. NO_MANIFEST_ROLES makes a
# claim about the tree that `test_skip_roles_classes_hold.py` asserts, so a role that grows its
# first manifest template stops being silently exempt. CALLER_RENDERED_ROLES makes the opposite
# claim, asserted the opposite way, so an entry that stops carrying templates is caught as a
# stale exemption rather than left as a name nobody can justify.
NO_MANIFEST_ROLES = {
    "volume-snapshot",
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
# suite: `tests/_k8s_render.py:rendered_build_job_text` renders it against a fixture of
# a caller's vars, and the security-context, token-mount and namespace-defaults guards parse
# that render. One manifest (image-builder's context-configmap) is still parsed as YAML
# nowhere; this names that gap where someone will look for it.
CALLER_RENDERED_ROLES = {
    "image-builder",
}

SKIP_ROLES = NO_MANIFEST_ROLES | CALLER_RENDERED_ROLES


def is_manifest_template(path: Path) -> bool:
    """True if this `.j2` under a role's templates/ is a manifest this script should parse.

    A role may also ship a helper script (registry's registry-gc.sh.j2) or a Dockerfile
    for image-builder (homelab-mcp). Shell is rendered and linted by validate/render_lib/shell.py;
    a Dockerfile is consumed by buildctl. Parsing either here reports a comment line as malformed
    YAML.

    Module-level so `test_skip_roles_classes_hold.py` asks the same question this script does.
    A test that reimplemented the predicate would keep passing after this one changed.
    """
    return not path.name.endswith(".sh.j2") and not path.name.startswith("Dockerfile")


_TEMPLATE_LOOKUP = re.compile(r"""lookup\(\s*['"]template['"]\s*,\s*([^)]*)\)""")
_TEMPLATES_PATH = re.compile(r"""['"](?:[^'"]*/)?templates/([^'"]*)['"]""")
_YAML_COMMENT_LINE = re.compile(r"^\s*#.*$", re.M)


def misplaced_template_lookups(source: str) -> list[str]:
    """The `lookup('template', …)` targets in a manifest's source that sit at `templates/` top level.

    App config a manifest embeds belongs in `templates/config/`, one level down: the validator
    parses every top-level `templates/*.j2` as a manifest, so config sitting there is
    schema-checked as nothing (a document with no `kind` is skipped) and placement-checked as
    nothing.

    A `*.sh.j2` target is not judged: it is a script, not app config. Only a literal path is judged. A target passed as a variable (image-builder's
    `lookup('template', src)`) resolves at task time from the caller's vars, which a text scan
    cannot see; those roles are in SKIP_ROLES regardless. Comments are stripped first — a
    `{# #}` block and a `#` line — so a comment that names a lookup by example (pihole's
    configmap.yaml.j2 does) is not judged as a call.
    """
    source = _YAML_COMMENT_LINE.sub("", strip_jinja_comments(source))
    misplaced = []
    for call in _TEMPLATE_LOOKUP.finditer(source):
        path = _TEMPLATES_PATH.search(call.group(1))
        # A `*.sh.j2` is a shell script the shell-template gate renders and lints, and
        # `is_manifest_template` already keeps it out of the manifest corpus, so a script a
        # ConfigMap embeds may sit at `templates/` top level (longhorn-restore-drill).
        if (
            path
            and not path.group(1).startswith("config/")
            and not path.group(1).endswith(".sh.j2")
        ):
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


def k8s_entries(services: Path = K8S_SERVICES) -> dict[str, dict]:
    """The entries of the service list `services`, keyed by service name."""
    from lib.render_guard import k8s_services

    return {e["name"]: e for e in k8s_services(services)}


def k8s_tags(services: Path = K8S_SERVICES) -> set[str]:
    """Every deploy tag of a k8s entry in `services`, each one Flux applies (C4).

    The tags rather than the entry names: an entry that declares `tags` is selected by each of
    them, and its name need not be one.
    """
    from lib.render_guard import entry_tags

    return {
        tag for entry in k8s_entries(services).values() for tag in entry_tags(entry)
    }


# Manifest basenames the renderer renders from a shared template under `k8s/templates/`
# when the owning role ships none of its own, mapped to that template's file name. Mirrors
# The manifests role's two defaults the render reads. A role with no `manifests_prune` of its own
# arms the prune and renders the `homelab/role` label, and one with no
# `manifests_rollout_timeout` of its own takes this budget.
MANIFESTS_PRUNE_DEFAULT = True
ROLLOUT_TIMEOUT_DEFAULT = "600s"

# The template that renders each manifest a role ships no template for.
SHARED_MANIFEST_DEFAULTS = {
    "service.yaml": "service-default.yaml.j2",
    "ingressroute.yaml": "ingressroute-default.yaml.j2",
}

# The file-list keys a role sets, and the basenames inside whichever value shape it wrote. Read
# textually rather than by loading the file: authelia and traefik build their lists with a folded
# `>-` expression that no plain YAML load resolves to a list at all.
_MANIFEST_FILE_KEY = re.compile(
    r"^(?P<indent>\s*)(?P<key>manifests_(?:exclude_|deferred_)?files):(?P<rest>.*)$"
)
_MANIFEST_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml\b")
_YAML_COMMENT = re.compile(r"(^|\s)#.*$")

# The derivation for a role that sets no `manifests_files`.
#
# A role's own manifests are its top-level `templates/*.yaml.j2`. A Dockerfile, a `.sh.j2` and a
# `templates/config/` file are not, by their name or their depth.
MANIFEST_TEMPLATE_GLOB = "*.yaml.j2"
# A one-off Job is staged in a reserved directory by the role's own task and applied on its own
# (registry's selftests, the netpol probes). In the directory apply it would run on every deploy.
JOB_MANIFEST_SUFFIX = "-job.yaml"
# The containers_list key that earns a role each shared default, when it ships no own template.
SHARED_DEFAULT_TRIGGERS = {"service.yaml": "port", "ingressroute.yaml": "hostname"}


# The render metadata a role sets in its defaults/main.yml.
RENDER_KEYS = frozenset(
    {
        "manifests_deferred_dir_name",
        "manifests_deferred_files",
        "manifests_exclude_files",
        "manifests_extra_rollouts",
        "manifests_files",
        "manifests_outside_secrets",
        "manifests_prune",
        "manifests_rollout",
        "manifests_self_rollouts",
    }
)


def _named_files(role, roles_dir: Path) -> dict[str, set[str]]:
    """For each file-list key a role's defaults/main.yml sets, the basenames in its value.

    Raises:
        ValueError: The role has no defaults/main.yml, or its `tasks/` sets a moved key.
    """
    role_dir = roles_dir / role
    defaults = role_dir / "defaults" / "main.yml"
    if not defaults.is_file():
        raise ValueError(f"{role} has no defaults/main.yml")
    return _file_list_keys(defaults)


def _file_list_keys(path: Path) -> dict[str, set[str]]:
    """For each file-list key `path` sets, the basenames in its value."""
    lines = path.read_text().splitlines()
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


def resolved_manifest_files(role, k8s_roles=None, entry=None) -> set[str]:
    """Every manifest basename the renderer renders for `role`.

    A `manifests_files` the role sets wins. Otherwise the list is derived: the role's own
    manifest templates, minus any one-off `-job.yaml`, its `manifests_deferred_files` and its
    `manifests_exclude_files`, plus `service.yaml` and `ingressroute.yaml` from the shared
    defaults when its containers_list entry carries a `port` or a `hostname` and it ships no
    template of that name.

    `entry` is the containers_list entry. Omitted, it is read from the k8s host's vars for the
    real tree, and taken as `{}` for any other `k8s_roles`, whose caller holds its own entries
    and passes the one it means.

    Over-inclusive for a list the caller passes as a Jinja expression: every `*.yaml` basename
    in the value counts, including one in a conditional branch the deploy may not take. That
    is the safe direction for the offline harnesses, which render what this returns.
    """
    roles_dir = Path(k8s_roles or K8S_ROLES)
    named = _named_files(role, roles_dir)
    if "manifests_files" in named:
        return named["manifests_files"]
    templates = roles_dir / role / "templates"
    own = (
        {p.name.removesuffix(".j2") for p in templates.glob(MANIFEST_TEMPLATE_GLOB)}
        if templates.is_dir()
        else set()
    )
    if entry is None:
        entry = _real_entry(role) if roles_dir == K8S_ROLES else {}
    shared = {
        basename
        for basename, key in SHARED_DEFAULT_TRIGGERS.items()
        if key in entry and basename not in own
    }
    candidates = {name for name in own if not name.endswith(JOB_MANIFEST_SUFFIX)}
    return (
        (candidates | shared)
        - named.get("manifests_exclude_files", set())
        - named.get("manifests_deferred_files", set())
    )


def manifest_template(role, basename, k8s_roles=None, entry=None) -> Path | None:
    """The template the renderer renders `basename` from for `role`, or None if it renders none.

    The role's own `templates/<basename>.j2` first, then the shared default under
    `k8s/templates/` for a basename the role resolves into `manifests_files` and ships
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
    if shared and basename in resolved_manifest_files(role, roles_dir, entry):
        return SHARED_TPL / shared
    return None


def shared_default_templates(role, k8s_roles=None, entry=None) -> list[Path]:
    """The shared templates the renderer renders for `role` because it ships none itself.

    Sorted, so a harness iterating this renders in a stable order.
    """
    roles_dir = Path(k8s_roles or K8S_ROLES)
    declared = resolved_manifest_files(role, roles_dir, entry)
    return sorted(
        SHARED_TPL / shared
        for basename, shared in SHARED_MANIFEST_DEFAULTS.items()
        if basename in declared
        and not (roles_dir / role / "templates" / f"{basename}.j2").is_file()
    )


# The shared template the renderer renders once per entry of a role's `k8s_claims`, as
# `claim-<name>.yaml`. One template, many renders, so a harness cannot find these claims by
# walking templates: it renders this once per context `claim_contexts` returns.
CLAIM_TEMPLATE = SHARED_TPL / "claim-default.yaml.j2"
# The shared template the renderer renders once per entry of a role's `render_secrets`, the
# same way, to the file `declared_secret_file` names.
SECRET_TEMPLATE = SHARED_TPL / "secret-default.yaml.j2"


def declared_secret_file(name: str) -> str:
    """The file a `render_secrets` entry named `name` renders to: `secret-<name>.yaml`."""
    return f"secret-{name}.yaml"


def claim_contexts(ctx: dict) -> list[dict]:
    """One render context per `k8s_claims` entry in a role's resolved context `ctx`.

    `manifests_claim` is the loop variable the deploy's render task binds, so rendering
    CLAIM_TEMPLATE under each of these gives the bytes the deploy stages. Empty for a role
    that declares no claims.
    """
    return [{**ctx, "manifests_claim": claim} for claim in ctx.get("k8s_claims") or []]


class RoleDefaultsError(ValueError):
    """A role's `defaults/main.yml` that cannot be read, or sets a field to a non-literal."""


@dataclasses.dataclass(frozen=True)
class RoleDefaults:
    """The fields a role's `defaults/main.yml` sets on its Flux Kustomizations.

    C3 *Per-role fields* lets a role set them (H1.1a). `flux_files` in
    `renderer_lib/bundle.py` decides which Kustomization each one reaches.

    Attributes:
        prune: `flux_prune`; False renders `prune: false` on `<tag>` alone. A plumbing tag
            sets it, so a render that drops an object leaves it serving (H1 *Plumbing tags
            are never pruned*).
        suspend: `flux_suspend`; True renders `suspend: true` on every kind (C3
            *Suspension*, D5.1b), because a suspended role runs none of its Jobs either.
        timeout: `flux_timeout`, else `manifests_rollout_timeout`, rendered as `timeout` on
            `<tag>` and `<tag>-deferred`; None takes the bundle's default. The manifests
            role's own rollout wait reads `manifests_rollout_timeout` too, and `flux_timeout`
            is for a role whose Kustomization waits longer than its rollout does (H6.1b).
        snapshot_pvcs: `k8s_autodeploy_snapshot_pvcs`, sorted: the claims the manifests role
            snapshots before each apply. A non-empty tuple calls for `<tag>-snapshot`
            (D5.2a), so `digests.json`'s `claims` and the publish census
            (`deploy_tools/bundle_census.py`) read this one field.
        health_check_exprs: `flux_health_check_exprs`, each string rendered as Jinja against
            the role's own defaults, so an expression follows the pin it compares with.
            Written as `spec.healthCheckExprs` on `<tag>` alone (H6.1b).
        prepull_timeout: `flux_prepull_timeout`, which alone calls for `<tag>-prepull` and
            is that Kustomization's `timeout`; None renders no pre-pull.
        prepull_workloads: `flux_prepull_workloads`, the workloads `<tag>-prepull` pulls
            for; None pulls for all. A role sets it only beside `flux_prepull_timeout`.
        cronjob_gates: `flux_cronjob_gates`, each CronJob the role renders mapped to its
            gate's class, `{kind: verify}` or `{kind: mutation, rerun_safe:
            <path>::<function>}`. A gate calls for `<tag>-verify`, which holds the gate Job
            `cronjob_gate.py` copies from the CronJob.
        verify_after: `flux_verify_after`, sorted: the other tags whose rolled workload the
            role's verify Jobs probe, so `<tag>-verify` waits on each one's last
            Kustomization too. crowdsec names `traefik`, because its ban proof reads the edge.
    """

    prune: bool = True
    suspend: bool = False
    timeout: str | None = None
    snapshot_pvcs: tuple[str, ...] = ()
    health_check_exprs: tuple[dict, ...] = ()
    prepull_timeout: str | None = None
    prepull_workloads: tuple[str, ...] | None = None
    cronjob_gates: dict[str, dict] = dataclasses.field(default_factory=dict)
    verify_after: tuple[str, ...] = ()


# A Go duration as Flux parses `spec.timeout`, such as `600s` or `1h30m`.
_DURATION = re.compile(r"(?:\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h))+")


# A duration of whole hours, minutes and seconds, the form `chained_timeout.seconds` reads.
WHOLE_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


_CHECK_KEYS = frozenset({"apiVersion", "kind", "inProgress", "failed", "current"})


_GATE_KINDS = frozenset({"verify", "mutation"})


def _bool(defaults: dict, key: str, default: bool) -> bool:
    value = defaults.get(key, default)
    if not isinstance(value, bool):
        raise RoleDefaultsError(f"{key} is {value!r}, not a literal bool")
    return value


def _duration(
    defaults: dict, key: str, example: str, grammar: re.Pattern = _DURATION
) -> str | None:
    value = defaults.get(key)
    if value is not None and not (
        isinstance(value, str) and value and grammar.fullmatch(value)
    ):
        raise RoleDefaultsError(
            f"{key} is {value!r}, not a literal duration such as {example}, so its "
            "Kustomization timeout cannot be read"
        )
    return value


def _snapshot_pvcs(defaults: dict) -> tuple[str, ...]:
    value = defaults.get("k8s_autodeploy_snapshot_pvcs")
    if value is not None and not isinstance(value, list):
        raise RoleDefaultsError(
            f"k8s_autodeploy_snapshot_pvcs is {value!r}, not a literal list"
        )
    return tuple(sorted(str(claim) for claim in value or []))


def _health_check_exprs(defaults: dict) -> tuple[dict, ...]:
    """The role's `flux_health_check_exprs`, each string rendered under `StrictUndefined`.

    So a name the defaults lack is a problem and not an empty string.
    """
    raw = defaults.get("flux_health_check_exprs")
    if raw is None:
        return ()
    if not (isinstance(raw, list) and all(isinstance(item, dict) for item in raw)):
        raise RoleDefaultsError(
            f"flux_health_check_exprs is {raw!r}, not a list of mappings"
        )
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    rendered = []
    for item in raw:
        if not {"apiVersion", "current"} <= set(item) or set(item) - _CHECK_KEYS:
            raise RoleDefaultsError(
                f"flux_health_check_exprs item {sorted(item)} is not a CustomHealthCheck"
            )
        try:
            rendered.append(
                {k: env.from_string(str(v)).render(defaults) for k, v in item.items()}
            )
        except jinja2.TemplateError as err:
            raise RoleDefaultsError(
                f"flux_health_check_exprs does not render: {err}"
            ) from err
    return tuple(rendered)


def _prepull_workloads(defaults: dict) -> tuple[str, ...] | None:
    value = defaults.get("flux_prepull_workloads")
    if value is None:
        return None
    if not (
        isinstance(value, list)
        and value
        and all(isinstance(name, str) and name for name in value)
    ):
        raise RoleDefaultsError(
            f"flux_prepull_workloads is {value!r}, not a non-empty list of workload names, "
            "so its pre-pull cannot be scoped"
        )
    if "flux_prepull_timeout" not in defaults:
        raise RoleDefaultsError(
            "flux_prepull_workloads is set with no flux_prepull_timeout, so no pre-pull "
            "renders for the names to scope"
        )
    return tuple(value)


def _gate_problems(value: object) -> list[str]:
    """Why `value` is not a literal `flux_cronjob_gates` mapping, empty when it is one."""
    if not isinstance(value, dict) or not value:
        return [
            f"{value!r} is not a non-empty mapping of CronJob names to gate classes"
        ]
    problems = []
    for name, gate in value.items():
        if not isinstance(gate, dict) or gate.get("kind") not in _GATE_KINDS:
            problems.append(f"{name}: {gate!r} names no kind of {sorted(_GATE_KINDS)}")
            continue
        extra = set(gate) - {"kind", "rerun_safe"}
        rerun = gate.get("rerun_safe")
        if extra:
            problems.append(f"{name}: unknown keys {sorted(extra)}")
        elif gate["kind"] == "mutation" and not (
            isinstance(rerun, str) and "::" in rerun
        ):
            problems.append(
                f"{name}: a mutation gate names its rerun_safe test as <path>::<function>"
            )
        elif gate["kind"] == "verify" and rerun is not None:
            problems.append(
                f"{name}: a verify gate never re-runs, so takes no rerun_safe"
            )
    return problems


def _cronjob_gates(defaults: dict) -> dict[str, dict]:
    value = defaults.get("flux_cronjob_gates")
    if value is None:
        return {}
    wrong = _gate_problems(value)
    if wrong:
        raise RoleDefaultsError(
            "; ".join(f"flux_cronjob_gates: {line}" for line in wrong)
        )
    return {str(name): dict(gate) for name, gate in value.items()}


def _verify_after(defaults: dict, tag: str) -> tuple[str, ...]:
    value = defaults.get("flux_verify_after")
    if value is None:
        return ()
    if not (
        isinstance(value, list)
        and value
        and all(isinstance(name, str) and name and name != tag for name in value)
    ):
        raise RoleDefaultsError(
            f"flux_verify_after is {value!r}, not a literal non-empty list of other "
            "tags' names"
        )
    return tuple(sorted(set(value)))


@functools.cache
def _role_defaults(roles: Path, tag: str) -> RoleDefaults:
    """`tag`'s `RoleDefaults`, loaded once per role tree; a role with no file sets none.

    Raises:
        RoleDefaultsError: The file does not parse, or a field is not a literal of its type.
    """
    path = roles / tag / "defaults" / "main.yml"
    try:
        defaults = (
            yaml_fast.safe_load(path.read_text()) if path.is_file() else {}
        ) or {}
    except (OSError, yaml.YAMLError) as err:
        raise RoleDefaultsError(f"defaults cannot be read: {err}") from err
    own_timeout = _duration(defaults, "flux_timeout", "100m")
    rollout_timeout = _duration(defaults, "manifests_rollout_timeout", "900s")
    return RoleDefaults(
        prune=_bool(defaults, "flux_prune", True),
        suspend=_bool(defaults, "flux_suspend", False),
        timeout=own_timeout or rollout_timeout,
        snapshot_pvcs=_snapshot_pvcs(defaults),
        health_check_exprs=_health_check_exprs(defaults),
        prepull_timeout=_duration(
            defaults, "flux_prepull_timeout", "3600s", WHOLE_DURATION
        ),
        prepull_workloads=_prepull_workloads(defaults),
        cronjob_gates=_cronjob_gates(defaults),
        verify_after=_verify_after(defaults, tag),
    )


def role_defaults(
    tags: Iterable[str], roles: Path = K8S_ROLES
) -> tuple[dict[str, RoleDefaults], list[str]]:
    """Each tag's `RoleDefaults`, and one line per tag whose defaults cannot be read.

    A value must be a literal: a bool for `flux_prune` and `flux_suspend`, a Go duration
    for either timeout and a whole h/m/s one for `flux_prepull_timeout`, a list for
    `k8s_autodeploy_snapshot_pvcs`, a list of CustomHealthCheck mappings for
    `flux_health_check_exprs`, a non-empty list of names for `flux_prepull_workloads` and
    `flux_verify_after`, and a mapping of gate classes for `flux_cronjob_gates`. A Jinja expression is a
    problem rather than a guess, and its tag is left out of the mapping.

    Returns:
        Each readable tag mapped to its `RoleDefaults`, the class defaults for a tag whose
        role sets none, and one problem per tag left out, starting `<tag>'s `.
    """
    found: dict[str, RoleDefaults] = {}
    problems = []
    for tag in sorted(set(tags)):
        try:
            found[tag] = _role_defaults(roles, tag)
        except RoleDefaultsError as err:
            problems.append(f"{tag}'s {err}")
    return found, problems


# Read at a commit from before the move to `k8s/`; goes once no landing reads one.
PRE_MOVE_SERVICES = "cluster/services.yml"


def k8s_tags_in(repo_root: Path, at: str = "") -> set[str]:
    """Every k8s entry's deploy tag, in `repo_root`'s tree or, given `at`, at that commit.

    A commit from before the list moved to `k8s/` holds it at `PRE_MOVE_SERVICES`, so that
    file is read at such a commit.

    Raises:
        subprocess.CalledProcessError: `at` holds neither file.
    """
    import tempfile

    from lib.git import git
    from lib.repo_paths import REPO

    rel = K8S_SERVICES.relative_to(REPO)
    if not at:
        return k8s_tags(repo_root / rel)
    shown = git("show", f"{at}:{rel.as_posix()}", cwd=repo_root, check=False)
    if shown.returncode != 0:
        shown = git("show", f"{at}:{PRE_MOVE_SERVICES}", cwd=repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / rel.name
        path.write_text(shown.stdout)
        return k8s_tags(path)
