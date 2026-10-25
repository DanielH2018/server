"""A fake host for `hosts/bootstrap-host.sh`: stub commands, a HOMELAB_ROOT and an offline kit.

The script runs under stub `hostname`, `apt-get`, `dpkg`, `dpkg-query`, `systemctl`,
`systemd-tmpfiles` and `curl`, and the real `gpg`, `age-keygen`, `git`, `tar` and coreutils. Its
pins name production digests, so `pin` rewrites a digest in the scratch COPY of the script to the
fixture's own. A case that wants the production pin to refuse leaves it alone.
"""

import hashlib
import io
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

from lib.repo_paths import REPO

SCRIPT = REPO / "hosts" / "bootstrap-host.sh"
KEYRING = REPO / "hosts" / "keys" / "homelab-archive-keyring.gpg"
INSTALLED_KEYRING = "usr/share/keyrings/homelab-archive-keyring.gpg"
SOURCES = "etc/apt/sources.list.d/homelab.sources"
AGE_KEY = "etc/homelab/age.key"
NFPM_ARCHIVE = "nfpm_2.48.0_Linux_x86_64.tar.gz"
UV_ARCHIVE = "uv-x86_64-unknown-linux-gnu.tar.gz"

# Records what the installer was given, then creates the unit its mode names. A case that sets
# WRONG_UNIT=1 makes it create the other mode's unit.
INSTALLER = """#!/bin/sh
echo run >> "$HOMELAB_ROOT/installer.runs"
echo "$INSTALL_K3S_EXEC $INSTALL_K3S_VERSION skipenable=$INSTALL_K3S_SKIP_ENABLE \
skipstart=$INSTALL_K3S_SKIP_START url=${K3S_URL:-unset} token=${K3S_TOKEN:+set}" \
> "$HOMELAB_ROOT/installer.args"
mkdir -p "$HOMELAB_ROOT/etc/systemd/system"
mode=$INSTALL_K3S_EXEC
if [ -n "${WRONG_UNIT:-}" ]; then
    case "$mode" in server) mode=agent ;; *) mode=server ;; esac
fi
case "$mode" in
    server) : > "$HOMELAB_ROOT/etc/systemd/system/k3s.service" ;;
    agent) : > "$HOMELAB_ROOT/etc/systemd/system/k3s-agent.service" ;;
esac
"""
K3S_BINARY = "#!/bin/sh\necho 'k3s version v1.37.1+k3s1'\n"
# `uv run ... render --out DIR` writes the three packages at the paths the renderer uses, with
# homelab-tools under its architecture; the anchor call exits ANCHOR_EXIT.
UV_STUB = """#!/bin/sh
echo "$PWD: $*" >> "$HOMELAB_ROOT/uv.calls"
case "$*" in
    *bootstrap_anchor*) exit "${ANCHOR_EXIT:-0}" ;;
    *" render "*)
        for arg; do [ "$prev" = --out ] && out=$arg; prev=$arg; done
        for dir in packages/homelab-tools/amd64 packages/homelab-updater hosts/daniel-box; do
            mkdir -p "$out/$dir"; : > "$out/$dir/nfpm.yaml"
        done ;;
esac
"""
NFPM_STUB = """#!/bin/sh
for arg; do [ "$prev" = --target ] && target=$arg; prev=$arg; done
echo "$HOMELAB_VERSION" >> "$HOMELAB_ROOT/nfpm.versions"
: > "${target}$(basename "$PWD")_0_amd64.deb"
"""


