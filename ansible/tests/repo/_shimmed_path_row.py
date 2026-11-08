"""The census rows that keep a shipped script from reaching a leakguard-shimmed binary for real.

`leakguard`'s `Popen` wrapper swaps an absolute shimmed program for its shim, but only for the
test's direct child (#6463). A script a test runs that names `/usr/bin/logger` itself execs
the real binary, and nothing records it (#6485). This row reads the scripts a test can run:
shell and Python sources, their `.j2` templates, and every tracked executable, outside the
tests' own directories. Unit files, cron `job:` lines and k8s manifests are left out because
no test execs them, and a manifest's `/usr/bin/kubectl` is a path inside a container image.

The second row reads the same scripts for a `PATH` that puts a fixed directory in front of the
one the script inherited, such as `export PATH="/usr/local/bin:${PATH}"`. leakguard puts its
stub directory first on the test's `PATH`, so that line hands every bare `k3s` or `kubectl`
after it the real binary (#7623). Appending the directory keeps it reachable from a bare
environment and keeps a test's stubs first.
"""

import re

from _helpers import REPO
from _row_table import Census, Subject, tracked
from leakguard import SHIMMED_BINARIES
from lib.proc_testing import run

_SCRIPT_SUFFIXES = (".sh", ".bash", ".py", ".sh.j2", ".bash.j2", ".py.j2")

# A real PATH directory, then a shimmed name. The lookbehind keeps a path that only ends in
# `/bin/<name>`, such as `/usr/lib/homelab/bin/sops`, out.
SHIMMED_PATH = re.compile(
    r"(?<![\w./-])/(?:usr/(?:local/)?)?s?bin/(?P<name>"
    + "|".join(re.escape(name) for name in SHIMMED_BINARIES)
    + r")(?![\w.-])"
)

# The overridable spellings: the path is only the default of a variable a test can set.
_OVERRIDABLE_BEFORE = re.compile(
    r"(?:\$\{\w+:?[-=]"
    r"|(?:environ\.get|getenv)\(\s*[\"']\w+[\"']\s*,\s*)[\"']?$"
)


