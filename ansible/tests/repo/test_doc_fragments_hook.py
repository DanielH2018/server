"""The `regen-doc-fragments` prek hook rewrites a stale docs fragment at commit time (#3827).

Changing a tunable a fragment reads used to fail `static-ratchet-tests` until someone ran
`scripts/docs/gen_doc_fragments.py` by hand. The hook runs it in `--fix` mode on the commit
instead. This guard holds what a later edit could quietly lose: the hook sits above
`static-ratchet-tests`, its `files` gate matches every source a fragment header names plus the
k8s role defaults the auto-deploy coverage walks, and `main(["--fix"])` over a stale fragment
rewrites it and exits 1, which is what stops the commit for a re-stage.

Run: uv run pytest ansible/tests/repo/test_doc_fragments_hook.py
"""

import re
import shutil
import tomllib

import pytest
from _helpers import REPO

import gen_doc_fragments as g

HOOK_ID = "regen-doc-fragments"


def _hook_ids() -> list[str]:
    config = tomllib.loads((REPO / "prek.toml").read_text())
    return [
        hook.get("id") for repo in config["repos"] for hook in repo.get("hooks", [])
    ]


def _hook() -> dict:
    config = tomllib.loads((REPO / "prek.toml").read_text())
    hooks = [
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id") == HOOK_ID
    ]
    assert len(hooks) == 1, f"expected exactly one {HOOK_ID} hook, found {len(hooks)}"
    return hooks[0]


def test_the_hook_runs_the_generator_in_fix_mode():
    assert _hook()["entry"].endswith(f"python {g.SELF} --fix")


def test_the_hook_runs_before_the_test_that_reads_the_fragments():
    ids = _hook_ids()
    assert ids.index(HOOK_ID) < ids.index("static-ratchet-tests")


def _header_sources() -> set[str]:
    sources = set()
    for build in g.FRAGMENTS.values():
        sources.update(build()[1])
    return sources


def test_the_gate_matches_every_source_a_fragment_names():
    sources = _header_sources()
    assert "ansible/secret_rotation.yml" in sources
    assert "scripts/secrets_mgmt/secret_rotation.py" in sources
    # A directory source stands for the files under it.
    paths = [s + "x.yml" if s.endswith("/") else s for s in sources]
    unmatched = sorted(p for p in paths if not re.search(_hook()["files"], p))
    assert not unmatched, f"{HOOK_ID} does not fire on: {unmatched}"


@pytest.mark.parametrize(
    "path",
    [
        "ansible/roles/k8s/sonarr/defaults/main.yml",
        "scripts/docs/fragment_readers.py",
        "scripts/docs/fragment_renderers.py",
        "docs/assets/generated/fragments/longhorn-tiers.md",
    ],
)
def test_the_gate_matches_a_source_no_header_names(path):
    """Autodeploy coverage walks every k8s role's defaults; a fragment hand edit is reverted."""
    assert re.search(_hook()["files"], path)


@pytest.mark.parametrize(
    "path",
    ["docs/reference/services.md", "docs/gitops-pipeline.md", "CLAUDE.md"],
)
def test_the_gate_skips_a_path_the_generator_does_not_read(path):
    assert not re.search(_hook()["files"], path)


def _stale_copy(tmp_path):
    out = tmp_path / "fragments"
    shutil.copytree(g.REPO / g.DEFAULT_OUT_DIR, out)
    fragment = out / "longhorn-tiers.md"
    fresh = fragment.read_text()
    fragment.write_text(fresh + "hand edit\n")
    return out, fragment, fresh


def test_fix_mode_rewrites_a_stale_fragment_and_fails(tmp_path, capsys):
    out, fragment, fresh = _stale_copy(tmp_path)
    assert g.main(["--fix", "--out-dir", str(out)]) == 1
    assert "longhorn-tiers" in capsys.readouterr().out
    assert fragment.read_text() == fresh


def test_fix_mode_passes_once_the_fragments_are_fresh(tmp_path):
    out, _, _ = _stale_copy(tmp_path)
    g.main(["--fix", "--out-dir", str(out)])
    assert g.main(["--fix", "--out-dir", str(out)]) == 0
