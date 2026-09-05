"""Guard: no two `pythonpath` roots hold a top-level module of the same name.

The repo has no `__init__.py` files, so every `*.py` sitting directly in a `pythonpath` root
is a BARE-importable top-level module. Two roots holding the same name means `import <name>`
resolves to whichever root `sys.path` reaches first, and nothing in the tree says which that
is. PR #1149 landed `ansible/roles/k8s/monitor-bridge/files/registry.py` next to the existing
`scripts/lib/registry.py` (issue #1200): monitor-bridge's `cli.py` and its eight test modules
import it bare, while every consumer of the `scripts/` one spells it `lib.registry`, so the
clash resolved correctly by accident of ordering rather than by construction. The `scripts/`
module is now `lib/cli_registry.py`, and this test is what stops the next pair landing.

Scope is deliberately TOP-LEVEL ONLY. A module in a subdirectory of a root is reached as
`package.module`, so it cannot shadow anything; that is why this is a sibling of
`test_no_two_scripts_share_a_basename` in `scripts/docs/tests/test_gen_reference_scripts.py`
rather than a widening of it. That guard enforces a different invariant — the reference page
keys its rows on bare filenames at ANY depth under `scripts/` — and merging the two would give
one test two rationales and the wrong scope for both.

Two benign collisions are excluded by name, each for a reason that is a property of the import
system rather than of today's tree:

- `conftest.py` exists at three roots. pytest imports a conftest by path under its own unique
  module key, so conftests never shadow one another.
- `tests/` exists at eleven roots, but directories are not modules here: with no `__init__.py`
  they are PEP 420 namespace packages, which MERGE across `sys.path` entries instead of
  shadowing. This census therefore reads `*.py` files and nothing else.

Clean/flagged pair below, per the repo rule that a new check ships with a proof it can go RED.

Run: uv run pytest ansible/tests/repo/test_pythonpath_module_basenames.py
"""

import tomllib

from _helpers import REPO

# pytest imports a conftest under its own key, so several never shadow one another.
EXEMPT = frozenset({"conftest.py"})

# Named members the census must contain, one per root SHAPE the list holds: a filter plugin, a
# cross-role shared host module, a deployer module, a monitor-bridge module reached bare from
# inside the container image, and two `scripts/` modules. A root that is renamed or moved one
# directory down drops out of the census silently, and a clash check over a shrunken census
# still passes — so the failure message has to name the missing member rather than report that
# a count moved.
KNOWN_MODULES = frozenset(
    {
        "toposort",
        "host_lib",
        "deploy_logic",
        "registry",
        "cli_registry",
        "repo_paths",
    }
)


def pythonpath_roots() -> list[str]:
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    return list(cfg["tool"]["pytest"]["ini_options"]["pythonpath"])


def top_level_modules() -> dict[str, list[str]]:
    """`{module name: [<root>/<file>, ...]}` for every bare-importable top-level module."""
    found: dict[str, list[str]] = {}
    for root in pythonpath_roots():
        for path in sorted((REPO / root).glob("*.py")):
            if path.name in EXEMPT:
                continue
            found.setdefault(path.stem, []).append(f"{root}/{path.name}")
    return found


def shadowing_pairs(modules: dict[str, list[str]]) -> dict[str, list[str]]:
    """The pure half both the live check and its red proof call."""
    return {name: paths for name, paths in modules.items() if len(paths) > 1}


def test_every_pythonpath_root_is_a_real_directory():
    """Non-vacuity, part one: a root that moved would leave the census silently short."""
    missing = [root for root in pythonpath_roots() if not (REPO / root).is_dir()]
    assert not missing, (
        f"pyproject pythonpath names directories that do not exist: {missing}"
    )


def test_the_census_reaches_the_known_modules():
    """Non-vacuity, part two: name what must be there, not how many there are."""
    names = set(top_level_modules())
    missing = KNOWN_MODULES - names
    assert not missing, (
        f"{sorted(missing)} absent from the top-level census, so the clash check below is "
        f"reading a shrunken tree; census sees {sorted(names)}"
    )


def test_no_two_pythonpath_roots_share_a_module_basename():
    clashes = shadowing_pairs(top_level_modules())
    assert not clashes, (
        "these top-level module names exist at more than one pythonpath root, so a bare "
        f"`import <name>` resolves by sys.path order rather than by construction: {clashes}"
    )


def test_a_census_with_one_home_per_name_is_clean():
    assert not shadowing_pairs(
        {
            "registry": ["ansible/roles/k8s/monitor-bridge/files/registry.py"],
            "cli_registry": ["scripts/lib/cli_registry.py"],
        }
    )


def test_a_name_at_two_roots_is_flagged():
    """The RED half.

    The clean half runs against a tree that already satisfies the invariant, so on its own
    it cannot tell a working check from one that never fires.
    """
    assert shadowing_pairs(
        {
            "registry": [
                "ansible/roles/k8s/monitor-bridge/files/registry.py",
                "scripts/lib/registry.py",
            ],
            "cli_registry": ["scripts/lib/cli_registry.py"],
        }
    ) == {
        "registry": [
            "ansible/roles/k8s/monitor-bridge/files/registry.py",
            "scripts/lib/registry.py",
        ]
    }
