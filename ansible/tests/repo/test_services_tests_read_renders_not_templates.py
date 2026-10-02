"""A guard under `ansible/tests/services/` asserts on a RENDER, never on a template's text.

#2809 settled this as prose — "no `ansible/tests/services` test asserts a literal copied from
a template" — which every session re-derives by scanning the directory, and which a new guard
written against a template's bytes passes. This module is that sentence as a check: no module
in `ansible/tests/services/` reads a path under a `templates/` directory, except an entry in
`TEMPLATE_SOURCE_READERS` carrying the reason the source text is the right thing to read.

The exposure is the same one `scripts/tests/test_tests_share_render_and_path_helpers.py`'s
rule 3 covers for `*.sh.j2` repo-wide: a value that moves into a role default leaves the
assertion matching `{{ ... }}`, and a pattern that matches nothing passes (#3178). The
sanctioned readers are `_k8s_render` (k8s manifests and `Dockerfile*.j2`), `_compose_render`
(the Pi's Compose and config templates) and `_shell_render` (`*.sh.j2`).

Two things the rule deliberately does not reach:

* **A role's `files/` copy.** `uptime-kuma/files/*.liquid` ships verbatim, so there is no
  render to read and the text IS the artifact. `test_kuma_email_template.py` and
  `test_kuma_discord_template.py` read one each, and neither names a `templates/` path.
* **A census wider than a template glob.** `test_healthchecks_pings.py` walks
  `ANSIBLE.rglob("*")` and filters by suffix, so no template path appears at its read site at
  all. `WIDE_CENSUS_BLIND_SPOTS` names it with that reason, and asserts it does NOT flag — a
  declared gap rather than a silent one.

Run: uv run pytest ansible/tests/repo/test_services_tests_read_renders_not_templates.py
"""

import ast
from pathlib import Path

from lib.repo_paths import ANSIBLE

SERVICES = ANSIBLE / "tests" / "services"

# Modules that read a template's SOURCE, each with why a render cannot answer the question.
# Every entry is permanent: a render erases the thing it reads.
TEMPLATE_SOURCE_READERS = {
    "_kuma_entities.py": (
        "the `*_push_token` NAMES are a render INPUT — they decide which gated tiles render "
        "at all, so they are read before the render rather than out of it"
    ),
    "test_n8n_build_is_pinned.py": (
        "Renovate's `matchString` runs against `Dockerfile.j2` on disk, braces intact; a "
        "render is not what Renovate sees"
    ),
    "test_nut_fsd_login_confined.py": (
        "which ROLES name `nut_monitor_password` — every render of that secret is the same "
        "`STUB`, so the census has to key on the variable name"
    ),
    "test_smtp_wiring.py": (
        "a `# DECIDED:` comment in authelia's config-secret template, which no render carries"
    ),
    "test_strangler_bridge.py": (
        "the ABSENCE of a retired variable name (`bridge_hostname`) across every template, "
        "which a render cannot show — an undefined name renders as nothing"
    ),
}

# Readers the detector cannot see, each with the reason, asserted below to stay unflagged so a
# gap that closes is noticed rather than left as a passing exemption.
WIDE_CENSUS_BLIND_SPOTS = {
    "test_healthchecks_pings.py": (
        "filters `ANSIBLE.rglob('*')` by a suffix tuple that holds `.j2`, so no template path "
        "appears at the read site"
    ),
}

# Modules the census must reach. An empty or partial scan means the walk stopped matching
# rather than that the directory is clean.
KNOWN_MEMBERS = frozenset(
    {
        "_homepage_config.py",
        "_jellyfin_plugins.py",
        "_kuma_entities.py",
        "test_authelia_access_tiers.py",
        "test_healthchecks_pings.py",
        "test_kuma_static_monitors.py",
        "test_monitor_bridge_modules.py",
        "test_smtp_wiring.py",
        "test_strangler_bridge.py",
    }
)

_READ_METHODS = frozenset({"read_text", "read_bytes", "open"})
_GLOB_METHODS = frozenset({"glob", "rglob"})


