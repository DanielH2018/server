"""The secret-manifest digest path must not read a secret manifest's content into Ansible (#2574).

Secret manifests are rendered under `no_log` from decrypted SOPS values. The operator approved
digesting them on 2026-09-26 on one condition: hashing must not open a new read path over that
output. A `slurp`, a `lookup('file')` or a `set_fact` of the content would put the plaintext in
a task result or a host fact, where anything that later prints it prints the secret. So the
digest runs as `files/secret_hmac.py`, which receives paths and prints one hex digest, one
`no_log` task per file. This test holds the task file to that shape.

Run: uv run pytest ansible/tests/k8s/test_secret_digest_reads_no_content.py
"""

from _helpers import ANSIBLE, load_tasks, task_named, walk_tasks

TASKS = ANSIBLE / "roles/k8s/manifests/tasks"

# The names that put a task on the secret-digest path.
_SECRET_NAMES = (
    "manifests_secret_files",
    "manifests_secret_digest_key",
    "manifests_release_secret_hmac",
)

# Modules that return a file's content, or print a value, into the play.
_CONTENT_MODULES = {
    f"{prefix}{name}"
    for prefix in ("", "ansible.builtin.")
    for name in ("slurp", "fetch", "debug", "include_vars")
}

_CONTENT_LOOKUPS = ("lookup('file'", 'lookup("file"', "query('file'", 'query("file"')


def _on_secret_path(task) -> bool:
    return any(name in repr(task) for name in _SECRET_NAMES)


def _content_leaks(tasks) -> list[str]:
    """Every task on the secret-digest path that could carry a secret manifest's content."""
    problems = []
    for task in walk_tasks(tasks):
        if not _on_secret_path(task):
            continue
        name = task.get("name", "<unnamed>")
        text = repr(task)
        if _CONTENT_MODULES & set(task):
            problems.append(f"{name}: uses a content-returning or printing module")
        if any(lookup in text for lookup in _CONTENT_LOOKUPS):
            problems.append(f"{name}: reads a file through a lookup")
        loops_secrets = "manifests_secret_files" in repr(task.get("loop", ""))
        if loops_secrets and task.get("no_log") is not True:
            problems.append(f"{name}: loops the secret files without no_log")
    return problems


def test_the_digest_file_has_no_content_read_path():
    assert _content_leaks(load_tasks(TASKS / "release_digest.yml")) == []


def test_the_hmac_task_is_one_no_log_call_per_secret_file():
    # Named, so the census above cannot pass by finding no secret-path task at all.
    task = task_named(
        load_tasks(TASKS / "release_digest.yml"), "Digest the rendered secret manifests"
    )
    assert task["loop"] == "{{ manifests_secret_files | default([]) }}"
    assert task["no_log"] is True
    argv = task["ansible.builtin.command"]["argv"]
    helper = next(
        i for i, arg in enumerate(argv) if arg.endswith("/files/secret_hmac.py")
    )
    # Paths only: the key file and the rendered file, never a value read from either.
    assert argv[helper + 1 :] == [
        "{{ manifests_secret_digest_key }}",
        "{{ manifests_dest_dir }}/{{ item }}",
    ]


def test_a_slurp_of_a_secret_file_is_flagged():
    slurp = [
        {
            "name": "read it",
            "ansible.builtin.slurp": {"src": "{{ manifests_dest_dir }}/{{ item }}"},
            "loop": "{{ manifests_secret_files }}",
            "no_log": True,
        }
    ]
    assert _content_leaks(slurp) == [
        "read it: uses a content-returning or printing module"
    ]


def test_a_lookup_of_the_key_is_flagged():
    lookup = [
        {
            "name": "key",
            "ansible.builtin.set_fact": {
                "k": "{{ lookup('file', manifests_secret_digest_key) }}"
            },
        }
    ]
    assert _content_leaks(lookup) == ["key: reads a file through a lookup"]


def test_a_secret_loop_without_no_log_is_flagged():
    loud = [
        {
            "name": "hash",
            "ansible.builtin.command": {"argv": ["true"]},
            "loop": "{{ manifests_secret_files | default([]) }}",
        }
    ]
    assert _content_leaks(loud) == ["hash: loops the secret files without no_log"]
