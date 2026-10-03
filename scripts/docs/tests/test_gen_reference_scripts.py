"""Tests for scripts/docs/reference/scripts.py and the classifier it assembles.

Fixture-driven: a synthetic scripts/ directory under tmp_path.
Run: uv run pytest scripts/docs/tests/test_gen_reference_scripts.py
"""

import textwrap
from pathlib import Path

from docs.reference import scripts as g
from lib import script_classify as sc


def _write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))


def test_the_summary_is_the_docstrings_first_line(tmp_path):
    _write(
        tmp_path / "probe.py", '"""Read-only homelab diagnostics.\n\nMore prose.\n"""\n'
    )
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert rows["probe.py"]["summary"] == "Read-only homelab diagnostics."


def test_a_script_is_never_imported_to_read_its_docstring(tmp_path):
    """Importing runs module-level code; ast.parse does not.

    A generator that imported its subjects would run 40 scripts' worth of top-level code
    on every docs refresh — including anything that dials a host or takes a lock.
    """
    _write(tmp_path / "boom.py", '"""Summary."""\nraise SystemExit("imported")\n')
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert rows["boom.py"]["summary"] == "Summary."


def test_a_script_that_does_not_parse_is_reported_not_skipped(tmp_path):
    """Silently dropping it would make the page quietly incomplete."""
    _write(tmp_path / "broken.py", '"""Summary."""\ndef (\n')
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert "broken.py" in rows
    assert "could not be parsed" in rows["broken.py"]["summary"]


def test_a_script_with_no_docstring_says_so(tmp_path):
    _write(tmp_path / "bare.py", "x = 1\n")
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert "no module docstring" in rows["bare.py"]["summary"]


def test_test_files_and_private_modules_are_excluded(tmp_path):
    _write(tmp_path / "test_probe.py", '"""x"""\n')
    _write(tmp_path / "conftest.py", '"""x"""\n')
    _write(tmp_path / "_private_helper.py", '"""x"""\n')
    assert g.build_rows(tmp_path) == []


def test_shell_scripts_use_their_leading_comment_block(tmp_path):
    _write(
        tmp_path / "deploy.sh",
        "#!/usr/bin/env bash\n# Deploy a service under the git-tree lock.\n# More detail.\nset -e\n",
    )
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert rows["deploy.sh"]["summary"] == "Deploy a service under the git-tree lock."


def test_the_usage_block_is_extracted_when_present(tmp_path):
    _write(
        tmp_path / "probe.py",
        '"""Summary.\n\nUsage::\n\n    uv run python scripts/diagnostics/probe.py targets\n"""\n',
    )
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert (
        "uv run python scripts/diagnostics/probe.py targets"
        in rows["probe.py"]["usage"]
    )


def test_a_script_with_no_usage_block_has_an_empty_usage(tmp_path):
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert rows["probe.py"]["usage"] == ""


def test_a_script_with_a_test_file_names_it(tmp_path):
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    _write(tmp_path / "test_probe.py", '"""x"""\n')
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert rows["probe.py"]["tests"] == "test_probe.py"


def test_a_script_with_no_test_file_is_reported_as_such(tmp_path):
    """An untested script is a fact worth surfacing, not an omission to hide."""
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert rows["probe.py"]["tests"] == ""


def test_rows_are_sorted_by_name(tmp_path):
    for name in ("zeta.py", "alpha.py", "mid.py"):
        _write(tmp_path / name, '"""Summary."""\n')
    names = [r["name"] for r in g.build_rows(tmp_path)]
    assert names == sorted(names)


def test_the_real_scripts_directory_yields_the_known_shape():
    """Guards the exclusion rules against the live tree, not just fixtures."""
    rows = g.build_rows()
    names = {r["name"] for r in rows}
    assert "probe.py" in names
    assert "deploy.sh" in names
    assert not any(n.startswith(("test_", "_")) for n in names)
    assert "conftest.py" not in names
    assert len(rows) >= 30


# --- classify(): how each script is run ------------------------------------------------


