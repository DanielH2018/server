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

`sops-manifest-entries`, the check on each role's `render_sops_manifests` list, lives in
`sops_entries.py` and is importable from here.

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


from lib.repo_paths import REPO, SHARED_TPL
from renderer.renderer_lib.environment import renderer_env
from renderer.renderer_lib.jinja_aliases import scan_templates
from renderer.renderer_lib.jinja_uses import (
    DYNAMIC_LOOKUP,
    Policy,
    TemplateScan,
    Use,
    embed_refusals,
    resolve_embed,
    rule5_templates,
    template_tag,
)
from renderer.renderer_lib.var_aliases import (
    ALIAS_MEMBERS,
    NON_SECRET_VARS_LOOKUPS as NON_SECRET_VARS_LOOKUPS,
    NOT_ALIASES,
    VarsLookupSite as VarsLookupSite,
    not_alias_problems,
    rule5_var_files,
    var_aliases,
    var_tag,
)
from renderer.renderer_lib.sops_entries import (
    SOPS_MANIFEST_KEYS,
    sops_manifest_entries as sops_manifest_entries,
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


_AUTHELIA_KEY, _PPB_KEY = "authelia_oidc_rsa_key_content", "pi_peer_backup_ssh_key"

# Every violation the first fleet render finds. Each entry leaves through the PR that fixes
# its source, and an entry that no longer matches fails the check, so the set shrinks only
# through a fix. R2's exit criterion requires it empty.
KNOWN_VIOLATIONS = frozenset(
    {
        # Rule 4: the two multi-line keys still interpolated with `indent`. R2's PRs 6 and 7
        # move each into a dedicated Secret. home-assistant's key already sits in its own
        # Secret, listed in `render_sops_manifests` (R2's PR 5).
        _rule4(
            "authelia", "config-secret.yaml", "Secret/authelia-config", _AUTHELIA_KEY
        ),
        _rule4("pi-peer-backup", "secret.yaml", "Secret/pi-peer-backup-ssh", _PPB_KEY),
        # Rule 5, the indent on each multi-line key, at the same two sites.
        _rule5("authelia", "config-secret.yaml.j2", "filter indent", _AUTHELIA_KEY),
        _rule5("pi-peer-backup", "secret.yaml.j2", "filter indent", _PPB_KEY),
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
        # Rule 5: the `observability_prometheus_config` set block outputs `{{ domain }}`, so it
        # is an alias of `domain`, and the Deployment hashes it with `hash('sha1')`. Under Flux
        # the hash would cover the placeholder, so a `domain` change would not restart
        # Prometheus. #4952; R2's PR 8 moves the key out of the hashed text.
        _rule5("observability", "prometheus.yaml.j2", "filter hash", "domain"),
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
    *(NamedUse(*member) for member in ALIAS_MEMBERS),
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
    path = Path("template.j2")
    return scan_templates({path: source}, keys, policy, env, {path: non_secret})[path]


@dataclass
class Rule5:
    """Rule 5 over the whole tree.

    Attributes:
        violations: Every refused use.
        naming: The templates whose tree names a secret key.
        accepted: Each accepted use, with its template's path from the repository root.
        lookup_sites: `(template, argument)` for every non-literal `vars` lookup.
        bindings: `(template, name, is an alias)` for each name a template binds.
    """

    violations: list[Violation] = field(default_factory=list)
    naming: list[str] = field(default_factory=list)
    accepted: list[tuple[str, Use]] = field(default_factory=list)
    lookup_sites: set[tuple[str, str]] = field(default_factory=set)
    bindings: set[tuple[str, str, bool]] = field(default_factory=set)


def rule5(
    keys: Iterable[str],
    paths: list[Path] | None = None,
    non_secret: frozenset[VarsLookupSite] = NON_SECRET_VARS_LOOKUPS,
    root: Path = REPO,
    var_files: list[Path] | None = None,
) -> Rule5:
    """Run rule 5 over `paths`, by default every template it reads, embeds included.

    `var_files` are the role defaults and inventory files the alias clause reads. They
    default to the real ones when `paths` does too, and to none otherwise.
    """
    keyset = frozenset(keys)
    env = renderer_env(SHARED_TPL)
    policy = Policy(PASSTHROUGH_FILTERS, SOPS_MANIFEST_KEYS)
    if var_files is None:
        var_files = rule5_var_files() if paths is None else []
    paths = rule5_templates() if paths is None else paths
    aliases = var_aliases(keyset, policy, var_files, env)
    result = Rule5([_rule5_violation(*r, root, var_tag) for r in aliases.refused])
    rels = {path: path.relative_to(root).as_posix() for path in paths}
    allowed = {
        path: frozenset(s.argument for s in non_secret if s.template == rel)
        for path, rel in rels.items()
    }
    sources = {path: path.read_text() for path in paths}
    scans = scan_templates(
        sources, keyset, policy, env, allowed, aliases=aliases.bindings
    )
    for path, scan in scans.items():
        rel = rels[path]
        result.bindings.update((rel, name, alias) for name, alias in scan.bindings)
        if scan.names_key:
            result.naming.append(rel)
        result.lookup_sites.update((rel, arg) for arg in scan.lookup_sites)
        refused = [(path, u.key, u.forms[-1]) for u in scan.uses if u.refused]
        result.violations.extend(_rule5_violation(*r, root) for r in refused)
        result.accepted.extend((rel, u) for u in scan.uses if not u.refused)
    embeds = embed_refusals(scans, resolve_embed, SOPS_MANIFEST_KEYS)
    result.violations.extend(_rule5_violation(*r, root) for r in embeds)
    return result


def _rule5_violation(path: Path, name: str, form: str, root: Path, tag=template_tag):
    rel = path.relative_to(root).as_posix()
    return Violation(NO_SECRET_EXPRESSION, tag(path), rel, form, name)


def rule5_members(
    result: Rule5,
    keys: Iterable[str],
    non_secret: frozenset[VarsLookupSite] = NON_SECRET_VARS_LOOKUPS,
    members: tuple[NamedUse, ...] = RULE5_MEMBERS,
    floor: int = RULE5_FLOOR,
    not_aliases: tuple[tuple[str, str], ...] = NOT_ALIASES,
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
    problems += not_alias_problems(result.bindings, not_aliases)
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
