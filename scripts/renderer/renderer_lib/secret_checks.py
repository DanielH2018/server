"""The renderer's secret checks: the five rules every placeholder render must pass (target slice R2).

`check` runs after every placeholder render, before anything is written. A violation makes
`homelab_render.py cluster` exit 1 and print the tag, the file, the document or form, and the
key.

1. `known-key`: in a directory that substitutes, every `${NAME}` that opens a Flux reference
   names a key in `ansible/vars/secrets.yml`.
2. `no-bare-dollar-brace`: in a directory that substitutes, every `${` that opens a reference
   is a placeholder of pass 3, read as Flux reads it, each `$$` paired first. In every
   directory, no sentinel fragment remains.
3. `confined-to-secret`: a placeholder sits only in a document of `kind: Secret`, except for
   the keys in `PLACEHOLDER_ANYWHERE`.
4. `no-sops-manifest-key`: no key in `SOPS_MANIFEST_KEYS` appears as a placeholder.
5. `no-secret-expression`: in the template sources, a secret key appears only in a form that
   leaves a single-line value intact. Rule 5 reads every `.j2` under the k8s roles'
   `templates/` and `ansible/templates/` except `*.sh.j2`, whatever the render's tags.

Rule 5's syntax-tree walk is `jinja_uses.py`. This module gives it the sets it reads, and
turns each refusal into a `Violation`.

Rules 1 to 4 read the rendered output. Each asserts its named member only when that member's
tag is in the render, and a `KNOWN_VIOLATIONS` entry of theirs counts as stale only when its
tag was rendered. Rule 5 scans the whole tree, so its floor, its named members and its stale
entries hold on every run.

`docs/homelab-render.md` is the operator's page, and
`docs/target-architecture/render/r2-secret-placeholders.md` the plan.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple


from lib.render_guard import containers_entries
from lib.repo_paths import HOST_VARS, K8S_ROLES, REPO, SHARED_TPL
from renderer.renderer_lib.environment import renderer_env
from renderer.renderer_lib.jinja_uses import (
    DYNAMIC_LOOKUP,
    TRUTHINESS,
    Policy,
    TemplateScan,
    Use,
    scan,
)
from renderer.renderer_lib.placeholders import (
    SENTINEL_PREFIX,
    PlaceholderRender,
    documents,
    references,
    substitution_disabled,
)

KNOWN_KEY = "known-key"
NO_BARE_DOLLAR_BRACE = "no-bare-dollar-brace"
CONFINED_TO_SECRET = "confined-to-secret"
NO_SOPS_MANIFEST_KEY = "no-sops-manifest-key"
NO_SECRET_EXPRESSION = "no-secret-expression"
OUTPUT_RULES = (
    KNOWN_KEY,
    NO_BARE_DOLLAR_BRACE,
    CONFINED_TO_SECRET,
    NO_SOPS_MANIFEST_KEY,
)

# The repo filters that place an argument into their output unchanged. Rule 5 accepts a secret
# key as an argument to a member, and refuses it as an argument to any other filter, so the set
# is closed. Each member passes the membership test in
# `scripts/renderer/tests/test_renderer_secret_checks.py`, and a filter that fails it leaves
# the set.
PASSTHROUGH_FILTERS = frozenset(
    {
        # ansible/filter_plugins/homepage_tiles.py:homepage_href
        "homepage_href",
        # ansible/filter_plugins/authelia_access.py:authelia_service_rules
        "authelia_service_rules",
    }
)

# The keys rule 3 lets leave a Secret. Both are identifiers, not credentials: `domain` names
# every route in IngressRoutes, Middlewares and ConfigMaps, and `email` the ACME and alert
# addresses. A key that may leave a Secret for one kind only is a `(key, kind)` pair, never a
# member here.
PLACEHOLDER_ANYWHERE = frozenset({"domain", "email"})

# The keys no placeholder may carry. Flux deletes every newline from a substitution value, so
# a multi-line value cannot pass through a placeholder at all. R2's PRs 5 to 7 move each into
# a dedicated Secret that D3 ships as a SOPS-encrypted manifest.
SOPS_MANIFEST_KEYS = frozenset(
    {
        # pi-peer-backup's SSH private key, a PEM block.
        "pi_peer_backup_ssh_key",
        # Authelia's OIDC signing key, a PEM block under `jwks`.
        "authelia_oidc_rsa_key_content",
        # Home Assistant's Google service account, a JSON document.
        "google_assistant_service_account",
    }
)

# A case-insensitive fragment of the sentinel `HOMELAB_SECRET__K__END`. A transform that only
# partly alters a sentinel, such as `lower`, leaves it in the output.
SENTINEL_FRAGMENT = re.compile(re.escape(SENTINEL_PREFIX.rstrip("_")), re.IGNORECASE)


class Violation(NamedTuple):
    """One breach of a rule, and the shape of a `KNOWN_VIOLATIONS` entry.

    Attributes:
        rule: The rule's name, such as `no-secret-expression`.
        tag: For rules 1 to 4, the directory under `cluster/`. For rule 5, the role whose
            template it is, or `shared` for `ansible/templates/`.
        file: For rules 1 to 4, the rendered file. For rule 5, the template's path from the
            repository root.
        site: For rules 1 to 4, the document as `Kind/name`. For rule 5, the form it refuses.
        key: The secret key, or the reference Flux would read.
    """

    rule: str
    tag: str
    file: str
    site: str
    key: str

    def line(self) -> str:
        return f"{self.rule}: {self.tag} {self.file} {self.site}: {self.key}"


_ROLE_TEMPLATES = "ansible/roles/k8s/{}/templates/{}"


def _rule5(role: str, template: str, form: str, name: str) -> Violation:
    """A rule 5 entry for `template` under `role`'s `templates/` directory."""
    path = _ROLE_TEMPLATES.format(role, template)
    return Violation(NO_SECRET_EXPRESSION, role, path, form, name)