def _names_a_template_path(node: ast.expr) -> bool:
    """Whether `node` builds a path that goes through a `templates/` directory or ends in `.j2`.

    A segment literal (`"templates"`), a slashed fragment (`"roles/k8s/x/templates/y.j2"`) and
    a bare `.j2` filename at the end of a path expression all count. A glob PATTERN is handled
    by `_glob_names_templates` instead: `"*.j2"` is a pattern, not a name.
    """
    for inner in ast.walk(node):
        if not (isinstance(inner, ast.Constant) and isinstance(inner.value, str)):
            continue
        value = inner.value
        if "*" in value:
            continue
        if (
            value == "templates"
            or "templates/" in value
            or value.endswith("/templates")
        ):
            return True
        if value.endswith(".j2"):
            return True
    return False


def _glob_names_templates(node: ast.Call) -> bool:
    """Whether a `glob`/`rglob` call enumerates templates, by its pattern."""
    return any(
        isinstance(a, ast.Constant)
        and isinstance(a.value, str)
        and (a.value.endswith(".j2") or "templates" in a.value)
        for a in node.args
    )


def _bindings(tree: ast.Module) -> dict[str, str]:
    """The names `tree` binds to a template path, by the expression each is bound to.

    Covers the four spellings a reader takes: an assignment to a path expression, a `for` over
    a template glob, the same `for` inside a comprehension, and a `for` over a TUPLE of names
    already bound this way — `_kuma_entities.py` reads its two templates that last way, which
    is why the resolution runs to a fixed point rather than in one pass.

    A path built under `tmp_path` is the module's own fixture rather than a deployed template,
    so it never binds: `test_nut_fsd_login_confined.py` lays out synthetic role trees.
    """
    pairs: list[tuple[list[ast.expr], ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            pairs.append((node.targets, node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            pairs.append(([node.target], node.value))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            pairs.append(([node.target], node.iter))

    bound: dict[str, str] = {}
    while True:
        grew = False
        for targets, value in pairs:
            text = ast.unparse(value)
            if "tmp_path" in text:
                continue
            hit = (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr in _GLOB_METHODS
                and _glob_names_templates(value)
            )
            if not hit and isinstance(value, (ast.Tuple, ast.List, ast.Set)):
                names = [e.id for e in value.elts if isinstance(e, ast.Name)]
                hit = len(names) == len(value.elts) > 0 and all(
                    name in bound for name in names
                )
            if not hit:
                hit = _names_a_template_path(value)
            if not hit:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in bound:
                    bound[target.id] = text
                    grew = True
        if not grew:
            return bound


def template_source_reads(source: str) -> list[str]:
    """The expressions in `source` that read a template's SOURCE text, by how they spell it."""
    tree = ast.parse(source)
    bound = _bindings(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
            target = node.args[0]
        elif isinstance(node.func, ast.Attribute) and node.func.attr in _READ_METHODS:
            target = node.func.value
        else:
            continue
        text = ast.unparse(target)
        if "tmp_path" in text:
            continue
        if text in bound or _names_a_template_path(target):
            found.append(ast.unparse(node))
    return sorted(set(found))


def _modules() -> list[Path]:
    return sorted(p for p in SERVICES.glob("*.py") if p.name != "__init__.py")


def test_the_census_reaches_every_known_module():
    found = {p.name for p in _modules()}
    missing = KNOWN_MEMBERS - found
    assert not missing, f"the scan no longer reaches: {sorted(missing)}"


def test_every_listed_module_still_exists():
    """A stale entry lets a whole module back out of the rule, or hides a closed blind spot."""
    listed = set(TEMPLATE_SOURCE_READERS) | set(WIDE_CENSUS_BLIND_SPOTS)
    gone = sorted(name for name in listed if not (SERVICES / name).exists())
    assert gone == [], f"listed modules that no longer exist: {gone}"


def offenders(sources: dict[str, str]) -> dict[str, list[str]]:
    """The modules in `sources` (name -> text) that read a template and are not exempt.

    Takes the sources rather than reading the directory, so the red proof below can hand it a
    module that does not exist on disk: a gate only ever observed passing is a gate with no
    evidence it can fail.
    """
    return {
        name: reads
        for name, text in sorted(sources.items())
        if name not in TEMPLATE_SOURCE_READERS
        and (reads := template_source_reads(text))
    }


def test_no_services_test_reads_a_templates_source():
    found = offenders({p.name: p.read_text() for p in _modules()})
    assert found == {}, (
        "these tests assert on a template's SOURCE, so a value moved into a role default "
        "leaves the assertion matching `{{ ... }}`. Render it instead — `_k8s_render` for a "
        "manifest or a `Dockerfile*.j2`, `_compose_render` for the Pi's templates, "
        "`_shell_render` for a `*.sh.j2` — or add an entry to TEMPLATE_SOURCE_READERS saying "
        f"why the source text is the subject: {found}"
    )


def test_every_exempt_module_is_still_flagged():
    """Non-vacuity: a renamed exemption must fail here rather than pass by matching nothing."""
    unflagged = sorted(
        name
        for name in TEMPLATE_SOURCE_READERS
        if not template_source_reads((SERVICES / name).read_text())
    )
    assert unflagged == [], (
        "these modules no longer read a template's source — drop them from "
        f"TEMPLATE_SOURCE_READERS: {unflagged}"
    )


def test_every_declared_blind_spot_is_still_blind():
    """A gap that closes becomes a rule, not a permanent note nobody rereads."""
    now_flagged = sorted(
        name
        for name in WIDE_CENSUS_BLIND_SPOTS
        if template_source_reads((SERVICES / name).read_text())
    )
    assert now_flagged == [], (
        "the detector now reaches these — move them from WIDE_CENSUS_BLIND_SPOTS to "
        f"TEMPLATE_SOURCE_READERS, or convert them: {now_flagged}"
    )


def test_a_template_source_read_is_flagged_in_each_spelling():
    for source in (
        'T = K8S_ROLES / "n8n" / "templates" / "Dockerfile.j2"\nT.read_text()\n',
        'T = ANSIBLE / "roles/k8s/uptime-kuma/templates/x.yaml.j2"\nT.read_bytes()\n',
        '(K8S_ROLES / "authelia" / "templates" / "c.yaml.j2").read_text()\n',
        'for p in ROLES.glob("*/*/templates/**/*.j2"):\n    p.read_text()\n',
        'TEXT = [p.read_text() for p in ROOT.rglob("*.j2")]\n',
        'A = ROLES / "k8s/a/templates/x.j2"\nB = ROLES / "k8s/b/templates/y.j2"\n'
        "for t in (A, B):\n    t.read_text()\n",
        'open(ROLES / "k8s" / "a" / "templates" / "x.j2")\n',
    ):
        assert template_source_reads(source) != [], source


def test_a_render_and_a_fixture_are_not_flagged():
    assert (
        template_source_reads(
            "from _k8s_render import rendered_texts\n"
            'TEXT = rendered_texts("authelia")["config-secret.yaml.j2"]\nTEXT.count("x")\n'
        )
        == []
    )
    # A role's `files/` copy ships verbatim: there is no render, so the text is the artifact.
    assert (
        template_source_reads(
            'T = ANSIBLE / "roles/k8s/uptime-kuma/files/email-message.liquid"\n'
            "T.read_text()\n"
        )
        == []
    )
    # A synthetic template the module wrote itself.
    assert (
        template_source_reads(
            'tpl = tmp_path / "templates" / "x.j2"\ntpl.write_text("")\ntpl.read_text()\n'
        )
        == []
    )
    # An inventory read is not a template read.
    assert (
        template_source_reads(
            'HOST_VARS = ANSIBLE / "inventory" / "host_vars"\n'
            'for f in HOST_VARS.glob("*.yml"):\n    f.read_text()\n'
        )
        == []
    )


def test_a_new_module_reading_a_role_template_is_named_as_an_offender():
    """The verify-by of #3196, run against a module that is not on disk."""
    found = offenders(
        {
            "test_new_guard.py": 'T = K8S_ROLES / "n8n" / "templates" / "Dockerfile.j2"\n'
            'assert "npm" in T.read_text()\n',
            "test_already_clean.py": "from _k8s_render import rendered_texts\n"
            'assert "npm" in rendered_texts("n8n")["Dockerfile.j2"]\n',
        }
    )
    assert list(found) == ["test_new_guard.py"], found
