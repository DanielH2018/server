"""`affected_lib/trace.py`: what an `strace -f -y` log says a process tree touched.

The lines are strace 6.8's own, cut down to the fields the parser reads.

Run: uv run pytest scripts/dev/tests/ci/test_affected_trace.py
"""

from dev.affected_lib import trace

REPO = "/repo"


def _parse(*lines: str) -> trace.Touched:
    return trace.parse(lines, REPO)


def test_a_trace_separates_what_was_found_from_what_was_asked_for_and_what_was_listed():
    touched = _parse(
        '100 openat(AT_FDCWD</repo>, "docs/a.md", O_RDONLY|O_CLOEXEC) = 3</repo/docs/a.md>',
        '100 newfstatat(AT_FDCWD</repo>, "docs/gone.md", 0x7ffc, 0) = -1 ENOENT (No such file or directory)',
        '100 openat(AT_FDCWD</repo>, "docs", O_RDONLY|O_DIRECTORY) = 4</repo/docs>',
        "100 getdents64(4</repo/docs>, 0x55d0 /* 12 entries */, 32768) = 400",
        '100 access("/repo/notes/x/../y.txt", R_OK) = -1 EACCES (Permission denied)',
    )

    assert touched.reads == {"/repo/docs/a.md", "/repo/docs"}
    assert touched.missing == {"/repo/docs/gone.md"}
    assert touched.listed == {100: {"/repo/docs"}}
    assert touched.root_pid == 100


def test_a_path_through_a_symlink_counts_under_both_names():
    touched = _parse(
        '100 openat(AT_FDCWD</repo>, "current/a.md", O_RDONLY) = 3</repo/v2/a.md>'
    )

    assert touched.reads == {"/repo/current/a.md", "/repo/v2/a.md"}


def test_a_relative_path_resolves_against_the_directory_of_the_process_that_asked():
    """`stat` takes no directory descriptor, so the parser tracks each process's own."""
    touched = _parse(
        '100 stat("pyproject.toml", {st_mode=S_IFREG|0644, st_size=1, ...}) = 0',
        "100 clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|SIGCHLD) = 101",
        '101 chdir("/tmp/scratch") = 0',
        '101 stat(".git", 0x7ffd) = -1 ENOENT (No such file or directory)',
        "101 vfork( <unfinished ...>",
        '102 stat("HEAD", {st_mode=S_IFREG|0644, st_size=1, ...}) = 0',
        '100 stat("uv.lock", {st_mode=S_IFREG|0644, st_size=1, ...}) = 0',
    )

    assert touched.reads == {
        "/repo/pyproject.toml",
        "/tmp/scratch",
        "/tmp/scratch/HEAD",
        "/repo/uv.lock",
    }
    assert touched.missing == {"/tmp/scratch/.git"}


def test_a_call_another_process_interrupted_is_read_as_one_call():
    touched = _parse(
        '100 openat(AT_FDCWD</repo>, "k8s/a.yml", O_RDONLY <unfinished ...>',
        '101 openat(AT_FDCWD</repo>, "k8s/b.yml", O_RDONLY) = 5</repo/k8s/b.yml>',
        "100 <... openat resumed>) = -1 ENOENT (No such file or directory)",
    )

    assert touched.reads == {"/repo/k8s/b.yml"}
    assert touched.missing == {"/repo/k8s/a.yml"}


def test_an_escaped_name_is_read_as_the_path_it_stands_for():
    touched = _parse(
        r'100 openat(AT_FDCWD</repo>, "docs/caf\303\251 \"x\".md", O_RDONLY) = 3'
    )

    assert touched.reads == {'/repo/docs/café "x".md'}
