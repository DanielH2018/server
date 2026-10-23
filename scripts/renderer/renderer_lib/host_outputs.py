"""Secret-bearing files in host mode: declared outputs, compose files and credentials.

H4's *Secret-bearing files* (docs/target-architecture/host/h4-secrets-unit.md) gives host mode
two exceptions to "render no secret", and `host.py` calls this module for both. Every other host
file keeps the rule.

A spec lists each secret-bearing file under `outputs:`::

    outputs:
      - src: templates/compose.env.j2         # in the component, rendered like a file's src
        path: /usr/lib/homelab/compose/compose.env   # where the consumer looks for it
        owner: root
        group: root
        mode: "0600"
        keys: [alloy_pi_http_password]        # every secret the template reads, exactly
        dependents: [homelab-compose.service] # units reconcile restarts when it changes
        persist: false                        # true puts it under /var/lib/homelab-secrets
        link: true                            # false ships no symlink at `path` (a drop-in)
        destination: usr/lib/homelab/compose/compose.env    # default: `path` without its `/`

The renderer reads each declared secret as a placeholder, and C1's escape applies: a literal `$`
followed by `$`, by `{` or by a placeholder renders as `$$` (`placeholders.Sentinels.resolve`).
The result installs at `/usr/lib/homelab/secret-templates/<path>`, and `homelab-secrets` renders
it. A `link` output also gets a symlink at `path` that points at the rendered file.

A `files:` entry with `compose: true` is the other kind. It installs at its own `dst`, which sits
under `/usr/lib/homelab/compose/`, because Compose does that file's substitution. It is escaped
the same way and appears in no `outputs` list.

`credentials:` lists the secret keys `homelab-secrets` writes to `/run/credstore`.

A package that lists the `secrets` component also gets `/usr/lib/homelab/secrets.json`, its
per-host ciphertext and `secrets_inputs_sha256` in its manifest. A package with an output or a
credential and no `secrets` component fails the render, and so does an output whose dependent
the package marks `windowed`. `scripts/secrets_mgmt/host_secrets.py:tree_keys` reads `credentials`
and each output's `keys` from the same specs, so the two agree on the names.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.host_manifest import UNIT_NAME
from renderer.renderer_lib.host_package import Package
from renderer.renderer_lib.host_sources import file_mode
from renderer.renderer_lib.placeholders import Sentinels

# A rendered output and its template, and what `homelab-secrets` reads (the script's constants).
TEMPLATE_ROOT = PurePosixPath("/usr/lib/homelab/secret-templates")
COMPOSE_ROOT = PurePosixPath("/usr/lib/homelab/compose")
SECRETS_JSON = "/usr/lib/homelab/secrets.json"
SOPS_ROOT = PurePosixPath("/usr/lib/homelab/secrets")
OUTPUT_ROOTS = {False: "/run/homelab-secrets", True: "/var/lib/homelab-secrets"}
# Names the script keeps at the top of each output root for its queue and digests.
STATE_NAMES = frozenset({".digests", ".hmac-key", "pending.json", "pending.lock"})
SOPS_SUFFIX = ".sops.yaml"

REQUIRED = frozenset({"src", "path", "owner", "group", "mode", "keys"})
OPTIONAL = frozenset({"destination", "persist", "dependents", "link"})
# The script's `NAME` and `KEY`: a placeholder name, and a credential's file name.
PLACEHOLDER_NAME = re.compile(r"[A-Za-z0-9_]+")
CREDENTIAL_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]*")

# Renders `src` with the secrets in `keys` read as placeholders; host.py supplies it.
Render = Callable[[str, frozenset[str]], str]


def add_credentials(
    package: Package, directory: Path, credentials: object, names: frozenset[str]
) -> None:
    """Record the secret keys a spec lists under `credentials:`.

    Raises:
        RenderRefused: The value is not a list of keys of `secrets.yml`.
    """
    if not isinstance(credentials, list):
        raise RenderRefused(f"{directory}: credentials {credentials!r} is not a list")
    for key in credentials:
        if not isinstance(key, str) or not CREDENTIAL_NAME.fullmatch(key):
            raise RenderRefused(f"{directory}: credential {key!r} is not a key name")
        if key not in names:
            raise RenderRefused(
                f"{directory}: credential {key} is not a key of secrets.yml"
            )
        if key not in package.credentials:
            package.credentials.append(key)


def add_output(
    package: Package,
    directory: Path,
    entry: object,
    names: frozenset[str],
    render: Render,
) -> None:
    """Render one declared output into `package`, with its symlink and its `secrets.json` row.

    Raises:
        RenderRefused: The entry is malformed, or its declared `keys` differ from the secrets
            its template reads.
    """
    if not isinstance(entry, dict):
        raise RenderRefused(f"{directory}: output {entry!r} is not a map")
    missing = sorted(REQUIRED - set(entry))
    unknown = sorted(set(entry) - REQUIRED - OPTIONAL)
    if missing or unknown:
        raise RenderRefused(
            f"{directory}: output {entry.get('path')!r} lacks {missing} or has unknown keys {unknown}"
        )
    path = PurePosixPath(str(entry["path"]))
    where = f"{directory}: output {path}"
    if (
        not path.is_absolute()
        or ".." in path.parts
        or len(path.parts) < 2
        or any(path.is_relative_to(root) for root in (TEMPLATE_ROOT, COMPOSE_ROOT))
        or any(
            path.is_relative_to(root)
            for root in map(PurePosixPath, OUTPUT_ROOTS.values())
        )
    ):
        raise RenderRefused(
            f"{where}: path is not a plain absolute path outside this slice's roots"
        )
    destination = str(entry.get("destination", str(path).lstrip("/")))
    parts = destination.split("/")
    if any(p in ("", ".", "..") for p in parts) or (
        len(parts) == 1 and parts[0] in STATE_NAMES
    ):
        raise RenderRefused(
            f"{where}: destination {destination!r} is not a plain relative path"
        )
    owner, group = entry["owner"], entry["group"]
    if not all(isinstance(v, str) and ACCOUNT.fullmatch(v) for v in (owner, group)):
        raise RenderRefused(
            f"{where}: owner {owner!r} and group {group!r} are not account names"
        )
    mode = file_mode(entry["mode"], directory, str(path))
    persist, link = entry.get("persist", False), entry.get("link", True)
    if not isinstance(persist, bool) or not isinstance(link, bool):
        raise RenderRefused(f"{where}: persist and link are true or false")
    dependents = entry.get("dependents", [])
    if not isinstance(dependents, list) or not all(
        isinstance(u, str) and UNIT_NAME.fullmatch(u) for u in dependents
    ):
        raise RenderRefused(
            f"{where}: dependents {dependents!r} is not a list of unit names"
        )
    declared = entry["keys"]
    if (
        not isinstance(declared, list)
        or not declared
        or not all(
            isinstance(k, str) and PLACEHOLDER_NAME.fullmatch(k) for k in declared
        )
    ):
        raise RenderRefused(f"{where}: keys {declared!r} is not a list of key names")
    keys = frozenset(declared)
    unlisted = sorted(keys - names)
    if unlisted:
        raise RenderRefused(f"{where}: keys {unlisted} are not keys of secrets.yml")
    if any(
        o["destination"] == destination and o["persist"] == persist
        for o in package.outputs
    ):
        raise RenderRefused(f"{package.name}: two outputs render to {destination}")

    text = render(str(entry["src"]), keys)
    sentinels = Sentinels(keys)
    read = sentinels.key_set({str(path): text})
    if read != keys:
        raise RenderRefused(
            f"{where}: declared keys differ from the placeholders its template renders "
            f"(declared and unread {sorted(keys - read)}, read and undeclared {sorted(read - keys)})"
        )
    template = str(TEMPLATE_ROOT / path.relative_to("/"))
    if template in package.files:
        raise RenderRefused(f"{package.name}: two files install {template}")
    package.files[template] = (sentinels.resolve(text).encode(), 0o644)
    if link:
        if str(path) in package.links:
            raise RenderRefused(f"{package.name}: two specs install {path}")
        package.links[str(path)] = f"{OUTPUT_ROOTS[persist]}/{destination}"
    package.outputs.append(
        {
            "template": template,
            "destination": destination,
            "owner": owner,
            "group": group,
            "mode": format(mode, "04o"),
            "persist": persist,
            "dependents": sorted(set(dependents)),
            "keys": sorted(keys),
        }
    )


def compose_file(
    directory: Path, src: str, dst: str, names: frozenset[str], render: Render
) -> bytes:
    """A compose file's bytes: its secret reads as placeholders, escaped by C1's rule.

    Raises:
        RenderRefused: `dst` is outside `/usr/lib/homelab/compose/`.
    """
    if not PurePosixPath(dst).is_relative_to(COMPOSE_ROOT):
        raise RenderRefused(
            f"{directory}: compose file {dst} is outside {COMPOSE_ROOT}"
        )
    return Sentinels(names).resolve(render(src, names)).encode()


def finish(package: Package, host: str | None, sops_dir: Path) -> None:
    """The checks that need every spec rendered, and the `secrets` files of a package that ships it.

    Raises:
        RenderRefused: The package breaks a rule above, or ships `secrets` without its
            ciphertext at `sops_dir/<host>.sops.yaml`.
    """
    named = package.outputs or package.credentials
    if (named or package.secrets) and host is None:
        raise RenderRefused(
            f"{package.name}: a shared package lists no output, credential or `secrets` "
            "component; a host package does"
        )
    if named and not package.secrets:
        raise RenderRefused(
            f"{package.name}: lists an output or a credential without the `secrets` component"
        )
    for out in package.outputs:
        for unit in out["dependents"]:
            if package.policies.get(unit) == "windowed":
                raise RenderRefused(
                    f"{package.name}: output {out['destination']} lists {unit} as a dependent, "
                    f"and {unit} is windowed. Reconcile never restarts a windowed unit, so the "
                    "entry in pending.json would never drain"
                )
    clash = sorted(set(package.links) & set(package.files))
    if clash:
        raise RenderRefused(
            f"{package.name}: a symlink and a file both install {clash}"
        )
    if not package.secrets:
        return
    cipher = sops_dir / f"{host}{SOPS_SUFFIX}"
    if not cipher.is_file():
        raise RenderRefused(
            f"{package.name}: ships `secrets` and has no {cipher.name} to carry"
        )
    sops_file = str(SOPS_ROOT / cipher.name)
    cipher_bytes = cipher.read_bytes()
    manifest = {
        "sops_file": sops_file,
        "credentials": sorted(package.credentials),
        "outputs": sorted(
            package.outputs, key=lambda o: (o["persist"], o["destination"])
        ),
    }
    text = (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode()
    for dst, content in ((sops_file, cipher_bytes), (SECRETS_JSON, text)):
        if dst in package.files:
            raise RenderRefused(
                f"{package.name}: {dst} is rendered by the package itself"
            )
        package.files[dst] = (content, 0o644)
    inputs = [(sops_file, cipher_bytes), (SECRETS_JSON, text)] + [
        (dst, content)
        for dst, (content, _) in sorted(package.files.items())
        if PurePosixPath(dst).is_relative_to(TEMPLATE_ROOT)
    ]
    digest = hashlib.sha256()
    for name, content in inputs:
        digest.update(f"{name}\0{len(content)}\0".encode())
        digest.update(content)
    package.secrets_inputs_sha256 = digest.hexdigest()