def _rule4(tag: str, file: str, site: str, name: str) -> Violation:
    return Violation(NO_SOPS_MANIFEST_KEY, tag, file, site, name)


# The push-tile tokens that uptime-kuma's static monitors test with
# `{% if <token> | default('') %}`.
_KUMA_TOKENS = (
    "aide_daniel_box_push_token",
    "aide_daniel_pi_push_token",
    "aide_daniel_server_push_token",
    "docs_refresh_push_token",
    "etcd_drill_fence_push_token",
    "etcd_drill_full_push_token",
    "etcd_snapshot_push_token",
    "homelab_eval_push_token",
    "kuma_status_page_sync_push_token",
    "live_drift_push_token",
    "loki_route_witness_daniel_server_push_token",
    "loki_route_witness_push_token",
    "manifest_prune_push_token",
    "registry_gc_push_token",
    "release_staleness_push_token",
    "render_records_push_token",
    "rkhunter_daniel_box_push_token",
    "rkhunter_daniel_pi_push_token",
    "rkhunter_daniel_server_push_token",
    "setup_drift_push_token",
    "ups_secondary_daniel_server_push_token",
    "ups_secondary_push_token",
)
_AUTHELIA_KEY, _PPB_KEY = "authelia_oidc_rsa_key_content", "pi_peer_backup_ssh_key"

# Every violation the first fleet render finds. Each entry leaves through the PR that fixes
# its source, and an entry that no longer matches fails the check, so the set shrinks only
# through a fix. R2's exit criterion requires it empty.
KNOWN_VIOLATIONS = frozenset(
    {
        # Rule 4: the two multi-line keys still interpolated with `indent`. R2's PRs 6 and 7
        # move each into a dedicated Secret. home-assistant's from_json key is in a
        # PLACEHOLDER_DEFERRED tag, so no placeholder render reaches it.
        _rule4(
            "authelia", "config-secret.yaml", "Secret/authelia-config", _AUTHELIA_KEY
        ),
        _rule4("pi-peer-backup", "secret.yaml", "Secret/pi-peer-backup-ssh", _PPB_KEY),
        # Rule 5, the indent on each multi-line key, at the same two sites.
        _rule5("authelia", "config-secret.yaml.j2", "filter indent", _AUTHELIA_KEY),
        _rule5("pi-peer-backup", "secret.yaml.j2", "filter indent", _PPB_KEY),
        # Rule 5: HA's from_json parse of the service account. R2's PR 5 drops it.
        _rule5(
            "home-assistant",
            "config/secrets.yaml.j2",
            "filter from_json",
            "google_assistant_service_account",
        ),
        # Rule 5: the two trims in qbittorrent and mosquitto's split. R2's PR 4 drops them.
        _rule5("qbittorrent", "secret.yaml.j2", "filter trim", "mullvad_account"),
        _rule5(
            "qbittorrent",
            "secret.yaml.j2",
            "filter trim",
            "wireguard_interface_private_key",
        ),
        _rule5(
            "mosquitto", "config-secret.yaml.j2", "filter string", "mqtt_password_hash"
        ),
        # Rule 5: the truthiness tests on a token, each rewritten as `is defined` by R2's
        # PR 3. The last one joins its test to a feature flag with `and`.
        _rule5("pi-peer-backup", "secret.yaml.j2", TRUTHINESS, "healthchecks_ping_key"),
        *(
            _rule5("uptime-kuma", "static-monitors.yaml.j2", TRUTHINESS, k)
            for k in _KUMA_TOKENS
        ),
        _rule5(
            "uptime-kuma",
            "static-monitors.yaml.j2",
            "truthiness test",
            "renovate_agent_kuma_push_token",
        ),
    }
)


