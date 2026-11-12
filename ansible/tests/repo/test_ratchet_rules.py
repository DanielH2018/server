"""Red-proof pairs for the patch counter in `ansible/tests/_ratchet_patches.py`.

The counter is the half of the two ratchets with a heuristic in it: which names in a test
module hold a first-party module, and therefore which `monkeypatch.setattr` calls and attribute assignments count. Every
test here is a pure call on a source string, so each rule has one input it must count and one
it must not.

The caps, the allowlist comparisons and the census of the real tree are in
`ansible/tests/repo/test_module_length_ratchet.py`; `_ratchet_patches.py`'s docstring is where
the heuristic's blind spots are written down.

Run: uv run pytest ansible/tests/repo/test_ratchet_rules.py
"""

from _ratchet_patches import (
    count_module_patches,
    first_party_module_names,
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


def test_a_patch_on_a_local_object_or_by_another_method_is_not_counted():
    src = (
        "import mod\n"
        "def test_x(monkeypatch, obj):\n"
        "    monkeypatch.setattr(obj, 'f', 1)\n"
        "    monkeypatch.setenv('MOD', '1')\n"
    )
    assert count_module_patches(src, {"mod"}) == 0


def test_a_string_target_naming_a_first_party_module_is_counted():
    """The object form's equal at runtime, and it pins the same module name into the test."""
    src = "def test_x(monkeypatch):\n    monkeypatch.setattr('mod.f', 1)\n"
    assert count_module_patches(src, {"mod"}) == 1
    nested = (
        "def test_x(monkeypatch):\n    monkeypatch.setattr('mod.subprocess.run', 1)\n"
    )
    assert count_module_patches(nested, {"mod"}) == 1


def test_a_string_target_naming_something_else_is_not_counted():
    """No import binds the name, so the root segment is matched against first-party names."""
    stdlib = "def test_x(monkeypatch):\n    monkeypatch.setattr('subprocess.run', 1)\n"
    undotted = "def test_x(monkeypatch):\n    monkeypatch.setattr('mod', 1)\n"
    assert count_module_patches(stdlib, {"mod"}) == 0
    assert count_module_patches(undotted, {"mod"}) == 0


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


def test_a_hand_rolled_patch_of_a_first_party_module_is_counted_once():
    """The save, assign, restore shape counts as one patch: the restore sits in `finally:`."""
    src = (
        "import mod\n"
        "def test_x():\n"
        "    saved = mod.sub.f\n"
        "    try:\n"
        "        mod.sub.f = lambda: 1\n"
        "        run()\n"
        "    finally:\n"
        "        mod.sub.f = saved\n"
    )
    assert count_module_patches(src, {"mod"}) == 1


def test_every_attribute_a_tuple_or_augmented_assignment_writes_is_counted():
    src = "import mod\ndef test_x():\n    mod.a, mod.b = 1, 2\n    mod.n += 1\n"
    assert count_module_patches(src, {"mod"}) == 3


def test_an_assignment_to_a_local_object_or_the_standard_library_is_not_counted():
    src = (
        "import mod\n"
        "import sys\n"
        "def test_x(cfg):\n"
        "    cfg.X = 1\n"
        "    local = Thing()\n"
        "    local.sub.f = 2\n"
        "    sys.argv = []\n"
        "    value = mod.f\n"
    )
    assert count_module_patches(src, {"mod"}) == 0