def _repo(tmp_path):
    """A synthetic tree with one of every place a script can be invoked from."""
    scripts = tmp_path / "scripts"
    for name in ("cronned", "wrapped", "gated", "shipped", "lib", "lonely"):
        _write(scripts / f"{name}.py", '"""Summary."""\n')
    _write(scripts / "user.py", '"""Summary."""\nimport lib\n')

    _write(tmp_path / "prek.toml", 'entry = "uv run python scripts/gated.py"\n')
    _write(
        tmp_path / ".github" / "workflows" / "ci.yml",
        "jobs:\n  x:\n    steps:\n      - run: python scripts/shipped.py\n",
    )
    _write(
        tmp_path / "ansible" / "roles" / "r" / "tasks" / "main.yml",
        """
        - name: A cron
          ansible.builtin.cron:
            name: Nightly thing
            job: "uv run python scripts/cronned.py"
        - name: A wrapper cron
          ansible.builtin.cron:
            name: Wrapped thing
            job: "/usr/local/bin/wrap.sh"
        """,
    )
    _write(
        tmp_path / "ansible" / "roles" / "r" / "templates" / "wrap.sh.j2",
        "#!/bin/sh\nuv run python scripts/wrapped.py\n",
    )
    return tmp_path, scripts


def test_a_cron_job_makes_a_script_scheduled(tmp_path):
    repo, scripts = _repo(tmp_path)
    verdicts = sc.classify(repo, scripts)
    assert verdicts["cronned.py"][0] == "scheduled"
    assert "Nightly thing" in verdicts["cronned.py"][1]


def test_a_cron_reaches_through_its_wrapper_template(tmp_path):
    """build_docs.py is named only by docs-refresh.sh, which is what its cron runs."""
    repo, scripts = _repo(tmp_path)
    verdicts = sc.classify(repo, scripts)
    assert verdicts["wrapped.py"][0] == "scheduled"
    assert "wrap.sh" in verdicts["wrapped.py"][1]


def test_a_prek_entry_and_a_workflow_step_are_gates(tmp_path):
    repo, scripts = _repo(tmp_path)
    verdicts = sc.classify(repo, scripts)
    assert verdicts["gated.py"][0] == "gate"
    assert verdicts["shipped.py"][0] == "gate"


def test_an_imported_module_is_a_library(tmp_path):
    repo, scripts = _repo(tmp_path)
    verdicts = sc.classify(repo, scripts)
    assert verdicts["lib.py"][0] == "library"
    assert "user.py" in verdicts["lib.py"][1]


def test_a_script_nothing_reaches_is_adhoc(tmp_path):
    repo, scripts = _repo(tmp_path)
    assert sc.classify(repo, scripts)["lonely.py"] == (
        "adhoc",
        "no automated caller in the tree",
    )


def test_the_highest_kind_wins_when_a_script_is_reached_twice(tmp_path):
    """The costliest way it runs is the one that decides what a break costs."""
    repo, scripts = _repo(tmp_path)
    _write(tmp_path / "prek.toml", 'entry = "uv run python scripts/cronned.py"\n')
    assert sc.classify(repo, scripts)["cronned.py"][0] == "scheduled"


def test_a_prose_mention_is_not_an_invocation(tmp_path):
    """Every CLAUDE.md and half the role defaults cite these scripts in backticks."""
    repo, scripts = _repo(tmp_path)
    _write(
        tmp_path / "ansible" / "roles" / "r" / "tasks" / "notes.yml",
        "- name: See `uv run python scripts/lonely.py` for detail\n",
    )
    assert sc.classify(repo, scripts)["lonely.py"][0] == "adhoc"


def test_a_printed_message_naming_a_script_is_not_an_invocation(tmp_path):
    """deploy.sh prints the prune_worktrees command when the lock is busy."""
    repo, scripts = _repo(tmp_path)
    _write(
        tmp_path / "ansible" / "hand.sh",
        'echo "try uv run python scripts/lonely.py" >&2\n',
    )
    assert sc.classify(repo, scripts)["lonely.py"][0] == "adhoc"


