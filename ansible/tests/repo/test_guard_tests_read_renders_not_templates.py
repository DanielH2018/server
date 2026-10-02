"""A guard under a SCANNED test directory asserts on a RENDER, never on a template's text.

#2809 settled this as prose — "no `ansible/tests/services` test asserts a literal copied from
a template" — which every session re-derives by scanning the directory, and which a new guard
written against a template's bytes passes. This module is that sentence as a check: no module
in a directory `SCANNED` names reads a path under a `templates/` directory, except an entry in
`TEMPLATE_SOURCE_READERS` carrying the reason the source text is the right thing to read.

`SCANNED` grows one directory at a time, and a directory joins it only once its own readers
are converted (#3107). `services/` joined with six permanent entries; `longhorn/` joined with
none, its one reader — the daily RecurringJob's group — now read out of `_setup_render`, and
`repo/` — the directory this module itself sits in, so a new guard written here is held to the
rule — joined with one, which keys on a variable NAME. `deploy/` joined with two (#3204):
the raw-byte hash the render stamp compares, and the autodeploy derivation's source fallback
for synthetic role trees no render reaches. The directories still outside it (`setup/`,
`k8s/`, `staging/`) each hold ten or more readers, so adding one is a PR of its own rather
than a wider glob here. Entries are keyed
`<directory>/<module>.py`, because a basename alone would let a module in one directory
inherit another's exemption.

The exposure is the same one `scripts/tests/test_tests_share_render_and_path_helpers.py`'s
rule 3 covers for `*.sh.j2` repo-wide: a value that moves into a role default leaves the
assertion matching `{{ ... }}`, and a pattern that matches nothing passes (#3178). The
sanctioned readers are `_k8s_render` (k8s manifests and `Dockerfile*.j2`), `_compose_render`
(the Pi's Compose and config templates) and `_shell_render` (`*.sh.j2`).

Two things the rule deliberately does not reach:

* **A role's `files/` copy.** `uptime-kuma/files/*.liquid` ships verbatim, so there is no
  render to read and the text IS the artifact. `test_kuma_email_template.py` and
  `test_kuma_discord_template.py` read one each, and neither names a `templates/` path.
#3196 named one blind spot, `test_healthchecks_pings.py`, whose `ANSIBLE.rglob("*")` census
put no template path at its read site. #3190 converted that census to a render in the same
wave, and the control read it kept IS visible here — so the module is a listed reader rather
than a declared gap, and no blind-spot map is needed.

Run: uv run pytest ansible/tests/repo/test_guard_tests_read_renders_not_templates.py
"""

import ast
from pathlib import Path

from lib.repo_paths import ANSIBLE

TESTS = ANSIBLE / "tests"
# Every directory the rule covers. Adding one means converting its readers first.
SCANNED = (
    TESTS / "services",
    TESTS / "longhorn",
    TESTS / "repo",
    TESTS / "deploy",
)

# Modules that read a template's SOURCE, each with why a render cannot answer the question.
# Every entry is permanent: a render erases the thing it reads.
TEMPLATE_SOURCE_READERS = {
    "services/_kuma_entities.py": (
        "the `*_push_token` NAMES are a render INPUT — they decide which gated tiles render "
        "at all, so they are read before the render rather than out of it"
    ),
    "services/test_n8n_build_is_pinned.py": (
        "Renovate's `matchString` runs against `Dockerfile.j2` on disk, braces intact; a "
        "render is not what Renovate sees"
    ),
    "services/test_nut_fsd_login_confined.py": (
        "which ROLES name `nut_monitor_password` — every render of that secret is the same "
        "`STUB`, so the census has to key on the variable name"
    ),
    "services/test_smtp_wiring.py": (
        "a `# DECIDED:` comment in authelia's config-secret template, which no render carries"
    ),
    "services/test_strangler_bridge.py": (
        "the ABSENCE of a retired variable name (`bridge_hostname`) across every template, "
        "which a render cannot show — an undefined name renders as nothing"
    ),
    "repo/test_secret_rendering_host_scripts_have_no_log.py": (
        "which VARIABLE NAMES a `*.sh.j2` references, to decide whether its template task "
        "needs `no_log`; a render replaces every secret with the same `STUB` and drops the "
        "name that identified it"
    ),
    "services/test_healthchecks_pings.py": (
        "the control that keeps its render honest asserts a template CARRIES `{{`, which is "
        "the one claim a render erases; the census itself renders (#3190)"
    ),
    "deploy/_autodeploy.py": (
        "the fallback for a role no render reaches: the synthetic `widget_role` trees under "
        "`tmp_path` the derivation's unit tests build, and the caller-rendered roles "
        "`_k8s_render` skips, all denylisted. Every role the render reaches reads its render, "
        "and `test_the_render_reaches_every_auto_deployable_role` holds the denylisted half"
    ),
    "deploy/test_setup_render_manifest.py": (
        "hashes the template's raw BYTES — a trailing newline and a truncated-read "
        "comparison, neither of which survives a render"
    ),
}