def hardcoded_shimmed_paths(subject: Subject) -> list[str]:
    """Each non-comment line naming a shimmed binary by absolute path, not as a default."""
    hits = []
    for n, line in enumerate(subject.text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if any(
            not _OVERRIDABLE_BEFORE.search(line[: m.start()])
            for m in SHIMMED_PATH.finditer(line)
        ):
            hits.append(f"line {n}: {line.strip()}")
    return hits


def _executables() -> frozenset[str]:
    listed = run(["git", "ls-files", "-s", "-z"], cwd=REPO, check=True)
    return frozenset(
        entry.split("\t", 1)[1]
        for entry in listed.stdout.split("\0")
        if entry.startswith("100755 ")
    )


def scripts_a_test_can_run() -> list[str]:
    """Tracked shell and Python sources and executables outside `tests/` and `docs/`."""
    executables = _executables()
    return [
        rel
        for rel in tracked("*")
        if (rel.endswith(_SCRIPT_SUFFIXES) or rel in executables)
        and "tests" not in rel.split("/")[:-1]
        and not rel.startswith("docs/")
        and not rel.endswith("conftest.py")
    ]


SHIMMED_PATHS = Census(
    name="scripts-run-no-shimmed-binary-by-hardcoded-path",
    reason=(
        "leakguard shims `logger`, `curl`, `kubectl` and the rest of `SHIMMED_BINARIES` for "
        "a test's direct child only. A script under test that runs `/usr/bin/logger` itself "
        "reaches the real binary with nothing recorded (#6485). Read the path from a variable "
        'the test can set: `${LOGGER:-/usr/bin/logger}` in shell, `os.environ.get("X", '
        '"/usr/bin/x")` in Python, or call the bare name and let PATH resolve it.'
    ),
    files=scripts_a_test_can_run,
    offence=hardcoded_shimmed_paths,
    red=(
        Subject("a.sh", "printf x | /usr/bin/logger -t check\n"),
        Subject("b.sh.j2", 'export LONGHORN_JOURNALCTL="/usr/bin/journalctl"\n'),
        Subject("c.py", 'subprocess.run(["/usr/local/bin/kubectl", "get", "pods"])\n'),
        Subject("e.sh", "/usr/local/bin/k3s etcd-snapshot ls\n"),
        Subject("d.sh", 'X="${A:-/usr/bin/curl}" /usr/bin/curl -fsS "$URL"\n'),
    ),
    green=(
        Subject("a.sh", "# scoped to /usr/bin/rsync by sudoers\n"),
        Subject("b.sh.j2", 'export J="${LONGHORN_JOURNALCTL:-/usr/bin/journalctl}"\n'),
        Subject("c.py", 'SOPS = ROOT + "/usr/lib/homelab/bin/sops"\n'),
        Subject("d.py", 'CURL = os.environ.get("CURL", "/usr/bin/curl")\n'),
        Subject("e.sh", "logger -t check /usr/bin/loggerd /usr/bin/sshd\n"),
    ),
    min_matches=500,
    must_find=frozenset(
        {
            "hosts/components/k3s-host-checks/templates/longhorn-backup-health.sh.j2",
            "hosts/components/claude-code/files/homelab_rootq.py",
            "hosts/lib/homelab-secrets",
            "hosts/components/pi-peer-backup-target/pi-peer-backup-shell.sh",
        }
    ),
    allow={
        "hosts/components/claude-code/files/homelab_rootq.py": (
            "it runs under sudo, so a binary path read from the environment would let the "
            "caller choose what root runs; its tests import it, so leakguard's `Popen` "
            "wrapper still sees each launch as the test's direct child"
        ),
        "hosts/components/claude-code/files/homelab-takeover-check.sh": (
            "it runs under sudo and ignores every override as root, for rootq's reason; as "
            "non-root it takes `HOMELAB_TC_KUBECTL`, which its tests set"
        ),
        "hosts/bootstrap-host.sh": (
            "every use is `$ROOT$K3S_BIN`, and its tests point `HOMELAB_ROOT` at a fake root"
        ),
        "hosts/components/k3s-host-checks/templates/longhorn-backup-health.sh.j2": (
            "the shim only exports the command; the reader runs it, and every test that runs "
            "the reader passes `LONGHORN_BACKUP_KUBECTL` a stub"
        ),
        "scripts/validate/validate_lib/secrets_case.py": (
            "`DOCKER_STUB_PATH` is where the case writes a stub INSIDE a test container; "
            "it names no binary on this host"
        ),
    },
)


# `PATH=/dir...:$PATH` or `PATH="/dir...:${PATH}"`: a fixed directory ahead of the inherited PATH.
_PREPENDED_PATH = re.compile(r"(?<![\w$])PATH=[\"']?/[^\s\"';]*:\$\{?PATH\b")


def prepended_paths(subject: Subject) -> list[str]:
    """Each non-comment line that puts a fixed directory ahead of the inherited `PATH`."""
    return [
        f"line {n}: {line.strip()}"
        for n, line in enumerate(subject.text.splitlines(), 1)
        if not line.lstrip().startswith("#") and _PREPENDED_PATH.search(line)
    ]


PREPENDED_PATHS = Census(
    name="scripts-append-to-path-rather-than-prepend",
    reason=(
        "leakguard's stub directory is first on a test's PATH. A script under test that puts "
        "`/usr/local/bin` in front of the PATH it inherited runs the real `k3s`, `kubectl`, "
        "`crictl` or `ctr` for every bare call after that line, with nothing recorded (#7623). "
        'Append instead: `export PATH="${PATH}:/usr/local/bin"`.'
    ),
    files=scripts_a_test_can_run,
    offence=prepended_paths,
    red=(
        Subject("a.sh.j2", 'export PATH="/usr/local/bin:${PATH}"\n'),
        Subject(
            "b.sh", "PATH=/usr/local/sbin:/usr/local/bin:$PATH k3s kubectl get pods\n"
        ),
    ),
    green=(
        Subject("a.sh.j2", 'export PATH="${PATH}:/usr/local/bin"\n'),
        Subject("b.sh", '# was: export PATH="/usr/local/bin:${PATH}"\n'),
        Subject("c.sh", 'PATH="$STUBS:$PATH" bash driver.sh\n'),
    ),
    min_matches=500,
    must_find=frozenset(
        {
            "hosts/components/hypervisor/templates/etcd-restore-drill-vm.sh.j2",
            "hosts/components/registry-gc/templates/registry-gc.sh.j2",
        }
    ),
)