def _replicas_vars(host_vars: Path = HOST_VARS) -> set[str]:
    """Every `replicas_var` of a `metrics:` item in any host's `containers_list`."""
    return {
        str(item["replicas_var"])
        for path in sorted(host_vars.glob("*.yml"))
        for entry in containers_entries(path)
        for item in entry.get("metrics") or []
        if isinstance(item, dict) and "replicas_var" in item
    }


class VarsLookupSite(NamedTuple):
    """A non-literal `lookup('vars', X)` that reads no secret key.

    Attributes:
        template: The template's path from the repository root.
        argument: `X` as `lookup_argument` writes it.
        names: Returns every variable name `X` can take.
    """

    template: str
    argument: str
    names: Callable[[], set[str]]


# The non-literal `vars` lookups that rule 5 does not count as a secret reference, for any of
# its clauses. Each entry fails the check when its site has gone, or when a name its argument
# can take is a secret key.
NON_SECRET_VARS_LOOKUPS = frozenset(
    {
        # `lookup('vars', job.replicas_var) | int > 0` drops a scrape job whose workload is
        # scaled to zero. `replicas_var` is a field of a `containers_list` entry's `metrics:`
        # list (ansible/filter_plugins/scrape_jobs.py), and names a replica count.
        VarsLookupSite(
            _ROLE_TEMPLATES.format("observability", "prometheus.yaml.j2"),
            "job.replicas_var",
            _replicas_vars,
        ),
    }
)

# The fewest in-scope templates whose syntax tree names a secret key: 59 at `70591c307`.
RULE5_FLOOR = 59


class NamedUse(NamedTuple):
    """A use rule 5 must find and accept on the real tree.

    At least `count` accepted uses of `key` in `template` must each pass through `form`.
    """

    template: str
    key: str
    form: str
    count: int


# The uses rule 5 must accept on the real tree, so a rule that stops matching cannot pass.
RULE5_MEMBERS = (
    NamedUse(
        _ROLE_TEMPLATES.format("uptime-kuma", "secret.yaml.j2"),
        "kuma_status_page_sync_push_token",
        "filter default",
        1,
    ),
    NamedUse(
        _ROLE_TEMPLATES.format("homepage", "config/services.yaml.j2"),
        "domain",
        "argument to homepage_href",
        21,
    ),
    NamedUse(
        _ROLE_TEMPLATES.format("authelia", "config-secret.yaml.j2"),
        "domain",
        "argument to authelia_service_rules",
        1,
    ),
    NamedUse(
        _ROLE_TEMPLATES.format("monitor-bridge", "env-secret.yaml.j2"),
        DYNAMIC_LOOKUP.format("check.token"),
        "output",
        1,
    ),
)


# -- rules 1 to 4: the rendered output ------------------------------------------------------


def output_violations(
    render: PlaceholderRender, known_keys: Iterable[str]
) -> list[Violation]:
    """Every breach of rules 1 to 4 in `render`."""
    known = set(known_keys)
    found = []
    for tag in render.tags:
        for directory, files in tag.directories().items():
            key_set = render.key_sets[directory]
            for name, text in sorted(files.items()):
                for site, doc in documents(text):
                    if SENTINEL_FRAGMENT.search(doc):
                        found.append(
                            Violation(
                                NO_BARE_DOLLAR_BRACE, directory, name, site, "sentinel"
                            )
                        )
                    if not key_set or substitution_disabled(doc):
                        continue
                    found.extend(
                        Violation(rule, directory, name, site, body)
                        for body in references(doc)
                        for rule in _broken(body, site, known, key_set)
                    )
    return found


