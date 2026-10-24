"""`promote.py` reaches GitHub and GHCR only through `forge.py`, and lists no tags.

Two properties of the boundary. The import census reads `promote.py`'s imports: no HTTP
library, and no `subprocess`, so it cannot start `gh` itself. The request count runs `decide`
against a registry holding 3,000 `sha-*` tags and counts what it asked ghcr.io for.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_forge_boundary.py
"""

import ast
from pathlib import Path

import pytest

import forge
import promote
from _promote_fakes import World, digest_of

PROMOTE = Path(promote.__file__)
# The modules split out of promote.py, read from disk so a fourth is held to the boundary too.
PROMOTE_LIB = sorted(PROMOTE.with_name("promote_lib").glob("*.py"))
# The members the glob must find, so a rename or a move out of the package fails by name
# rather than leaving the census over fewer modules.
PROMOTE_LIB_MEMBERS = frozenset(
    {
        "wait.py",
        "verify_promotion.py",
        "prune.py",
        "forward_only.py",
        "reads.py",
        "ci_contexts.py",
        "stateful.py",
        "reboot_window.py",
        "reset.py",
        "wait_reads.py",
    }
)

# The modules that read only the checkout or the handoff directory, or compute from what they
# are passed, so they have no forge call to route and need not import the adapter. The
# forbidden imports still hold for them.
CHECKOUT_ONLY = frozenset(
    {
        "controller_rollout.py",
        "promotion.py",
        "reboot_window.py",
        "storage_rollout.py",
        "verify_handoff.py",
    }
)
# The modules that would let promote.py make a forge call without the adapter. `subprocess`
# and `pty` are here because `gh` and `cosign` are forge calls too.
FORBIDDEN = (
    "urllib",
    "http",
    "requests",
    "httpx",
    "urllib3",
    "socket",
    "subprocess",
    "pty",
)

# The `lib` modules promote.py may import. An allowlist rather than a denylist, because several
# `lib` modules start subprocesses of their own (`lib.gh` runs `gh`, `lib.kubectl` and `lib.b2`
# reach other services), and a new one would pass a denylist unseen. `lib.git` runs only git.
ALLOWED_LIB = frozenset(
    {
        "lib.cli_help",
        "lib.exit_codes",
        "lib.git",
        "lib.removes_object",
        "lib.repo_paths",
        "lib.yaml_fast",
    }
)

# No `deployer_lib` module: `ci_verdict` moved to `hosts/lib/` in H7.2e, and the rest of the
# deployer reads GitHub's check-runs API through `urlopen` (`deploy_toolbox.fetch_ci_verdict`)
# or posts to webhooks (`deploy_alerts`).
ALLOWED_DEPLOYER_LIB: frozenset[str] = frozenset()

# Each package whose imports are held to an allowlist, by the package name.
ALLOWLISTS = {"lib": ALLOWED_LIB, "deployer_lib": ALLOWED_DEPLOYER_LIB}

# The `os` functions that start a process, which promote.py's `import os` would reach.
_OS_EXEC = ("system", "popen", "exec", "spawn", "posix_spawn", "fork")

# The adapter itself: the one member the census must find, so a rename that leaves promote.py
# importing nothing cannot pass as "imports nothing forbidden".
ADAPTER = "forge"