# Modules the census must reach. An empty or partial scan means the walk stopped matching
# rather than that the directory is clean.
KNOWN_MEMBERS = frozenset(
    {
        "services/_homepage_config.py",
        "services/_jellyfin_plugins.py",
        "services/_kuma_entities.py",
        "services/test_authelia_access_tiers.py",
        "services/test_healthchecks_pings.py",
        "services/test_kuma_static_monitors.py",
        "services/test_monitor_bridge_modules.py",
        "services/test_smtp_wiring.py",
        "services/test_strangler_bridge.py",
        "longhorn/_restore_drill.py",
        "longhorn/test_daily_group_membership_is_the_r2_list.py",
        "longhorn/test_longhorn_restore_drill_byte_floor.py",
        "longhorn/test_prune_backups.py",
        "repo/test_guard_tests_read_renders_not_templates.py",
        "repo/test_secret_rendering_host_scripts_have_no_log.py",
        "repo/test_testpaths_covers_every_test_file.py",
        "deploy/_autodeploy.py",
        "deploy/test_gitops_manual_trigger.py",
        "deploy/test_k8s_autodeploy_rollout_gates.py",
        "deploy/test_setup_render_manifest.py",
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


def _modules() -> dict[str, Path]:
    """Every scanned module, by its `<directory>/<module>.py` key."""
    return {
        f"{directory.name}/{p.name}": p
        for directory in SCANNED
        for p in sorted(directory.glob("*.py"))
        if p.name != "__init__.py"
    }


def test_the_census_reaches_every_known_module():
    missing = KNOWN_MEMBERS - set(_modules())
    assert not missing, f"the scan no longer reaches: {sorted(missing)}"


def test_every_listed_module_still_exists():
    """A stale entry lets a whole module back out of the rule."""
    found = _modules()
    gone = sorted(name for name in TEMPLATE_SOURCE_READERS if name not in found)
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
    found = offenders({key: p.read_text() for key, p in _modules().items()})
    assert found == {}, (
        "these tests assert on a template's SOURCE, so a value moved into a role default "
        "leaves the assertion matching `{{ ... }}`. Render it instead — `_k8s_render` for a "
        "manifest or a `Dockerfile*.j2`, `_compose_render` for the Pi's templates, "
        "`_shell_render` for a `*.sh.j2` — or add an entry to TEMPLATE_SOURCE_READERS saying "
        f"why the source text is the subject: {found}"
    )


def test_every_exempt_module_is_still_flagged():
    """Non-vacuity: a renamed exemption must fail here rather than pass by matching nothing."""
    found = _modules()
    unflagged = sorted(
        name
        for name in TEMPLATE_SOURCE_READERS
        if not template_source_reads(found[name].read_text())
    )
    assert unflagged == [], (
        "these modules no longer read a template's source — drop them from "
        f"TEMPLATE_SOURCE_READERS: {unflagged}"
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
            "services/test_new_guard.py": 'T = K8S_ROLES / "n8n" / "templates" / "Dockerfile.j2"\n'
            'assert "npm" in T.read_text()\n',
            "services/test_already_clean.py": "from _k8s_render import rendered_texts\n"
            'assert "npm" in rendered_texts("n8n")["Dockerfile.j2"]\n',
        }
    )
    assert list(found) == ["services/test_new_guard.py"], found


def test_a_new_longhorn_guard_reading_a_role_template_is_named_as_an_offender():
    """The directory added in #3107, and the reason the keys carry one.

    `services/test_smtp_wiring.py` is exempt; a `longhorn/` module of the same basename is
    not, so a basename-keyed exemption would have waved this read through.
    """
    source = (
        'T = SETUP_ROLES / "k3s" / "templates" / "longhorn-recurringjob.yaml.j2"\n'
        'assert "default" in T.read_text()\n'
    )
    found = offenders(
        {
            "longhorn/test_new_guard.py": source,
            "longhorn/test_smtp_wiring.py": source,
        }
    )
    assert sorted(found) == [
        "longhorn/test_new_guard.py",
        "longhorn/test_smtp_wiring.py",
    ], found


def test_a_reverted_deploy_guard_is_named_as_an_offender():
    """The directory added in #3204, against the spelling its manual-trigger guard used to take.

    `test_gitops_manual_trigger.py` read the polkit rule's source until #3204; putting that
    read back must fail the census rather than pass under a directory it does not scan.
    """
    found = offenders(
        {
            "deploy/test_gitops_manual_trigger.py": (
                '_RULE = _ROLE / "templates/50-gitops-deploy.rules.j2"\n'
                'assert "polkit.Result.YES" in _RULE.read_text()\n'
            ),
            "deploy/test_already_clean.py": (
                "from _setup_render import rendered_setup_text\n"
                'assert "YES" in rendered_setup_text("gitops_deploy", "50-gitops-deploy.rules.j2")\n'
            ),
        }
    )
    assert list(found) == ["deploy/test_gitops_manual_trigger.py"], found
