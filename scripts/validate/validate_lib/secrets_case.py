#!/usr/bin/env python3
"""The container case for a package that carries `secrets-unit` (H4.1c).

`scripts/validate/host_package_cases.py` runs it after its other cases. Until H4.2 ships the
first real host file, the `sealed` fixture of `hosts/lib/tests/container/fixture/` stands in for a
package that carries `secrets-unit`. The case:

  * installs the `homelab-tools` build the job made, so the container runs the `sops` the hosts
    run, and an `age` from the image;
  * generates a throwaway age key in the container, writes it to the path the unit reads, and
    encrypts a fixture file to it. The fixture holds the real key names with fake values;
  * renders the fixture twice, once with a stand-in ciphertext (the operator's file at H4.2) and
    once with that fixture ciphertext, and asserts the two builds differ only at the ciphertext
    path and in the manifest hash derived from it;
  * installs the fixture build, starts `homelab-secrets.service`, and reads the fake values from
    the output and the credential the unit wrote;
  * puts a different age key in place and restarts the unit, which must fail and leave the output
    as it was. This is the case's red proof: a unit that read no key would pass the step above.

The case starts the unit itself and does not rely on reconcile to do it.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from validate import host_packages
from validate.validate_lib.host_container import CaseFailed, Container, expect
from renderer.renderer_lib import host as host_mode
from validate.validate_lib.host_fixtures import (
    HOST,
    build,
    pi_disarmed_variant,
    pi_dropin_variant,
    render_fixture,
)

AGE_KEY = "/etc/homelab/age.key"
SOPS = "/usr/lib/homelab/bin/sops"
FAKE = {"nut_monitor_password": "fixture-only-not-a-secret"}
OUTPUT = "/etc/homelab-fixture/sealed.conf"
CREDENTIAL = "/run/credstore/nut_monitor_password"
PACKAGE_ROOT = f"hosts/{HOST}/root"
CIPHERTEXT = f"{PACKAGE_ROOT}/usr/lib/homelab/secrets/{HOST}.sops.yaml"
MANIFEST = f"{PACKAGE_ROOT}/usr/lib/homelab/manifest.json"
HASH_FIELD = "secrets_inputs_sha256"
NAME = "install a package that carries secrets-unit, then run homelab-secrets"


def compare_builds(real: Path, fixture: Path) -> list[str]:
    """What is wrong with the claim that two renders differ only at the ciphertext path.

    The manifest records `secrets_inputs_sha256`, a digest over the ciphertext, so it differs
    too. That one field is the only other difference allowed.
    """
    expected = {
        f"- `{CIPHERTEXT}`: content changed",
        f"- `{MANIFEST}`: content changed",
    }
    lines = host_packages.diff(real, fixture)
    problems = [f"{line} is not expected" for line in lines if line not in expected]
    problems += [f"{line} is missing" for line in sorted(expected - set(lines))]
    try:
        manifests = [
            json.loads((side / MANIFEST).read_text()) for side in (real, fixture)
        ]
    except (OSError, ValueError) as exc:
        return [*problems, f"a manifest is unreadable: {exc}"]
    hashes = [manifest.pop(HASH_FIELD, None) for manifest in manifests]
    if manifests[0] != manifests[1]:
        problems.append(f"the manifests differ beyond {HASH_FIELD}")
    if None in hashes or hashes[0] == hashes[1]:
        problems.append(f"{HASH_FIELD} is absent or did not follow the ciphertext")
    return problems


def encrypt(c: Container, values: dict[str, str]) -> str:
    """The ciphertext of `values`, encrypted in `c` to the public half of its age key."""
    public = c.ok(f"age-keygen -y {AGE_KEY}").strip()
    plain = "".join(f"{key}: {value}\n" for key, value in values.items())
    # A scratch directory with no .sops.yaml above it, so only the flags choose the recipient.
    c.ok(
        f"rm -rf /tmp/seal && mkdir /tmp/seal && cd /tmp/seal && printf %s {shlex.quote(plain)}"
        f" > {HOST}.yaml && {SOPS} --encrypt --age {public} --input-type yaml"
        f" --output-type yaml {HOST}.yaml > {HOST}.sops.yaml"
    )
    return c.ok(f"cat /tmp/seal/{HOST}.sops.yaml")


def seal_pi(cases, c: Container, seal=None, make_deb=None) -> None:
    """Rebuild the real Pi package and its two variants with a ciphertext `c` can decrypt.

    The real package ships `secrets-unit` and the operator's ciphertext, which only the Pi's key
    decrypts. Reconcile exits 1 when `homelab-secrets` cannot run, so a case that installs the
    real package needs the same treatment the `sealed` fixture gets: a throwaway age key in `c`,
    and a ciphertext encrypted to it for the real package's credentials, with fake values.
    Only the ciphertext and the manifest hash derived from it differ from the real build.
    `cases.debs` then names the sealed builds, at the versions of the real ones.

    `seal` and `make_deb` default to `encrypt` and `build`; a test passes stand-ins.
    """
    seal, make_deb = seal or encrypt, make_deb or build
    if not cases.tools:
        raise CaseFailed(
            "host_packages built no homelab-tools, so the container has no sops"
        )
    c.ok(
        "DEBIAN_FRONTEND=noninteractive apt-get install -y"
        f" /srv/debs/{cases.debs[cases.tools[0]].name}"
    )
    c.ok(f"install -d -m 0755 /etc/homelab && age-keygen -o {AGE_KEY}")
    credentials = host_mode.render_host(HOST).credentials
    expect(bool(credentials), "the real Pi package lists no credential to seal")
    sealed = seal(c, {key: f"fixture-only-{key}" for key in credentials})
    root = cases.work / "build" / f"pi-sealed-{c.name}"
    seal_dir = root / "sops"
    seal_dir.mkdir(parents=True)
    (seal_dir / f"{HOST}.sops.yaml").write_text(sealed)
    head = root / "head"
    host_mode.write_packages([host_mode.render_host(HOST, sops_dir=seal_dir)], head)
    builds = {
        "pi-head": head / "hosts" / HOST,
        "pi-dropin": pi_dropin_variant(head, root / "dropin"),
        "pi-disarmed": pi_disarmed_variant(root / "disarmed", sops_dir=seal_dir),
    }
    for label, directory in builds.items():
        deb = make_deb(directory, cases.versions[label], root / f"{label}-deb")
        cases.add(label, deb, name=f"sealed-{c.name}-{deb.name}")


def _body(cases, c: Container) -> None:
    if not cases.tools:
        raise CaseFailed(
            "host_packages built no homelab-tools, so the container has no sops"
        )
    tools = f"/srv/debs/{cases.debs[cases.tools[0]].name}"
    c.ok(f"DEBIAN_FRONTEND=noninteractive apt-get install -y {tools}")
    c.ok(f"install -d -m 0755 /etc/homelab && age-keygen -o {AGE_KEY}")
    sealed = encrypt(c, FAKE)
    expect(
        all(value not in sealed for value in FAKE.values()),
        "the fixture ciphertext holds a plaintext value",
    )
    build_root = cases.work / "build"
    seal_dir = build_root / "sealed-sops"
    seal_dir.mkdir()
    (seal_dir / f"{HOST}.sops.yaml").write_text(sealed)
    real_out, fixture_out = build_root / "sealed-real", build_root / "sealed-fixture"
    render_fixture("sealed", real_out)
    fixture = render_fixture("sealed", fixture_out, sops_dir=seal_dir)
    problems = compare_builds(real_out, fixture_out)
    expect(not problems, "; ".join(problems))
    version = f"{cases.versions['updater-head']}.5"
    deb = build(fixture, version, build_root / "sealed-deb")
    shutil.copy(deb, cases.work / "debs" / deb.name)
    updater = cases.debs["updater-head"].name
    c.ok(
        "DEBIAN_FRONTEND=noninteractive apt-get install -y"
        f" /srv/debs/{updater} /srv/debs/{deb.name} {tools}"
    )
    # Joins the reconcile the postinst queued, so it cannot overlap the unit started below.
    c.sh("systemctl start homelab-reconcile.service")
    status = c.sh("systemctl restart homelab-secrets.service").returncode
    expect(status == 0, f"homelab-secrets.service exited non-zero ({status})")
    want = f"password={FAKE['nut_monitor_password']}\n"
    expect(c.read(OUTPUT) == want, f"{OUTPUT} holds {c.read(OUTPUT)!r}, not {want!r}")
    got = c.read(CREDENTIAL)
    expect(got == FAKE["nut_monitor_password"], f"{CREDENTIAL} holds {got!r}")
    # The red proof: another key cannot decrypt the file, and the output stays as it was.
    c.ok(f"cp {AGE_KEY} /tmp/real.key && age-keygen -o /tmp/other.key")
    c.ok(f"install -m 0600 /tmp/other.key {AGE_KEY}")
    status = c.sh("systemctl restart homelab-secrets.service").returncode
    c.ok(f"install -m 0600 /tmp/real.key {AGE_KEY}")
    expect(status != 0, "homelab-secrets.service succeeded with the wrong age key")
    expect(c.read(OUTPUT) == want, "a failed decrypt changed the output")


def run(cases) -> None:
    """Boot a container, run the case, and record the verdict on `cases`."""
    c = cases.boot("sealed")
    if c is None:
        return
    try:
        _body(cases, c)
    except (CaseFailed, subprocess.SubprocessError) as exc:
        cases.failed.append(NAME)
        print(f"[FAIL] {c.name}: {NAME}: {exc}", file=sys.stderr)
        journal = c.sh(
            "journalctl -u homelab-secrets.service -u homelab-reconcile.service"
            " -n 60 --no-pager -o short-monotonic"
        ).stdout
        print(f"::group::journal after {NAME}\n{journal}::endgroup::", file=sys.stderr)
    else:
        print(f"[PASS] {c.name}: {NAME}", file=sys.stderr)
    finally:
        c.remove()