def test_a_sentence_in_a_python_string_is_not_an_invocation(tmp_path):
    """session-health.py prints "(scripts/deploy_tools/deploy_staleness.py, exit 4)" as prose."""
    repo, scripts = _repo(tmp_path)
    _write(
        tmp_path / ".claude" / "hooks" / "h.py",
        'print("the gate (scripts/lonely.py, exit 4) refuses")\n',
    )
    assert sc.classify(repo, scripts)["lonely.py"][0] == "adhoc"


def test_an_argv_element_in_python_source_is_an_invocation(tmp_path):
    """build_docs.py runs the generators through subprocess, one path per list element."""
    repo, scripts = _repo(tmp_path)
    _write(
        scripts / "runner.py",
        '"""Summary."""\nimport subprocess\n'
        'subprocess.run(["python", "scripts/lonely.py", "--out", "x"])\n',
    )
    _write(
        tmp_path / "prek.toml",
        'entry = "uv run python scripts/gated.py"\n'
        'other = "uv run python scripts/runner.py"\n',
    )
    verdicts = sc.classify(repo, scripts)
    assert verdicts["runner.py"][0] == "gate"
    assert verdicts["lonely.py"][0] == "gate"
    assert "runner.py" in verdicts["lonely.py"][1]


def test_the_live_tree_classifies_the_names_we_already_know(live_verdicts):
    """A derivation that quietly narrows reads exactly like one that works.

    Every name here has an invocation site someone can open. If one moves to `adhoc`,
    either the tree changed or the census stopped seeing a whole class of caller.
    """
    verdicts = live_verdicts
    expected = {
        "build_docs.py": "scheduled",
        "gen_infra_map.py": "scheduled",
        "secret_rotation.py": "scheduled",
        "k8s_manifests.py": "gate",
        "compose_templates.py": "gate",
        "shell_templates.py": "gate",
        "unit_templates.py": "gate",
        "validate_ha_config.py": "gate",
        "grafana_dashboards.py": "gate",
        "deploy_tags.py": "gate",
        "deploy_staleness.py": "gate",
        "smoke_extract.py": "gate",
        # "scheduled" rather than "gate": release-staleness-check.sh.j2 (a daniel-box cron,
        # roles/setup/k3s) invokes `probe.py releases --stale-only`, and "scheduled" outranks
        # "gate" in _PRECEDENCE. probe.py is still a gate too (deploy.sh, hooks) -- the cron
        # adds a caller, it doesn't drop one.
        "probe.py": "scheduled",
        "docs_provenance.py": "library",
        "core.py": "library",
        # "gate" rather than "adhoc" because of the call graph, not the classifier. The staging
        # gate has always ended in `deploy.sh`, but it reached it through a script PIPED over
        # ssh, which is invisible to a scan of the tree. The restricted key's dispatcher is a
        # role template, so the chain dispatcher -> staging_gate_remote.sh -> deploy.sh is
        # visible and deploy.sh inherits the caller's kind. A person also runs it by hand; the
        # gate is simply a caller the classifier can see.
        "deploy.sh": "gate",
        "etcd_restore_drill.sh": "gate",  # a role copies it; its crons hide behind wrappers
    }
    assert {name: verdicts[name][0] for name in expected} == expected


def test_every_reference_generator_is_reached_from_the_docs_cron(live_verdicts):
    """build_docs.py runs them, docs-refresh.sh runs build_docs.py, a cron runs that.

    The generators are found by DIRECTORY, not by a `gen_reference_` filename prefix. They
    carried that prefix until they moved into `scripts/docs/reference/`, at which point the
    prefix match returned an empty list — the `>= 5` below is what said so, since a vacuous
    `all()` over nothing passes.
    """
    verdicts = live_verdicts
    generators = [p.name for p in (g.SCRIPTS / "docs" / "reference").glob("*.py")]
    assert len(generators) >= 5
    assert all(verdicts[name][0] == "scheduled" for name in generators)


