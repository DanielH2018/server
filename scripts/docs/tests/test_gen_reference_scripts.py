"""Tests for scripts/docs/gen_reference_scripts.py.

Fixture-driven: a synthetic scripts/ directory under tmp_path.

Run: uv run pytest scripts/docs/tests/test_gen_reference_scripts.py
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import gen_reference_scripts as g


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


def test_markdown_opens_with_the_provenance_banner(tmp_path):
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    out = g.render_markdown(g.build_rows(tmp_path))
    assert out.startswith("---\n")
    assert "generated_from: scripts/docs/gen_reference_scripts.py" in out


def test_markdown_counts_the_untested_scripts(tmp_path):
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    _write(tmp_path / "tested.py", '"""Summary."""\n')
    _write(tmp_path / "test_tested.py", '"""x"""\n')
    out = g.render_markdown(g.build_rows(tmp_path))
    assert "1 of all 2" in out


def test_markdown_escapes_a_pipe_in_a_summary(tmp_path):
    """A summary is free text; a literal pipe would silently add a column."""
    _write(tmp_path / "p.py", '"""Reads a | b."""\n')
    out = g.render_markdown(g.build_rows(tmp_path))
    assert r"Reads a \| b." in out


def test_markdown_ends_with_exactly_one_newline(tmp_path):
    """A second newline makes end-of-file-fixer rewrite the page and abort the cron."""
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    out = g.render_markdown(g.build_rows(tmp_path))
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


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
    verdicts = g.classify(repo, scripts)
    assert verdicts["cronned.py"][0] == "scheduled"
    assert "Nightly thing" in verdicts["cronned.py"][1]


def test_a_cron_reaches_through_its_wrapper_template(tmp_path):
    """build_docs.py is named only by docs-refresh.sh, which is what its cron runs."""
    repo, scripts = _repo(tmp_path)
    verdicts = g.classify(repo, scripts)
    assert verdicts["wrapped.py"][0] == "scheduled"
    assert "wrap.sh" in verdicts["wrapped.py"][1]


def test_a_prek_entry_and_a_workflow_step_are_gates(tmp_path):
    repo, scripts = _repo(tmp_path)
    verdicts = g.classify(repo, scripts)
    assert verdicts["gated.py"][0] == "gate"
    assert verdicts["shipped.py"][0] == "gate"


def test_an_imported_module_is_a_library(tmp_path):
    repo, scripts = _repo(tmp_path)
    verdicts = g.classify(repo, scripts)
    assert verdicts["lib.py"][0] == "library"
    assert "user.py" in verdicts["lib.py"][1]


def test_a_script_nothing_reaches_is_adhoc(tmp_path):
    repo, scripts = _repo(tmp_path)
    assert g.classify(repo, scripts)["lonely.py"] == (
        "adhoc",
        "no automated caller in the tree",
    )


def test_a_test_importing_its_subject_does_not_make_it_a_library(tmp_path):
    """Otherwise every tested entry point would read as a module nobody runs."""
    repo, scripts = _repo(tmp_path)
    _write(scripts / "test_lonely.py", '"""x"""\nimport lonely\n')
    assert g.classify(repo, scripts)["lonely.py"][0] == "adhoc"


def test_the_highest_kind_wins_when_a_script_is_reached_twice(tmp_path):
    """The costliest way it runs is the one that decides what a break costs."""
    repo, scripts = _repo(tmp_path)
    _write(tmp_path / "prek.toml", 'entry = "uv run python scripts/cronned.py"\n')
    assert g.classify(repo, scripts)["cronned.py"][0] == "scheduled"


def test_a_prose_mention_is_not_an_invocation(tmp_path):
    """Every CLAUDE.md and half the role defaults cite these scripts in backticks."""
    repo, scripts = _repo(tmp_path)
    _write(
        tmp_path / "ansible" / "roles" / "r" / "tasks" / "notes.yml",
        "- name: See `uv run python scripts/lonely.py` for detail\n",
    )
    assert g.classify(repo, scripts)["lonely.py"][0] == "adhoc"


def test_a_printed_message_naming_a_script_is_not_an_invocation(tmp_path):
    """deploy.sh prints the prune_worktrees command when the lock is busy."""
    repo, scripts = _repo(tmp_path)
    _write(
        tmp_path / "ansible" / "hand.sh",
        'echo "try uv run python scripts/lonely.py" >&2\n',
    )
    assert g.classify(repo, scripts)["lonely.py"][0] == "adhoc"


def test_a_sentence_in_a_python_string_is_not_an_invocation(tmp_path):
    """session-health.py prints "(scripts/deploy_tools/deploy_staleness.py, exit 4)" as prose."""
    repo, scripts = _repo(tmp_path)
    _write(
        tmp_path / ".claude" / "hooks" / "h.py",
        'print("the gate (scripts/lonely.py, exit 4) refuses")\n',
    )
    assert g.classify(repo, scripts)["lonely.py"][0] == "adhoc"


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
    verdicts = g.classify(repo, scripts)
    assert verdicts["runner.py"][0] == "gate"
    assert verdicts["lonely.py"][0] == "gate"
    assert "runner.py" in verdicts["lonely.py"][1]


def test_the_live_tree_classifies_the_names_we_already_know():
    """A derivation that quietly narrows reads exactly like one that works.

    Every name here has an invocation site someone can open. If one moves to `adhoc`,
    either the tree changed or the census stopped seeing a whole class of caller.
    """
    verdicts = g.classify()
    expected = {
        "build_docs.py": "scheduled",
        "gen_infra_map.py": "scheduled",
        "secret_rotation.py": "scheduled",
        "validate_k8s_manifests.py": "gate",
        "validate_compose_templates.py": "gate",
        "validate_shell_templates.py": "gate",
        "validate_ha_config.py": "gate",
        "validate_config_templates.py": "gate",
        "validate_grafana_dashboards.py": "gate",
        "deploy_tags.py": "gate",
        "deploy_staleness.py": "gate",
        "smoke_extract.py": "gate",
        "probe.py": "gate",
        "docs_provenance.py": "library",
        "probe_core.py": "library",
        # "gate" rather than "adhoc" since 2026-08-29, and the call graph changed rather than
        # the classifier. The staging gate has always ended in `deploy.sh`, but it reached it
        # through a script PIPED over ssh, which is invisible to a scan of the tree. The
        # restricted key's dispatcher is a role template, so the chain
        # dispatcher -> staging_gate_remote.sh -> deploy.sh is now visible and deploy.sh
        # inherits the caller's kind. A person still runs it by hand too; the gate is simply no
        # longer the caller nobody could see.
        "deploy.sh": "gate",
        "etcd_restore_drill.sh": "adhoc",
    }
    assert {name: verdicts[name][0] for name in expected} == expected


def test_every_reference_generator_is_reached_from_the_docs_cron():
    """build_docs.py runs them, docs-refresh.sh runs build_docs.py, a cron runs that."""
    verdicts = g.classify()
    generators = [n for n in verdicts if n.startswith("gen_reference_")]
    assert len(generators) >= 5
    assert all(verdicts[name][0] == "scheduled" for name in generators)


def test_a_test_that_names_the_path_counts_as_indirect_coverage(tmp_path):
    """gitops_tick.sh's five tests live in ansible/tests/deploy/test_gitops_manual_trigger.py."""
    repo, scripts = _repo(tmp_path)
    _write(scripts / "run.sh", "#!/bin/sh\n# Summary.\n")
    _write(
        repo / "ansible" / "tests" / "test_elsewhere.py",
        'WRAPPER = "scripts/run.sh"\n',
    )
    rows = {r["name"]: r for r in g.build_rows(scripts, repo)}
    assert rows["run.sh"]["tests"] == ""
    assert rows["run.sh"]["indirect_tests"] == "test_elsewhere.py"


