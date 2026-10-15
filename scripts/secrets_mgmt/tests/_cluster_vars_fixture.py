"""A throwaway SOPS tree for the `cluster_vars.py` tests: two age keys made in `tmp_path`.

Every key here is generated per test by `age-keygen`, never a real one. The fixture
`.sops.yaml` has the real file's three rules in the real order: the cluster file and the
cluster manifests go to a "cluster" key and a "host" key, and the generic rule to the host
key alone. Every sops call runs with `SOPS_AGE_KEY_FILE` on the host key and
`XDG_CONFIG_HOME` on an empty directory, so sops cannot also load this machine's own
`~/.config/sops/age/keys.txt`, and `SOPS_AGE_KEY` and `SOPS_AGE_KEY_CMD` are unset.

`sops` and `age-keygen` must be installed. Without them the fixture fails rather than skips:
CI's `pytest` job installs both, and a skip there would read as a pass.

`ansible/tests/leakguard.py` puts an exit-127 shim for `sops` first on PATH, because a test
that reached the real binary could decrypt the real `secrets.yml`. These tests need the real
binary and reach nothing but `tmp_path`, so the fixture links it into a directory of its own
and puts that first on the PATH it hands every sops call, the way leakguard lets a test that
brings its own binary win.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import yaml

from lib.proc_testing import path_with
from secrets_mgmt.cluster_vars_check import Expected, ManifestEntry, Tree

# A sops or age-keygen call on a fixture this small takes well under a second.
TIMEOUT = 30

# Distinct enough that a scan of every file for them cannot match ciphertext by chance.
VALUES = {
    "alpha_one": "fixture-alpha-7f3q",
    "beta_two": "fixture beta {single} brace",
    "gamma_flag": "plainwordzq",
    "blob_key": "-----FIXTURE BLOCK-----\nline-two-q9x\n-----END FIXTURE-----\n",
    "number_key": 7364519,
    "unused_key": "fixture-unused-k2",
}
EXPECTED = Expected(
    keys=frozenset({"alpha_one", "beta_two", "gamma_flag"}),
    standalone=frozenset({"gamma_flag"}),
    manifests=(
        ManifestEntry(
            "demo",
            "blob-secret.yaml",
            "apiVersion: v1\nkind: Secret\nmetadata:\n  name: demo-blob\n"
            "  namespace: homelab\ntype: Opaque\nstringData:\n"
            '  blob: "HOMELAB_SECRET__blob_key__END"\n',
            frozenset({"blob_key"}),
        ),
        ManifestEntry(
            "acct",
            "number-secret.yaml",
            "apiVersion: v1\nkind: Secret\nmetadata:\n  name: acct-number\n"
            "  namespace: homelab\ntype: Opaque\nstringData:\n"
            '  account: "HOMELAB_SECRET__number_key__END"\n',
            frozenset({"number_key"}),
        ),
    ),
)


def plaintexts(values: dict = VALUES) -> list[str]:
    """Each fixture value's text, a multi-line one split into its lines."""
    return [
        line for value in values.values() for line in str(value).splitlines() if line
    ]


@dataclass(frozen=True)
class SopsTree:
    """A fixture repository whose SOPS files only throwaway keys decrypt.

    Attributes:
        tree: The fixture repository's paths.
        run: `subprocess.run` bound to the isolated sops environment.
        cluster: The cluster key's public half.
        host: The host key's public half.
    """

    tree: Tree
    run: partial
    cluster: str
    host: str

    def encrypt_to(self, plaintext: str, filename_override: str, kind: str) -> str:
        """`plaintext` encrypted by the rule `filename_override` resolves to, from stdin."""
        return self.run(
            [
                "sops",
                "--config",
                str(self.tree.sops_config),
                "encrypt",
                "--filename-override",
                filename_override,
                "--input-type",
                kind,
                "--output-type",
                kind,
                "/dev/stdin",
            ],
            input=plaintext,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def set_secrets(self, values: dict) -> None:
        """Rewrite the fixture `secrets.yml` from `values`, as a fresh encrypt."""
        text = yaml.safe_dump(values, sort_keys=True)
        self.tree.secrets.write_text(
            self.encrypt_to(text, str(self.tree.secrets), "yaml")
        )

    def decrypt(self, path: Path) -> str:
        return self.run(
            ["sops", "decrypt", str(path)], capture_output=True, text=True, check=True
        ).stdout

    def snapshot(self) -> dict[str, bytes]:
        """Every file under the fixture root, by relative path."""
        root = self.tree.root
        return {
            p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }


def _keypair(path: Path, env: dict[str, str]) -> str:
    subprocess.run(
        ["age-keygen", "-o", str(path)],
        env=env,
        capture_output=True,
        check=True,
        timeout=TIMEOUT,
    )
    return subprocess.run(
        ["age-keygen", "-y", str(path)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=TIMEOUT,
    ).stdout.strip()


def real_binary(name: str) -> Path | None:
    """`name` on PATH, passing over leakguard's shim, or None when it is not installed."""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            continue
        if b"LEAKGUARD" not in candidate.read_bytes()[:4096]:
            return candidate
    return None


def make_sops_tree(base: Path, values: dict = VALUES) -> SopsTree:
    """A fixture repository under `base/repo`, its keys under `base/keys`."""
    sops, keygen = real_binary("sops"), shutil.which("age-keygen")
    assert sops and keygen, "the cluster_vars write tests need sops and age-keygen"
    keys, empty, bin_dir = base / "keys", base / "empty-config", base / "bin"
    root = base / "repo"
    for directory in (keys, empty, bin_dir, root / "ansible" / "vars"):
        directory.mkdir(parents=True)
    (bin_dir / "sops").symlink_to(sops)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("SOPS_AGE_KEY", "SOPS_AGE_KEY_CMD")
    }
    env.update(
        PATH=path_with(bin_dir, env=env),
        SOPS_AGE_KEY_FILE=str(keys / "host.txt"),
        XDG_CONFIG_HOME=str(empty),
    )
    cluster, host = (
        _keypair(keys / "cluster.txt", env),
        _keypair(keys / "host.txt", env),
    )
    tree = Tree(root)
    tree.sops_config.write_text(
        "creation_rules:\n"
        "  - path_regex: vars/cluster-vars\\.sops\\.env$\n"
        f'    age: "{cluster},{host}"\n'
        "  - path_regex: vars/cluster-manifests/[^/]+\\.sops\\.yaml$\n"
        "    encrypted_regex: ^(data|stringData)$\n"
        f'    age: "{cluster},{host}"\n'
        "  - path_regex: .*(vars|secrets)\\/.*\\.ya?ml$\n"
        f'    age: "{host}"\n'
    )
    # A call that passes its own `timeout`, as every production sops call does, overrides this.
    run = partial(subprocess.run, env=env, timeout=TIMEOUT)
    sops_tree = SopsTree(tree, run, cluster, host)
    sops_tree.set_secrets(values)
    return sops_tree