def _broken(body: str, site: str, known: set[str], keys: frozenset[str]) -> list[str]:
    """Each rule one reference `${body}` in a directory that substitutes breaks.

    Rules 3 and 4 read only a placeholder of pass 3, a reference to a key in `keys`.
    """
    rules = [KNOWN_KEY] if body not in known else []
    if body not in keys:
        return [*rules, NO_BARE_DOLLAR_BRACE]
    if body in SOPS_MANIFEST_KEYS:
        rules.append(NO_SOPS_MANIFEST_KEY)
    if not site.startswith("Secret/") and body not in PLACEHOLDER_ANYWHERE:
        rules.append(CONFINED_TO_SECRET)
    return rules


def _document(render: PlaceholderRender, directory: str, site: str) -> str | None:
    """The text of the document `site` in `directory`, or None when it is not there."""
    for tag in render.tags:
        for name, files in tag.directories().items():
            if name != directory:
                continue
            for text in files.values():
                for found, doc in documents(text):
                    if found == site:
                        return doc
    return None


def output_members(render: PlaceholderRender, known_keys: Iterable[str]) -> list[str]:
    """Each named member of rules 1 to 4 the render lacks, one line apiece.

    A member is asserted only when its directory is in the render, so a `--role` render of
    another tag never fails on it.
    """
    known = set(known_keys)
    missing = []
    rendered = set(render.key_sets)

    def require(
        rule: str, directory: str, site: str, holds: Callable[[str], bool], what: str
    ):
        if directory not in rendered:
            return
        doc = _document(render, directory, site)
        if doc is None or not holds(doc):
            missing.append(f"{rule}: named member gone: {what} in {directory}'s {site}")

    require(
        CONFINED_TO_SECRET,
        "homepage",
        "IngressRoute/homepage",
        lambda doc: "domain" in references(doc),
        "${domain}",
    )
    require(
        KNOWN_KEY,
        "authelia",
        "Secret/authelia-config",
        lambda doc: any(body in known for body in references(doc)),
        "a placeholder",
    )
    require(
        NO_BARE_DOLLAR_BRACE,
        "qbittorrent",
        "ConfigMap/qbittorrent-netns-reset",
        lambda doc: "$${" in doc,
        "an escaped $${",
    )
    for key in sorted(SOPS_MANIFEST_KEYS - known):
        missing.append(
            f"{NO_SOPS_MANIFEST_KEY}: {key} is in SOPS_MANIFEST_KEYS and not in "
            "ansible/vars/secrets.yml"
        )
    return missing


# -- rule 5: the template sources ----------------------------------------------------------


def rule5_templates(roles: Path = K8S_ROLES, shared: Path = SHARED_TPL) -> list[Path]:
    """Every template rule 5 reads: each `.j2` but `*.sh.j2` under the two template roots."""
    paths = [*roles.glob("*/templates/**/*.j2"), *shared.glob("**/*.j2")]
    return sorted(p for p in paths if not p.name.endswith(".sh.j2"))


POLICY = Policy(PASSTHROUGH_FILTERS, SOPS_MANIFEST_KEYS)


def scan_template(
    source: str,
    keys: frozenset[str],
    non_secret: frozenset[str] = frozenset(),
    passthrough: frozenset[str] = PASSTHROUGH_FILTERS,
    env=None,
) -> TemplateScan:
    """Rule 5's walk over one template, with `passthrough` and `SOPS_MANIFEST_KEYS`."""
    env = env or renderer_env(SHARED_TPL)
    policy = Policy(passthrough, SOPS_MANIFEST_KEYS)
    return scan(env, source, keys, policy, non_secret)


def template_tag(path: Path, roles: Path = K8S_ROLES) -> str:
    """The role a template belongs to, or `shared` for `ansible/templates/`."""
    return path.relative_to(roles).parts[0] if path.is_relative_to(roles) else "shared"


@dataclass
class Rule5:
    """Rule 5 over the whole tree.

    Attributes:
        violations: Every refused use.
        naming: The templates whose tree names a secret key.
        accepted: Each accepted use, with its template's path from the repository root.
        lookup_sites: `(template, argument)` for every non-literal `vars` lookup.
    """

    violations: list[Violation] = field(default_factory=list)
    naming: list[str] = field(default_factory=list)
    accepted: list[tuple[str, Use]] = field(default_factory=list)
    lookup_sites: set[tuple[str, str]] = field(default_factory=set)