def test_markdown_splits_the_scripts_by_how_they_run(tmp_path):
    repo, scripts = _repo(tmp_path)
    out = g.render_markdown(g.build_rows(scripts, repo))
    for heading in ("on a schedule", "Imported, never run", "Run by hand"):
        assert heading in out


def test_a_nested_script_is_listed_and_classified_by_its_caller(tmp_path):
    """Depth is not capped: a module two directories down is found and classified.

    Discovery globbed `scripts/*` and `scripts/*/*`, and both script-reference patterns
    allowed a single directory segment. A module at `scripts/a/b/c.py` was therefore
    absent from the page altogether — worse than being listed as uncovered, because a
    reference page that silently omits a script says nothing is there to check.
    """
    repo, scripts = _repo(tmp_path)
    _write(scripts / "deep" / "nested" / "buried.py", '"""Buried summary."""\n')
    _write(
        scripts / "caller.py",
        '"""Summary."""\nimport subprocess\n'
        'subprocess.run(["uv", "run", "python", "scripts/deep/nested/buried.py"])\n',
    )
    rows = {r["name"]: r for r in g.build_rows(scripts, repo)}
    assert "buried.py" in rows
    assert rows["buried.py"]["path"] == "scripts/deep/nested/buried.py"
    assert rows["buried.py"]["evidence"] == f"caller.py ({sc.RUNS['adhoc']})"


def test_a_path_outside_scripts_is_still_not_a_reference(tmp_path):
    """The RED half: widening the directory group must not widen what counts as a script.

    `*` on the segment makes the pattern accept any depth under `scripts/`. It must still
    reject a same-shaped path rooted anywhere else, or every `ansible/roles/x/files/y.py`
    would start reading as an invocation of a first-party script.
    """
    assert sc.ARGV_RE.fullmatch("scripts/deep/nested/buried.py")
    assert not sc.ARGV_RE.fullmatch("ansible/roles/r/files/buried.py")
    assert not sc.ARGV_RE.fullmatch("other/scripts/deep/buried.py")


def _basename_clashes(paths):
    """Pairs of paths sharing a basename, in the order given."""
    seen: dict[str, Path] = {}
    clashes = []
    for path in paths:
        if path.name in seen:
            clashes.append(f"{seen[path.name]} and {path}")
        seen[path.name] = path
    return clashes


def test_no_two_scripts_share_a_basename():
    """The page keys verdicts and importers on a script's bare filename.

    Two files with the same basename in different `scripts/` subdirectories therefore
    merge into one row, and the merged row states things that are false: a rename of
    `infra_map_common.py` to `common.py` would make the page claim
    `scripts/availability_bots/`'s bots import infra_map's module, because
    `availability_bots/common.py` already exists. The rename shipped as `constants.py`
    instead, and this is what stops the next one landing silently.

    Keying by path would relocate the ambiguity rather than remove it. The evidence that
    a script is invoked comes from free text — a cron `job:` string, an argv literal, a
    shell line — and that text often names a bare filename with no directory at all. A
    path-keyed generator still needs a basename-to-path resolution step, and that step is
    undecidable for exactly the colliding names this test forbids. So uniqueness is the
    invariant, and this is where it is enforced. Sibling, different invariant, keep apart:
    the `no-two-import-roots-share-a-module-basename` row of
    `ansible/tests/repo/test_census_rows_python.py` forbids one NAME at two
    `pythonpath` roots — sys.path shadowing, so every root but top level only.
    """
    clashes = _basename_clashes(sc.candidates(g.SCRIPTS))
    assert not clashes, (
        "two scripts share a basename, so they merge into one row on the reference page: "
        + "; ".join(clashes)
    )


def test_the_basename_guard_flags_a_real_clash():
    """The RED half of the pair above.

    The clean half runs against a tree that currently satisfies the invariant, so on its
    own it is indistinguishable from a check that never fires.
    """
    clashes = _basename_clashes(
        [
            Path("scripts/availability_bots/common.py"),
            Path("scripts/infra_map/common.py"),
            Path("scripts/infra_map/render.py"),
        ]
    )
    assert clashes == [
        "scripts/availability_bots/common.py and scripts/infra_map/common.py"
    ]
