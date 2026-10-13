"""The round trip: build each placeholder directory and its stub twin with `flux build`, then compare.

`shadow_diff.py roundtrip` renders both modes into one directory, `DIR/placeholder/` and
`DIR/stub/`, each in C1's layout. For every directory under `cluster/` it writes two
throwaway Flux Kustomizations under `DIR/kustomizations/` that differ only in `postBuild`, and
runs `flux build kustomization <dir> --dry-run --kustomization-file <file> --path <dir>` once
per mode:

- **Placeholder side, a directory that substitutes.** The file carries every key's R1 stub
  value inline in `postBuild.substitute`, and the command adds `--strict-substitute`.
- **Placeholder side, a directory that does not substitute.** The file has no `postBuild`, so
  the comparison proves the renderer escaped nothing there.
- **Stub side.** The file has no `postBuild`, so nothing is substituted.

Which directories substitute comes from the render's own key sets
(`placeholders.PlaceholderRender.key_sets`), never from the output. `flux build` runs the
controller's own `SubstituteVariables` per resource and serialises both sides with the same
code, so the outputs compare byte for byte, and the report names the first object that
differs. It prints no file content: every value on either side is a stub.

`roundtrip` needs the pinned flux CLI. Where `flux` is not on `PATH`, it refuses and never
reports a pass. `docs/homelab-render.md` is the operator's page.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from lib import yaml_fast
from lib.exit_codes import SHADOW_DIFFERENT, SHADOW_OK
from renderer.renderer_lib.cluster import (
    DEFERRED_SUFFIX,
    RenderedTag,
    render_tag,
    write_cluster,
)
from renderer.renderer_lib.placeholders import PlaceholderRender, render_placeholders

PLACEHOLDER, STUB = "placeholder", "stub"
KUSTOMIZATIONS = "kustomizations"
# One namespace for every throwaway Kustomization: `flux build` stamps the Kustomization's
# name and namespace on each object it builds, so the two sides must share both.
FLUX_NAMESPACE = "flux-system"
BUILD_TIMEOUT_SECONDS = 120

# The `run` callable `build` takes: the subprocess call, or a test's stand-in for it.
Runner = Callable[..., subprocess.CompletedProcess]


class FluxMissing(RuntimeError):
    """No `flux` on `PATH`, so no round trip can run, and none may report a pass."""


def find_flux() -> str:
    """The path of the `flux` binary on `PATH`.

    Raises:
        FluxMissing: There is none.
    """
    path = shutil.which("flux")
    if path is None:
        raise FluxMissing(
            "no flux CLI on PATH. The round trip runs in CI, which installs the pinned "
            "CLI; it reports no pass without one"
        )
    return path


def kustomization_file(name: str, substitute: Mapping[str, str] | None) -> str:
    """A throwaway Flux Kustomization `name`, with `postBuild.substitute` only when given."""
    spec: dict = {
        "interval": "10m",
        "path": "./",
        "prune": True,
        "sourceRef": {"kind": "GitRepository", "name": FLUX_NAMESPACE},
    }
    if substitute is not None:
        spec["postBuild"] = {"substitute": dict(sorted(substitute.items()))}
    return yaml.safe_dump(
        {
            "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
            "kind": "Kustomization",
            "metadata": {"name": name, "namespace": FLUX_NAMESPACE},
            "spec": spec,
        },
        sort_keys=False,
        width=4096,
    )


def build(
    flux: str, name: str, path: Path, kfile: Path, strict: bool, run: Runner
) -> subprocess.CompletedProcess:
    """Run `flux build kustomization` over `path` with the Kustomization in `kfile`."""
    command = [
        flux,
        "build",
        "kustomization",
        name,
        "--path",
        str(path),
        "--kustomization-file",
        str(kfile),
        "--dry-run",
    ]
    if strict:
        command.append("--strict-substitute")
    return run(
        command,
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT_SECONDS,
        check=False,
    )


def documents(output: str) -> list[str]:
    """The YAML documents of a `flux build` output, as text, empty ones dropped."""
    docs, current = [], []
    for line in output.splitlines(keepends=True):
        if line.rstrip("\n") == "---":
            docs.append("".join(current))
            current = []
        else:
            current.append(line)
    docs.append("".join(current))
    return [d for d in docs if d.strip()]


def object_name(doc: str) -> str:
    """`Kind/namespace/name` of one YAML document, for a report line."""
    try:
        obj = yaml_fast.safe_load(doc) or {}
    except yaml.YAMLError:
        return "an unparseable document"
    meta = obj.get("metadata") or {}
    return f"{obj.get('kind')}/{meta.get('namespace', '')}/{meta.get('name')}"


def first_difference(placeholder: str, stub: str) -> str | None:
    """The first object that differs between two `flux build` outputs, or None if equal."""
    if placeholder == stub:
        return None
    left, right = documents(placeholder), documents(stub)
    for a, b in zip(left, right, strict=False):
        if a != b:
            return f"{object_name(b)} differs"
    if len(left) != len(right):
        return f"{len(left)} objects on the placeholder side, {len(right)} on the stub side"
    return "the outputs differ outside any object"


@dataclass
class Report:
    """The verdict of one round trip.

    Attributes:
        tags: The tags rendered on both sides.
        skipped: The requested tags placeholder mode skipped, from `PLACEHOLDER_DEFERRED`.
        directories: Every directory compared.
        substituting: The directories built with `postBuild`.
        objects: The objects the stub side built, summed over the directories.
        differences: One line per directory whose two builds differ or fail.
    """

    tags: list[str]
    skipped: list[str]
    directories: list[str] = field(default_factory=list)
    substituting: list[str] = field(default_factory=list)
    objects: int = 0
    differences: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        deferred = sum(d.endswith(DEFERRED_SUFFIX) for d in self.directories)
        lines = list(self.differences)
        if self.skipped:
            lines.append(
                f"roundtrip: skipped, in PLACEHOLDER_DEFERRED: {', '.join(self.skipped)}"
            )
        lines.append(
            f"roundtrip: {len(self.tags)} tags, {deferred} deferred directories, "
            f"{len(self.substituting)} substituting, {self.objects} objects, "
            f"{len(self.differences)} differences"
        )
        return lines

    def exit_code(self) -> int:
        return SHADOW_DIFFERENT if self.differences else SHADOW_OK


@dataclass(frozen=True)
class Sides:
    """The two renders a round trip compares, written under one directory.

    Attributes:
        root: The directory both sides and the throwaway Kustomizations live under.
        placeholder: The placeholder render, whose key sets decide `postBuild`.
        tags: The tags rendered on both sides.
    """

    root: Path
    placeholder: PlaceholderRender
    tags: list[str]

    def cluster(self, side: str) -> Path:
        return self.root / side / "cluster"


def render_sides(root: Path, tags: list[str], keys: list[str], stubs: dict) -> Sides:
    """Render `tags` in both modes under `root`: `placeholder/` and `stub/`.

    Raises:
        RenderError: A template fails to render.
        RenderRefused: A directory to write already holds files.
        ValueError: A pinned fact shares a name with a secret key.
    """
    placeholder = render_placeholders(tags, keys)
    rendered = [t.tag for t in placeholder.tags]
    stub: list[RenderedTag] = [render_tag(tag, stubs) for tag in rendered]
    write_cluster(placeholder.tags, root / PLACEHOLDER)
    write_cluster(stub, root / STUB)
    return Sides(root, placeholder, rendered)


def compare_sides(
    sides: Sides,
    substitute: Mapping[str, str],
    flux: str,
    run: Runner = subprocess.run,
) -> Report:
    """Build every directory on both sides and compare the outputs.

    Args:
        sides: The two renders, from `render_sides`.
        substitute: Each secret key mapped to the value the placeholder side substitutes,
            its R1 stub for a round trip that must come out equal.
        flux: The flux binary.
        run: The subprocess call; a test passes a stand-in.
    """
    report = Report(sides.tags, sides.placeholder.skipped)
    kdir = sides.root / KUSTOMIZATIONS
    kdir.mkdir(parents=True, exist_ok=True)
    for name, keys in sorted(sides.placeholder.key_sets.items()):
        report.directories.append(name)
        if keys:
            report.substituting.append(name)
        outputs = {}
        for side in (PLACEHOLDER, STUB):
            substitutes = side == PLACEHOLDER and bool(keys)
            kfile = kdir / f"{name}.{side}.yaml"
            kfile.write_text(
                kustomization_file(name, substitute if substitutes else None)
            )
            path = sides.cluster(side) / name
            result = build(flux, name, path, kfile, substitutes, run)
            if result.returncode != 0:
                tail = " ".join(result.stderr.strip().splitlines()[-3:])
                report.differences.append(
                    f"{name}: flux build failed on the {side} side "
                    f"(exit {result.returncode}): {tail}"
                )
                break
            outputs[side] = result.stdout
        else:
            report.objects += len(documents(outputs[STUB]))
            difference = first_difference(outputs[PLACEHOLDER], outputs[STUB])
            if difference:
                report.differences.append(f"{name}: {difference}")
    return report