def rule5(
    keys: Iterable[str],
    paths: list[Path] | None = None,
    non_secret: frozenset[VarsLookupSite] = NON_SECRET_VARS_LOOKUPS,
    root: Path = REPO,
) -> Rule5:
    """Run rule 5 over `paths`, by default every template it reads."""
    keyset = frozenset(keys)
    env = renderer_env(SHARED_TPL)
    result = Rule5()
    for path in rule5_templates() if paths is None else paths:
        rel = path.relative_to(root).as_posix()
        allowed = frozenset(s.argument for s in non_secret if s.template == rel)
        scan = scan_template(path.read_text(), keyset, allowed, env=env)
        if scan.names_key:
            result.naming.append(rel)
        result.lookup_sites.update((rel, arg) for arg in scan.lookup_sites)
        for use in scan.uses:
            if use.refused:
                result.violations.append(
                    Violation(
                        NO_SECRET_EXPRESSION,
                        template_tag(path),
                        rel,
                        use.forms[-1],
                        use.key,
                    )
                )
            else:
                result.accepted.append((rel, use))
    return result


def rule5_members(
    result: Rule5,
    keys: Iterable[str],
    non_secret: frozenset[VarsLookupSite] = NON_SECRET_VARS_LOOKUPS,
    members: tuple[NamedUse, ...] = RULE5_MEMBERS,
    floor: int = RULE5_FLOOR,
) -> list[str]:
    """Each named member, floor or `NON_SECRET_VARS_LOOKUPS` entry rule 5 fails, one line apiece."""
    problems = []
    if len(result.naming) < floor:
        problems.append(
            f"{NO_SECRET_EXPRESSION}: {len(result.naming)} templates name a secret key, "
            f"under the floor of {floor}"
        )
    for member in members:
        found = sum(
            1
            for rel, use in result.accepted
            if rel == member.template
            and use.key == member.key
            and member.form in use.forms
        )
        if found < member.count:
            problems.append(
                f"{NO_SECRET_EXPRESSION}: named member gone: {member.count} accepted uses of "
                f"{member.key} through {member.form} in {member.template}, found {found}"
            )
    known = set(keys)
    for site in sorted(non_secret):
        if (site.template, site.argument) not in result.lookup_sites:
            problems.append(
                f"{NO_SECRET_EXPRESSION}: stale NON_SECRET_VARS_LOOKUPS entry: no "
                f"lookup('vars', {site.argument}) in {site.template}"
            )
        secret = sorted(site.names() & known)
        if secret:
            problems.append(
                f"{NO_SECRET_EXPRESSION}: NON_SECRET_VARS_LOOKUPS entry {site.template} "
                f"lookup('vars', {site.argument}) can read the secret keys {secret}"
            )
    return problems


# -- the check -------------------------------------------------------------------------------


@dataclass
class Report:
    """The verdict of the five rules on one placeholder render.

    Attributes:
        violations: Every violation found, known ones included.
        unknown: The violations `KNOWN_VIOLATIONS` does not hold.
        stale: The `KNOWN_VIOLATIONS` entries that no longer match.
        missing: Each named member, floor or allowlist entry that failed, one line apiece.
    """

    violations: list[Violation]
    unknown: list[Violation]
    stale: list[Violation]
    missing: list[str]

    def ok(self) -> bool:
        return not (self.unknown or self.stale or self.missing)

    def lines(self) -> list[str]:
        lines = [f"secret check: violation: {v.line()}" for v in self.unknown]
        lines += [
            f"secret check: stale KNOWN_VIOLATIONS entry, remove it: {v.line()}"
            for v in self.stale
        ]
        lines += [f"secret check: {m}" for m in self.missing]
        return lines


def check(
    render: PlaceholderRender,
    known_keys: Iterable[str],
    known_violations: frozenset[Violation] = KNOWN_VIOLATIONS,
    rule5_result: Rule5 | None = None,
) -> Report:
    """Run the five rules on `render` and hold the result to `known_violations`.

    Args:
        render: A placeholder render, after all three passes.
        known_keys: The key names of `ansible/vars/secrets.yml`.
        known_violations: The ratchet.
        rule5_result: Rule 5's result, when the caller has it already.
    """
    keys = list(known_keys)
    result = rule5(keys) if rule5_result is None else rule5_result
    violations = output_violations(render, keys) + result.violations
    found = set(violations)
    rendered = set(render.key_sets)
    stale = sorted(
        v
        for v in known_violations - found
        if v.rule == NO_SECRET_EXPRESSION or v.tag in rendered
    )
    unknown = sorted({v for v in violations if v not in known_violations})
    missing = output_members(render, keys) + rule5_members(result, keys)
    return Report(violations, unknown, stale, missing)
