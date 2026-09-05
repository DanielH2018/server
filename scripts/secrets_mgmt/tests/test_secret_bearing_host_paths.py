"""Tests for the derivation in scripts/secrets_mgmt/secret_bearing_host_paths.py.

`.claude/hooks/block-protected-bash.py` denies a content-printing read of every path this
module returns. A derivation that quietly narrows therefore unblocks a read that prints a
credential into the terminal and the transcript, and nothing else in the tree notices — the
module had no test at all until this file (#1170).

Two shapes of test, because either alone is green while checking nothing:

- **Against the real tree.** `MUST_FIND` pins path -> secret-name pairs the census has to
  return. The module finds its own subject by pattern (`roles/**/tasks/*.yml`, plus
  `task_file.parent.parent` for the role directory), so a rename or a moved directory makes it
  return `{}` — and every synthetic-tree test below still passes, since those build the tree to
  match the pattern. The pin is a subset, never equality or a count: a new secret-bearing
  script must not fail this file.
- **Against a synthetic tree.** One input the derivation must flag and one it must not, so a
  rule that stopped matching altogether fails here rather than reading green.

Nothing in this file prints the content of a secret-bearing source. The assertions are over
dest paths and secret NAMES, which is exactly what the module returns and what the hook's deny
message already says out loud.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_bearing_host_paths.py
"""

import ast

import pytest

from lib.repo_paths import REPO
from secrets_mgmt.secret_bearing_host_paths import (
    HOST_BIN_PREFIXES,
    references_a_secret,
    secret_bearing_host_paths,
    secret_names,
)

# Path -> a secret name the census MUST report for it. Three entries chosen to span the shapes
# the derivation has to cover, so a narrowing cannot survive by keeping one of them:
#   * secret-rotation-audit.sh — the incident the module's docstring records (roles/setup/)
#   * ups-secondary-health.sh  — the nut_host plane, pinning "walks every role, not just k8s"
#   * qbittorrent-prefs-check.sh — a name that is not `*_push_token`, so the census cannot
#     silently narrow to push-token-shaped matches and still pass
#   * secret-rotate.sh — the path #1183 narrowed. It used to be reported for `email` as well
#     as `secret_rotation_push_token`; tiering `email` `ignore` left the push token as its
#     only reason to be in the census, so nothing else pins the path any more.
# The NAME is pinned alongside the path deliberately: a path survives a `GENERIC_NAMES` entry
# or a tier flipped to `ignore` while losing the very name that made it dangerous.
MUST_FIND = {
    "/usr/local/bin/secret-rotation-audit.sh": "secret_rotation_push_token",
    "/usr/local/bin/ups-secondary-health.sh": "nut_monitor_password",
    "/usr/local/bin/qbittorrent-prefs-check.sh": "qbittorrent_password",
    "/usr/local/bin/secret-rotate.sh": "secret_rotation_push_token",
}

# The hook's own copy of the prefix list. It short-circuits on this before importing anything,
# so a prefix added here and not there makes the guard stop firing for the new tree.
HOOK = REPO / ".claude" / "hooks" / "block-protected-bash.py"


@pytest.fixture(scope="module")
def real_census():
    return secret_bearing_host_paths()


# ── the real tree: the census must not be empty, and must contain these ──────────────────
def test_the_census_reaches_the_known_secret_bearing_scripts(real_census):
    missing = {
        dest: name
        for dest, name in MUST_FIND.items()
        if name not in real_census.get(dest, ())
    }
    assert not missing, (
        f"census lost {missing}; it currently reports {sorted(real_census)}"
    )


def test_every_returned_dest_is_under_a_host_bin_prefix(real_census):
    stray = [d for d in real_census if not d.startswith(HOST_BIN_PREFIXES)]
    assert not stray, f"{stray} would be invisible to the hook's cheap prefix gate"


def test_the_hooks_prefix_gate_matches_the_modules():
    """Read the hook's literal rather than importing it — it reads stdin at import time."""
    tree = ast.parse(HOOK.read_text(), filename=str(HOOK))
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_HOST_BIN_PREFIXES"
            for t in node.targets
        )
    ]
    assert literals, f"_HOST_BIN_PREFIXES not found in {HOOK}"
    assert ast.literal_eval(literals[0]) == HOST_BIN_PREFIXES


def test_the_ignore_tier_is_excluded_from_the_tracked_names():
    """`domain` is the registry's tracked-but-not-a-credential entry (tier: ignore).

    Matching on it flagged five host scripts that embed no credential at all, which is how a
    guard gets switched off. Pinned by name so a tier change to `domain` fails here.
    """
    assert "domain" not in secret_names()
    assert "secret_rotation_push_token" in secret_names()