def test_a_test_that_merely_says_the_word_is_not_coverage(tmp_path):
    """Matching the bare stem credited `deploy.sh` to a test that says "deploy"."""
    repo, scripts = _repo(tmp_path)
    _write(scripts / "deploy.sh", "#!/bin/sh\n# Summary.\n")
    _write(scripts / "test_other.py", 'MSG = "deploy the thing"\n')
    rows = {r["name"]: r for r in g.build_rows(scripts, repo)}
    assert rows["deploy.sh"]["indirect_tests"] == ""


def test_no_script_is_credited_to_another_scripts_own_test():
    """A path inside `test_<other>.py` is that test talking about this script, not testing it.

    Caught twice: this generator's own test names every script in the tree, and
    `test_deploy_detach_notify.py`'s first line names `scripts/deploy.sh`, which credited
    15 KB of shell that runs on every deploy to a test of the notifier. Asserted as a class
    rather than by name, so the next instance fails here instead of being noticed.
    """
    rows = g.build_rows()
    stems = {Path(r["name"]).stem for r in rows}
    laundered = {
        r["name"]: r["indirect_tests"]
        for r in rows
        if r["indirect_via"] == "path"
        and Path(r["indirect_tests"][len("test_") :]).stem in stems
    }
    assert laundered == {}


def test_an_import_counts_even_from_another_scripts_test():
    """The reject above is about path mentions; an import is real exercise.

    Asserted on the MECHANISM and on the credited file really importing the module, not on
    which filename wins. Several tests import `probe_core`, so pinning one name made this
    fail the moment probe.py was split and a different importer sorted first -- a rename in
    the suite is not a regression in the classifier.
    """
    rows = {r["name"]: r for r in g.build_rows()}
    credited = rows["probe_core.py"]["indirect_tests"]
    assert rows["probe_core.py"]["indirect_via"] == "import"
    assert credited.startswith("test_")
    hit = next(p for p in (g.SCRIPTS / "diagnostics").rglob(credited))
    assert re.search(r"^\s*import probe_core\b", hit.read_text(), re.MULTILINE)


def test_deploy_sh_is_credited_to_the_test_that_reads_it():
    """Not to `test_deploy_detach_notify.py`, whose first line merely names the path."""
    rows = {r["name"]: r for r in g.build_rows()}
    assert rows["deploy.sh"]["indirect_tests"] == "test_deploy_annotations.py"


def test_markdown_splits_the_scripts_by_how_they_run(tmp_path):
    repo, scripts = _repo(tmp_path)
    out = g.render_markdown(g.build_rows(scripts, repo))
    for heading in ("on a schedule", "Imported, never run", "Run by hand"):
        assert heading in out