def imported_modules(source: str) -> set[str]:
    """Every module name an `import` or `from ... import` in `source` names.

    `from lib import yaml_fast` names `lib.yaml_fast`, so an allowlist in ALLOWLISTS sees the
    module rather than the package.
    """
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module in ALLOWLISTS:
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _os_exec_uses(source: str) -> set[str]:
    """Each `os.<exec>` attribute and `from os import <exec>` name in `source`."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        ):
            name = node.attr
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            found.update(
                f"os.{alias.name}"
                for alias in node.names
                if alias.name.startswith(_OS_EXEC)
            )
            continue
        else:
            continue
        if name.startswith(_OS_EXEC):
            found.add(f"os.{name}")
    return found


def forbidden_imports(source: str) -> set[str]:
    """Every import or `os` call in `source` that could reach a forge without the adapter."""
    modules = imported_modules(source)
    return (
        {name for name in modules if name.split(".")[0] in FORBIDDEN}
        | {
            name
            for name in modules
            if name.split(".")[0] in ALLOWLISTS
            and name not in ALLOWLISTS[name.split(".")[0]]
        }
        | _os_exec_uses(source)
    )


def test_the_census_finds_every_promote_lib_member():
    assert PROMOTE_LIB_MEMBERS <= {path.name for path in PROMOTE_LIB}


@pytest.mark.parametrize(
    "module",
    [PROMOTE, *PROMOTE_LIB],
    ids=lambda p: p.relative_to(PROMOTE.parent).as_posix(),
)
def test_promote_imports_the_adapter_and_no_http_library_is_clean(module):
    """Each module split out of promote.py holds the same boundary: `promote_lib/wait.py`
    (`land-wait`), whose kubectl and logger go through `flux_host.py`; `promote_lib/verify_promotion.py`
    (`verify`), whose status reads go through forge; `promote_lib/prune.py` (`prune`);
    `promote_lib/forward_only.py`, whose PR and status reads are `promote.Reads` fields;
    `promote_lib/reads.py`, which builds those fields over forge; and
    `promote_lib/ci_contexts.py` (`ci-verdict`). CHECKOUT_ONLY makes no forge call."""
    source = module.read_text(encoding="utf-8")
    if module.name not in CHECKOUT_ONLY:
        assert ADAPTER in imported_modules(source)
    assert forbidden_imports(source) == set()


def test_a_module_importing_the_http_library_itself_is_flagged():
    source = "import forge\nimport urllib.request\n\nurllib.request.urlopen('https://ghcr.io')\n"
    assert forbidden_imports(source) == {"urllib.request"}


def test_a_module_starting_gh_itself_is_flagged():
    source = (
        "import forge\nfrom subprocess import run\n\nrun(['gh', 'variable', 'get'])\n"
    )
    assert forbidden_imports(source) == {"subprocess"}


@pytest.mark.parametrize(
    "source, flagged",
    [
        ("from lib.gh import gh\n", "lib.gh"),
        ("from lib import gh\n", "lib.gh"),
        ("import lib.kubectl\n", "lib.kubectl"),
        ("import os\n\nos.system('gh variable get DEPLOY_HOLD')\n", "os.system"),
        ("from os import popen\n", "os.popen"),
        (
            "from deployer_lib.deploy_toolbox import fetch_ci_verdict\n",
            "deployer_lib.deploy_toolbox",
        ),
        ("from deployer_lib import deploy_alerts\n", "deployer_lib.deploy_alerts"),
        ("from deployer_lib.deploy_git import ci_verdict\n", "deployer_lib.deploy_git"),
    ],
    ids=[
        "from-lib-gh",
        "from-lib-import-gh",
        "lib-kubectl",
        "os-system",
        "os-popen",
        "deployer-lib-toolbox",
        "from-deployer-lib-import-alerts",
        "deployer-lib-git",
    ],
)
def test_a_module_reaching_gh_through_a_package_or_os_is_flagged(source, flagged):
    assert flagged in forbidden_imports("import forge\n" + source)


def test_the_allowed_package_imports_are_found_in_promote():
    """Non-vacuity for the allowlists: the package imports of promote.py and the modules split
    out of it are each recognised."""
    modules = set().union(
        *(
            imported_modules(p.read_text(encoding="utf-8"))
            for p in [PROMOTE, *PROMOTE_LIB]
        )
    )
    assert {"lib.git", "lib.yaml_fast", "ci_verdict"} <= modules
    assert {name for name in modules if name.startswith("lib.")} <= ALLOWED_LIB
    assert {
        name for name in modules if name.startswith("deployer_lib.")
    } <= ALLOWED_DEPLOYER_LIB


# -- request count -------------------------------------------------------------------------

TAGS_PUBLISHED = 3000


def _world_with_3000_tags(tmp_path) -> World:
    world = World(tmp_path, size=6)
    for n in range(TAGS_PUBLISHED):
        world.transport.tags[f"sha-{n:040x}"] = digest_of(str(n))
    world.set_prod(world.commits[0])
    return world


def _registry_requests_within_budget(world: World, examined: list[str]) -> bool:
    """One token request, the in-flight HEADs, one `prod` fetch, one `HEAD` per examined
    candidate, then the two bundle pulls `verify` reuses, T's and `prod`'s.
    """
    repository = f"/v2/{forge.ARTIFACT}"
    manifests = f"{repository}/manifests"
    expected = [
        ("GET", "/token"),
        ("HEAD", f"{manifests}/prod"),
        ("HEAD", f"{manifests}/last-good"),
        ("GET", f"{manifests}/prod"),
    ]
    expected += [("HEAD", f"{manifests}/sha-{sha}") for sha in examined]
    for sha in (examined[-1], world.commits[0]):
        manifest = world.transport.manifests[digest_of(sha)]
        expected += [
            ("GET", f"{manifests}/{digest_of(sha)}"),
            ("GET", f"{repository}/blobs/{manifest['layers'][0]['digest']}"),
        ]
    return world.transport.registry_requests() == expected


def test_decide_makes_one_head_per_candidate_examined_is_clean(
    tmp_path, monkeypatch, capsys
):
    """The tip and the next commit are unpublished, so the walk examines three candidates."""
    world = _world_with_3000_tags(tmp_path)
    *_, c3, c4, tip = world.commits
    world.publish(c3)
    result = world.decide(monkeypatch, capsys, trigger=tip)
    assert result.outputs["target"] == c3
    assert len(world.transport.tags) > TAGS_PUBLISHED
    assert _registry_requests_within_budget(world, [tip, c4, c3])


class _ListingRegistry(forge.Registry):
    """A registry client that lists every tag before each `HEAD`, as `flux list` does."""

    def digest(self, reference):
        self._transport(
            "GET",
            f"https://{forge.REGISTRY}/v2/{self.repository}/tags/list",
            {"Authorization": f"Bearer {self._token()}"},
        )
        return super().digest(reference)


def test_a_client_that_lists_tags_is_flagged(tmp_path, monkeypatch, capsys):
    world = _world_with_3000_tags(tmp_path)
    tip = world.commits[-1]
    world.publish(tip)
    registry = _ListingRegistry(world.transport, "job-token")
    result = world.decide(monkeypatch, capsys, trigger=tip, registry=registry)
    assert result.outputs["target"] == tip
    assert not _registry_requests_within_budget(world, [tip])


def test_the_shared_ci_verdict_module_imports_nothing_that_reaches_a_forge():
    """`promote.py` imports `hosts/lib/ci_verdict.py` bare, so the module is held to the boundary.

    It must import only the standard library's `collections.abc` and `__future__`, and
    `forbidden_imports` finds no HTTP library, `subprocess` or `os` exec in it.
    """
    path = Path(promote.__file__).parents[2] / "hosts" / "lib" / "ci_verdict.py"
    source = path.read_text(encoding="utf-8")
    assert forbidden_imports(source) == set()
    assert imported_modules(source) == {"__future__", "collections.abc"}