def group(pattern: str, text: str, flags: int = 0) -> str:
    """The first group of `pattern` in `text`; a pattern that finds nothing fails the test."""
    match = re.search(pattern, text, flags)
    assert match, f"{pattern!r} matches nothing"
    return match[1]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tarball(member: str, body: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = body.encode()
        info = tarfile.TarInfo(member)
        info.size, info.mode = len(data), 0o755
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeHost:
    """A scratch directory holding the script, its keyring, stub commands and a HOMELAB_ROOT."""

    def __init__(self, tmp_path: Path, hostname: str = "daniel-pi") -> None:
        self.dir = tmp_path / "copy"
        self.dir.mkdir()
        shutil.copy(SCRIPT, self.dir / SCRIPT.name)
        self.keyring = self.dir / KEYRING.name
        shutil.copy(KEYRING, self.keyring)
        self.root = tmp_path / "root"
        self.root.mkdir()
        self.calls = tmp_path / "apt-get.calls"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.kit = tmp_path / "kit"
        self.kit.mkdir()
        self.log = tmp_path / "commands.log"
        self._stub("hostname", f"echo {hostname}")
        self._stub("apt-get", f'echo "$*" >> {self.calls}')
        self._stub("dpkg", "echo amd64")
        self._stub("dpkg-query", "exit 1")
        self._stub("systemctl", f'echo "systemctl $*" >> {self.log}')
        self._stub("systemd-tmpfiles", f'echo "tmpfiles $*" >> {self.log}')
        self._stub(
            "curl",
            'for arg; do [ "$prev" = -o ] && dest=$arg; prev=$arg; url=$arg; done\n'
            f'cp "{self.kit}/${{url##*/}}" "$dest"',
        )

    def _stub(self, name: str, body: str) -> None:
        stub = self.bin / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)

    def pin(self, old: str, new: str) -> None:
        """Replace the production digest `old` with `new` in the scratch copy of the script."""
        script = self.dir / SCRIPT.name
        text = script.read_text()
        assert old in text, f"the script no longer carries {old}"
        script.write_text(text.replace(old, new))

    def stock_kit(self) -> None:
        """Fill the kit with a fixture installer, k3s binary, nfpm and uv, and pin their digests."""
        script = (self.dir / SCRIPT.name).read_text()
        installer = INSTALLER.encode()
        (self.kit / "install.sh").write_bytes(installer)
        self.pin(group(r"K3S_INSTALL_SHA256=(\w+)", script), sha256(installer))
        (self.kit / "k3s").write_text(K3S_BINARY)
        (self.kit / "sha256sum-amd64.txt").write_text(
            f"{sha256(K3S_BINARY.encode())}  k3s\n{'0' * 64}  k3s-arm64\n"
        )
        for tool, archive, member, body in (
            ("nfpm", NFPM_ARCHIVE, "nfpm", NFPM_STUB),
            ("uv", UV_ARCHIVE, "uv-x86_64-unknown-linux-gnu/uv", UV_STUB),
        ):
            data = tarball(member, body)
            (self.kit / archive).write_bytes(data)
            # The first sha256 after the tool's amd64 key in the PINS block is amd64's.
            old = group(rf"  {tool}:\n    amd64:\n.*?sha256: (\w+)", script, re.DOTALL)
            self.pin(old, sha256(data))

    def clone(self, tmp_path: Path) -> Path:
        """A one-commit git clone holding the file the script reads host_python_version from."""
        clone = tmp_path / "clone"
        variables = clone / "ansible" / "inventory" / "group_vars"
        variables.mkdir(parents=True)
        (variables / "all.yml").write_text('host_python_version: "3.14.6"\n')
        git = [
            "git",
            "-C",
            str(clone),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
        ]
        subprocess.run([*git, "init", "-q"], check=True, capture_output=True)
        subprocess.run([*git, "add", "."], check=True, capture_output=True)
        subprocess.run(
            [*git, "commit", "-q", "-m", "x"], check=True, capture_output=True
        )
        return clone

    def run(self, *argv: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/sh", str(self.dir / SCRIPT.name), *argv],
            env={
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "HOMELAB_ROOT": str(self.root),
                **env,
            },
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def apt_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def commands(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def written(self) -> list[Path]:
        return [path for path in self.root.rglob("*") if path.is_file()]
