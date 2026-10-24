"""The two scans of the `host_packages` job that read rendered package roots (H4.1c).

H4's *Steps, as PRs* gives the job a `gitleaks dir` scan and a placeholder check, both over one
named set of package roots. A package root is the `root/` directory beside a rendered
`nfpm.yaml`, the tree dpkg installs. Each member is found by the `name:` in its `nfpm.yaml`, so a
renamed package fails the scan by name where a path glob would scan less and pass. A member
missing from the render is a finding too (failure class 4, selector drift).

  * `scan` runs `gitleaks dir .` with each root as the working directory. gitleaks reports
    paths relative to that directory, so `.gitleaks.toml`'s anchored entry for
    `^usr/lib/homelab/secrets/[^/]+\\.sops\\.yaml$` exempts a package's ciphertext and nothing
    else. A path argument would make gitleaks report absolute paths and the entry would match
    nothing.
  * `prove` is that scan's red proof on the real binary. A token at the ciphertext path must
    pass, and the same token at `etc/` must fail, so a gitleaks release that changed its path
    shape fails here before a real ciphertext reaches the scan.
  * `placeholder_problems` reads the declared outputs under `usr/lib/homelab/secret-templates/`
    and the compose files under `usr/lib/homelab/compose/`, and names every `${key}` whose key
    the root's own SOPS file does not carry. It reads key names only and never decrypts, and it
    imports nothing from the renderer, so H4 keeps its single edge to H3.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
import subprocess
import tempfile
from pathlib import Path

from lib import yaml_fast

# The named set. Add a package here when it joins the scans. daniel-server joined in H4.2.
NAMED = ("homelab-updater", "homelab-daniel-pi", "homelab-daniel-server")

SOPS_DIR = "usr/lib/homelab/secrets"
TEMPLATES = "usr/lib/homelab/secret-templates"
COMPOSE = "usr/lib/homelab/compose"
# homelab-secrets' own single pass: `$$` first, so `$${key}` leaves literal text, then `${name}`.
# A `${` that does not close as `${name}` is an error in a template, which the script refuses.
TOKEN = re.compile(r"\$\$|\$\{([A-Za-z0-9_]+)\}|\$\{")


def package_roots(
    render: Path, names: tuple[str, ...] = NAMED
) -> tuple[dict[str, Path], list[str]]:
    """Each named package's root under `render`, and a `[FAIL]` line per member not found."""
    found = {}
    for spec in sorted(render.rglob("nfpm.yaml")):
        name = (yaml_fast.safe_load(spec.read_text()) or {}).get("name")
        if name in names:
            found[name] = spec.parent / "root"
    problems = [
        f"[FAIL] {name}: no package of this name is rendered under {render}"
        for name in names
        if name not in found
    ]
    problems += [
        f"[FAIL] {name}: {root} is not a directory"
        for name, root in found.items()
        if not root.is_dir()
    ]
    return {n: r for n, r in found.items() if r.is_dir()}, problems


def _run_gitleaks(gitleaks: Path, config: Path, root: Path) -> int:
    return subprocess.run(
        [str(gitleaks), "dir", ".", "--config", str(config.resolve())]
        + ["--no-banner", "--redact"],
        cwd=root,
        check=False,
    ).returncode


def scan(render: Path, gitleaks: Path, config: Path) -> list[str]:
    """Run gitleaks over each named root; a `[FAIL]` line per root it flags or cannot find."""
    roots, problems = package_roots(render)
    for name, root in roots.items():
        status = _run_gitleaks(gitleaks, config, root)
        if status:
            problems.append(f"[FAIL] {name}: gitleaks exited {status}")
        else:
            print(f"[PASS] {name}: gitleaks found nothing", file=_sys.stderr)
    return problems


def prove(gitleaks: Path, config: Path) -> list[str]:
    """The scan's red proof: a token at the ciphertext path passes, and at `etc/` it is flagged."""
    # Split so this file holds no token, and the repo's own gitleaks hook stays quiet.
    token = "ghp_" + "aB3dE5fG7hI9jK1lM2nO4pQ6rS8tU0vW2xY4"
    problems = []
    for where, flagged in ((f"{SOPS_DIR}/x.sops.yaml", False), ("etc/x.conf", True)):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / where
            path.parent.mkdir(parents=True)
            path.write_text(f'token = "{token}"\n')
            status = _run_gitleaks(gitleaks, config, Path(scratch))
        if (status != 0) != flagged:
            problems.append(
                f"[FAIL] gitleaks exited {status} on a token at {where}; "
                f"it must {'flag' if flagged else 'pass'} it"
            )
    return problems


def host_keys(root: Path) -> set[str]:
    """The key names of the root's SOPS files, which sit in plaintext beside the ciphertext."""
    keys: set[str] = set()
    for sops in sorted((root / SOPS_DIR).glob("*.sops.yaml")):
        keys |= set(yaml_fast.safe_load(sops.read_text()) or {}) - {"sops"}
    return keys


def placeholder_problems(root: Path) -> list[str]:
    """Each placeholder in `root`'s templates and compose files that its SOPS file lacks."""
    known = host_keys(root)
    problems = []
    for tree, strict in ((TEMPLATES, True), (COMPOSE, False)):
        for path in sorted(p for p in (root / tree).rglob("*") if p.is_file()):
            text = path.read_text()
            for match in TOKEN.finditer(text):
                if match.group(0) == "$$":
                    continue
                key = match.group(1)
                where = f"{path.relative_to(root)}:{text.count(chr(10), 0, match.start()) + 1}"
                if key is None and strict:
                    problems.append(f"{where}: `${{` does not close as `${{name}}`")
                elif key is not None and key not in known:
                    problems.append(
                        f"{where}: placeholder ${{{key}}} names no key of the root's SOPS file"
                    )
    return problems


def check_placeholders(render: Path) -> list[str]:
    """`placeholder_problems` over the named set, as `[FAIL]` lines."""
    roots, problems = package_roots(render)
    for name, root in roots.items():
        problems += [f"[FAIL] {name}: {p}" for p in placeholder_problems(root)]
    return problems
