"""Red-proof pairs for the patch counter in `ansible/tests/_ratchet.py`.

The counter is the half of the two ratchets with a heuristic in it: which names in a test
module hold a first-party module, and therefore which `monkeypatch.setattr` calls count. Every
test here is a pure call on a source string, so each rule has one input it must count and one
it must not.

The caps, the allowlist comparisons and the census of the real tree are in
`ansible/tests/repo/test_module_length_ratchet.py`; `_ratchet.py`'s docstring is where the
heuristic's blind spots are written down.

Run: uv run pytest ansible/tests/repo/test_ratchet_rules.py
"""

from _ratchet import (
    count_module_patches,
    first_party_module_names,
    module_fixture_names,
)


def test_a_first_party_name_is_a_module_stem_or_the_directory_holding_one():
    names = first_party_module_names(["scripts/deploy_tools/land_lib/tools.py"])
    assert "tools" in names and "land_lib" in names
    assert "scripts" not in names and "deploy_tools" not in names


def test_a_patch_on_an_imported_first_party_module_is_counted():
    src = "import mod\ndef test_x(monkeypatch):\n    monkeypatch.setattr(mod.sub, 'f', 1)\n"
    assert count_module_patches(src, {"mod"}) == 1


def test_a_patch_on_the_standard_library_is_not_counted():
    """A seam cannot remove one, so counting it would leave an entry stuck above zero."""
    src = "import sys\ndef test_x(monkeypatch):\n    monkeypatch.setattr(sys, 'argv', [])\n"
    assert count_module_patches(src, {"mod"}) == 0


def test_a_patch_on_a_local_object_or_a_string_target_is_not_counted():
    src = (
        "import mod\n"
        "def test_x(monkeypatch, obj):\n"
        "    monkeypatch.setattr(obj, 'f', 1)\n"
        "    monkeypatch.setattr('mod.f', 1)\n"
        "    monkeypatch.setenv('MOD', '1')\n"
    )
    assert count_module_patches(src, {"mod"}) == 0


def test_an_aliased_import_is_still_the_module_it_aliases():
    src = "import a.b as mod\ndef test_x(monkeypatch):\n    monkeypatch.setattr(mod, 'f', 1)\n"
    assert count_module_patches(src, {"b"}) == 1


def test_a_module_bound_by_importlib_is_counted_in_both_spellings():
    """`session-health.py` is not an identifier, so its tests reach it through a spec."""
    spec = "_mod = importlib.util.module_from_spec(_spec)\n"
    named = "_mod = importlib.import_module('scripts.thing')\n"
    patch = "def test_x(monkeypatch):\n    monkeypatch.setattr(_mod, 'f', 1)\n"
    assert count_module_patches(spec + patch, set()) == 1
    assert count_module_patches(named + patch, set()) == 1


def test_a_conftest_fixture_that_hands_back_a_module_is_found_in_both_spellings():
    annotated = (
        "import pytest\n"
        "@pytest.fixture(scope='session')\n"
        "def mod() -> ModuleType:\n    return None\n"
    )
    imported = "import pytest\n@pytest.fixture\ndef mod():\n    import thing\n    return thing\n"
    assert module_fixture_names(annotated) == {"mod"}
    assert module_fixture_names(imported) == {"mod"}


def test_a_fixture_that_hands_back_something_else_is_not_a_module_fixture():
    """And a plain function with the annotation is not one either — pytest never calls it."""
    other = "import pytest\n@pytest.fixture\ndef mod() -> Path:\n    return Path('.')\n"
    undecorated = "def mod() -> ModuleType:\n    return None\n"
    assert module_fixture_names(other) == frozenset()
    assert module_fixture_names(undecorated) == frozenset()


def test_a_patch_on_a_module_fixture_parameter_is_counted():
    src = (
        "def test_x(monkeypatch, gitops_deploy):\n"
        "    monkeypatch.setattr(gitops_deploy, 'REPO', '/tmp')\n"
    )
    assert count_module_patches(src, set(), {"gitops_deploy"}) == 1


def test_the_same_patch_is_not_counted_when_no_fixture_returns_a_module():
    """The parameter is an ordinary object until a conftest says otherwise."""
    src = (
        "def test_x(monkeypatch, gitops_deploy):\n"
        "    monkeypatch.setattr(gitops_deploy, 'REPO', '/tmp')\n"
    )
    assert count_module_patches(src, set()) == 0
