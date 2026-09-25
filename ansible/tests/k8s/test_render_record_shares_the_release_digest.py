"""The render record and the release record compute one digest, through one file (#2574).

`probe.py releases --stale-only` is meant to compare a release record's `manifests_digest`
against a render record's. That comparison is only sound while both digests come from the same
code: a copied expression drifts one edit at a time, and each drift reads as a stale service
nobody can clear. So `release_stamp.yml` and `render_record.yml` both include
`release_digest.yml`, and neither hashes anything itself.

The render record's own dict must not share a name with the mode flag. The render cron passes
`manifests_render_record=true` with -e, extra vars outrank task vars, and the first fleet run
wrote the string "true" as all 45 records.

Run: uv run pytest ansible/tests/k8s/test_render_record_shares_the_release_digest.py
"""

from _helpers import ANSIBLE, load_tasks, load_yaml, task_named, walk_tasks

MANIFESTS = ANSIBLE / "roles/k8s/manifests"
TASKS = MANIFESTS / "tasks"
DIGEST_FILE = "release_digest.yml"


def _includes(tasks) -> set[str]:
    return {
        t["ansible.builtin.include_tasks"]
        for t in walk_tasks(tasks)
        if "ansible.builtin.include_tasks" in t
    }


def _hashes_itself(tasks) -> bool:
    return "hash(" in repr(tasks)


def _shadowed_vars(tasks) -> set[str]:
    """Task-level `vars:` names that a role default of the same name lets -e override."""
    defaults = set(load_yaml(MANIFESTS / "defaults/main.yml"))
    return {
        name for t in walk_tasks(tasks) for name in (t.get("vars") or {})
    } & defaults


def test_both_records_take_the_digest_from_the_shared_file():
    for name in ("release_stamp.yml", "render_record.yml"):
        tasks = load_tasks(TASKS / name)
        assert DIGEST_FILE in _includes(tasks), name
        assert not _hashes_itself(tasks), f"{name} computes a digest of its own"


def test_a_task_file_with_its_own_digest_is_flagged():
    copied = [{"ansible.builtin.set_fact": {"d": "{{ x | to_json | hash('sha256') }}"}}]
    assert _hashes_itself(copied)


def test_the_digest_stats_the_ordinary_manifests_only():
    stat = task_named(
        load_tasks(TASKS / DIGEST_FILE), "Checksum the rendered manifests"
    )
    assert stat["loop"] == "{{ manifests_files | default([]) }}"


def test_the_render_entry_is_not_shadowed_by_the_mode_flag():
    assert not _shadowed_vars(load_tasks(TASKS / "render_record.yml"))


def test_a_task_var_named_like_a_role_default_is_flagged():
    shadowed = [{"vars": {"manifests_render_record": {"service": "x"}}}]
    assert _shadowed_vars(shadowed) == {"manifests_render_record"}


def test_the_render_record_is_written_only_by_a_render_mode_dry_run():
    include = task_named(load_tasks(TASKS / "main.yml"), "Record the render digest")
    assert include["ansible.builtin.include_tasks"] == "render_record.yml"
    assert include["when"] == ["k8s_dry_run | bool", "manifests_render_record | bool"]