def test_the_generic_address_name_is_not_tracked_but_its_password_is():
    """`email` is an address, so it is tiered `ignore` rather than listed in GENERIC_NAMES.

    The pair is the point (#1183). `email` word-matches in any script mentioning an address —
    it reached `/usr/local/bin/secret-rotate.sh` through a `git -c user.email=` line that
    embeds no credential — so a census that tracks it teaches sessions the guard is noise.
    `smtp_notify_app_password` is the value that actually authenticates that SMTP session and
    must stay tracked, so a sweep that tiered the whole SMTP family `ignore` fails here.
    """
    names = secret_names()
    assert "email" not in names
    assert "smtp_notify_app_password" in names


def test_tiering_the_address_did_not_drop_its_script_from_the_census(real_census):
    """Narrowing a name must not narrow the deny set.

    `MUST_FIND` already pins the path; this says why it is there, and fails with the reason
    rather than with a missing-key diff if a later change removes the push token too.
    """
    reported = real_census.get("/usr/local/bin/secret-rotate.sh", ())
    assert "email" not in reported
    assert "secret_rotation_push_token" in reported, (
        "secret-rotate.sh left the census; the hook now permits a content-printing read of a "
        "script that renders a push token inline"
    )


# ── the synthetic tree: one input it must flag, one it must not ──────────────────────────
def _tree(tmp_path, dest, src_text, *, role="demo", task_dir="tasks"):
    role_dir = tmp_path / "roles" / role
    (role_dir / task_dir).mkdir(parents=True)
    (role_dir / "templates").mkdir(parents=True, exist_ok=True)
    (role_dir / "templates" / "thing.sh.j2").write_text(src_text)
    (role_dir / task_dir / "main.yml").write_text(
        "- name: Install thing\n"
        "  ansible.builtin.template:\n"
        "    src: thing.sh.j2\n"
        f"    dest: {dest}\n"
    )
    (tmp_path / "secret_rotation.yml").write_text(
        "entries:\n"
        "  demo_push_token:\n"
        "    tier: auto\n"
        "    last_rotated: '2026-01-01'\n"
        "  demo_ignored_name:\n"
        "    tier: ignore\n"
        "    last_rotated: '2026-01-01'\n"
    )
    return tmp_path


def _census(tree):
    """The census over a synthetic tree, with its own registry.

    Both paths are parameters on the production function, so these cases neither read the real
    registry nor patch a module global — the repo's monkeypatch ratchet asks for the seam.
    """
    return secret_bearing_host_paths(tree, tree / "secret_rotation.yml")


def test_a_host_bin_script_rendering_a_secret_is_flagged(tmp_path):
    tree = _tree(
        tmp_path,
        "/usr/local/bin/demo.sh",
        "export PUSH={{ demo_push_token }}\n",
    )
    assert _census(tree) == {"/usr/local/bin/demo.sh": ["demo_push_token"]}


def test_a_host_bin_script_rendering_no_secret_is_clean(tmp_path):
    tree = _tree(tmp_path, "/usr/local/bin/demo.sh", "echo hello\n")
    assert _census(tree) == {}


def test_a_host_bin_script_rendering_only_an_ignore_tier_name_is_clean(tmp_path):
    """The `ignore` tier is tracked for completeness, not because it holds a credential.

    Matching on one flagged five host scripts that embed no secret at all, and a guard that
    fires on those is one that gets switched off.
    """
    tree = _tree(tmp_path, "/usr/local/bin/demo.sh", "echo {{ demo_ignored_name }}\n")
    assert _census(tree) == {}


def test_a_secret_bearing_file_outside_the_host_bin_trees_is_clean(tmp_path):
    """The prefix filter is what keeps the guard off ordinary config inspection.

    Same source, same secret — only the dest moves. Widening the prefixes would make this
    fire on every /etc config file a session reads while orienting.
    """
    tree = _tree(tmp_path, "/etc/demo/demo.conf", "export PUSH={{ demo_push_token }}\n")
    assert _census(tree) == {}


def test_a_secret_bearing_script_under_archive_is_clean(tmp_path):
    """`archive/` roles deploy nothing, so a path only they render is not live."""
    tree = _tree(
        tmp_path,
        "/usr/local/bin/demo.sh",
        "export PUSH={{ demo_push_token }}\n",
        role="archive/demo",
    )
    assert _census(tree) == {}


def test_an_unparseable_task_file_does_not_sink_the_whole_census(tmp_path):
    tree = _tree(
        tmp_path,
        "/usr/local/bin/demo.sh",
        "export PUSH={{ demo_push_token }}\n",
    )
    (tree / "roles" / "broken").mkdir(parents=True)
    (tree / "roles" / "broken" / "tasks").mkdir()
    (tree / "roles" / "broken" / "tasks" / "main.yml").write_text("- : [unbalanced\n")
    assert _census(tree) == {"/usr/local/bin/demo.sh": ["demo_push_token"]}


# ── the whole-word rule ──────────────────────────────────────────────────────────────────
def test_a_secret_name_matches_as_a_whole_word():
    assert references_a_secret("PUSH={{ demo_push_token }}", ["demo_push_token"]) == [
        "demo_push_token"
    ]


def test_a_longer_identifier_containing_the_name_is_clean():
    assert references_a_secret("demo_push_token_backup", ["demo_push_token"]) == []
